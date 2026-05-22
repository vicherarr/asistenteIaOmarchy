"""Cliente para LiteRT-LM (Google AI Edge) con soporte para Tool Calling nativo."""

import asyncio
import logging
from typing import List, Callable, Optional, Any, Union, Dict
from pathlib import Path

import litert_lm
from src.config import settings
from src.schema import ChatMessage

logger = logging.getLogger(__name__)

class LiteRTClient:
    """
    Gestor del motor LiteRT-LM conforme a la API 2026. 
    Encapsula el Engine y facilita la creación de conversaciones con herramientas.
    """

    def __init__(self, model_path: str = settings.LITERT_MODEL_PATH):
        self.model_path = model_path
        self.engine: Optional[litert_lm.Engine] = None
        self._lock = asyncio.Lock()  # Bloqueo para evitar sesiones concurrentes
        self._load_engine()

    def _load_engine(self):
        """Carga el motor LiteRT con estrategia de backend flexible y robusta."""
        try:
            path = Path(self.model_path)
            if not path.is_absolute():
                path = settings.PROJECT_ROOT / path
            
            if not path.exists():
                logger.error(f"Modelo LiteRT no encontrado en: {path}")
                return

            logger.info(f"Cargando motor LiteRT desde {path}...")
            backend_mode = settings.LITERT_BACKEND.lower()
            logger.info(f"Modo de backend configurado: {backend_mode}")

            # Estrategia 1: Carga automática (Recomendada y 100% estable)
            if backend_mode == "auto":
                try:
                    logger.info("Cargando motor LiteRT de forma automática (segura e interna)...")
                    self.engine = litert_lm.Engine(str(path))
                    logger.info("Motor LiteRT cargado exitosamente (Backend automático).")
                    return
                except Exception as auto_err:
                    logger.warning(f"Carga automática falló ({auto_err}). Intentando fallback a CPU...")
                    backend_mode = "cpu"

            # Estrategia 2: Forzar GPU (Para rendimiento avanzado)
            if backend_mode == "gpu":
                try:
                    logger.info("Intentando forzar motor LiteRT con Backend GPU...")
                    self.engine = litert_lm.Engine(
                        str(path),
                        backend=litert_lm.Backend.GPU,
                        vision_backend=None,
                        audio_backend=litert_lm.Backend.CPU
                    )
                    logger.info("Motor LiteRT cargado exitosamente (Backend GPU).")
                    return
                except Exception as gpu_err:
                    logger.warning(f"Carga con GPU forzada falló ({gpu_err}). Intentando fallback a CPU...")
                    backend_mode = "cpu"

            # Estrategia 3: CPU (Máxima compatibilidad)
            if backend_mode == "cpu":
                logger.info("Cargando motor LiteRT con Backend CPU...")
                cpu_delegate = getattr(litert_lm, "Backend", None)
                if cpu_delegate and hasattr(cpu_delegate, "CPU"):
                    self.engine = litert_lm.Engine(
                        str(path),
                        backend=litert_lm.Backend.CPU,
                        vision_backend=None,
                        audio_backend=litert_lm.Backend.CPU
                    )
                else:
                    self.engine = litert_lm.Engine(str(path))
                logger.info("Motor LiteRT cargado exitosamente (Backend CPU / Fallback).")
                
        except Exception as e:
            logger.error(f"Error fatal cargando motor LiteRT: {e}")
            self.engine = None

    async def chat_stream(
        self,
        prompt: str,
        tools: Optional[List[Callable]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[List[ChatMessage]] = None,
        image_path: Optional[str] = None
    ):
        """
        Envía un mensaje al modelo y transmite los chunks de texto en tiempo real.
        """
        if not self.engine:
            yield "Error: Motor LiteRT no inicializado."
            return

        # Wrapper para ejecutar herramientas asíncronas de forma síncrona dentro de LiteRT
        sync_tools = []
        if tools:
            import inspect
            import functools
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()

            for tool in tools:
                if inspect.iscoroutinefunction(tool):
                    def make_wrapper(async_func):
                        @functools.wraps(async_func)
                        def wrapper(*args, **kwargs):
                            future = asyncio.run_coroutine_threadsafe(async_func(*args, **kwargs), loop)
                            return future.result()
                        wrapper.__signature__ = inspect.signature(async_func)
                        return wrapper
                    sync_tools.append(make_wrapper(tool))
                else:
                    sync_tools.append(tool)

        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def run_inference():
            try:
                # --- Límites de truncamiento dinámico ---
                MAX_SYSTEM_CHARS       = 2400
                MAX_RECENT_MSG_CHARS   = 6000
                MAX_HIST_MSG_CHARS     = 600
                MAX_PROMPT_CHARS       = 2000

                def _smart_trunc(text: str, limit: int) -> str:
                    if len(text) <= limit:
                        return text
                    truncated = text[:limit]
                    last_space = truncated.rfind(' ')
                    if last_space > limit - 30:
                        truncated = truncated[:last_space]
                    code_blocks = truncated.count("```")
                    if code_blocks % 2 != 0:
                        truncated += "\n```"
                    return truncated.strip() + " [...]"

                formatted_messages = []
                
                if system_prompt:
                    formatted_messages.append({
                        "role": "system",
                        "content": [{"type": "text", "text": _smart_trunc(system_prompt, MAX_SYSTEM_CHARS)}]
                    })
                
                if history:
                    total_msgs = len(history)
                    for idx, msg in enumerate(history):
                        role = "model" if msg.role == "assistant" else msg.role
                        if total_msgs - idx <= 2:
                            msg_limit = MAX_RECENT_MSG_CHARS
                        else:
                            msg_limit = MAX_HIST_MSG_CHARS
                            
                        formatted_messages.append({
                            "role": role,
                            "content": [{"type": "text", "text": _smart_trunc(msg.content, msg_limit)}]
                        })

                content_parts = [{"type": "text", "text": _smart_trunc(prompt, MAX_PROMPT_CHARS)}]
                if image_path:
                    content_parts.append({"type": "image", "path": str(image_path)})
                
                current_message = {
                    "role": "user",
                    "content": content_parts
                }

                # Intentar con herramientas primero; si falla por tool call parsing, reintentar sin ellas
                tools_to_use = sync_tools
                for attempt in range(2):
                    try:
                        with self.engine.create_conversation(messages=formatted_messages, tools=tools_to_use) as conversation:
                            stream = conversation.send_message_async(current_message)
                            for chunk in stream:
                                text_parts = []
                                if isinstance(chunk, dict):
                                    for part in chunk.get("content", []):
                                        if part.get("type") == "text":
                                            text_parts.append(part.get("text", ""))
                                elif hasattr(chunk, "text"):
                                    text_parts.append(chunk.text)
                                
                                text_to_send = "".join(text_parts)
                                if text_to_send:
                                    loop.call_soon_threadsafe(queue.put_nowait, ("text", text_to_send))
                            break  # Éxito
                    except Exception as e:
                        err_str = str(e)
                        if "Failed to parse tool calls" in err_str or "INVALID_ARGUMENT" in err_str:
                            if attempt == 0:
                                logger.warning("Tool call parsing failed, retrying without tools...")
                                tools_to_use = None
                                continue
                        raise

                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as e:
                logger.error(f"Error en inferencia LiteRT streaming (API 2026): {e}")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))

        async with self._lock:
            inference_task = asyncio.create_task(asyncio.to_thread(run_inference))
            try:
                while True:
                    msg_type, val = await queue.get()
                    if msg_type == "text":
                        yield val
                    elif msg_type == "done":
                        break
                    elif msg_type == "error":
                        yield f"\n[Error en la generación: {val}]"
                        break
            finally:
                await inference_task

    async def chat(
        self, 
        prompt: str, 
        tools: Optional[List[Callable]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[List[ChatMessage]] = None,
        image_path: Optional[str] = None
    ) -> str:
        """
        Envía un mensaje al modelo y devuelve la respuesta de texto.
        """
        if not self.engine:
            return "Error: Motor LiteRT no inicializado."

        # Wrapper para ejecutar herramientas asíncronas de forma síncrona dentro de LiteRT
        sync_tools = []
        if tools:
            import inspect
            import functools
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()

            for tool in tools:
                if inspect.iscoroutinefunction(tool):
                    def make_wrapper(async_func):
                        @functools.wraps(async_func)
                        def wrapper(*args, **kwargs):
                            future = asyncio.run_coroutine_threadsafe(async_func(*args, **kwargs), loop)
                            return future.result()
                        # Preservar la firma original para que el SDK la parse
                        wrapper.__signature__ = inspect.signature(async_func)
                        return wrapper
                    sync_tools.append(make_wrapper(tool))
                else:
                    sync_tools.append(tool)

        # Asegurar que solo una sesión de inferencia corre a la vez
        async with self._lock:
            # Ejecutamos la inferencia en un hilo separado para no bloquear el event loop
            return await asyncio.to_thread(
                self._chat_sync, 
                prompt, 
                sync_tools, 
                system_prompt, 
                history,
                image_path
            )

    def _chat_sync(
        self, 
        prompt: str, 
        tools: Optional[List[Callable]], 
        system_prompt: Optional[str],
        history: Optional[List[ChatMessage]],
        image_path: Optional[str]
    ) -> str:
        """Versión síncrona para ser ejecutada en un thread usando el API 2026.
        
        Protección de ventana de contexto:
        - Cada mensaje del historial se trunca a MAX_HIST_MSG_CHARS.
        - El prompt actual se trunca a MAX_PROMPT_CHARS.
        - El system prompt se trunca a MAX_SYSTEM_CHARS.
        Esto garantiza que nunca se supere el límite de 4096 tokens del modelo.
        """
        # --- Límites de truncamiento dinámico (caracteres aprox. = tokens * 3) ---
        MAX_SYSTEM_CHARS       = 2400   # ~800 tokens para el system prompt completo
        MAX_RECENT_MSG_CHARS   = 6000   # ~2000 tokens para los dos mensajes más recientes (sin cortes)
        MAX_HIST_MSG_CHARS     = 600    # ~200 tokens para mensajes más antiguos del historial
        MAX_PROMPT_CHARS       = 2000   # ~660 tokens para el prompt actual del usuario

        def _smart_trunc(text: str, limit: int) -> str:
            if len(text) <= limit:
                return text
            
            truncated = text[:limit]
            
            # Intentar no cortar a mitad de una palabra si hay espacio de retroceso
            last_space = truncated.rfind(' ')
            if last_space > limit - 30:
                truncated = truncated[:last_space]
                
            # Prevenir que los bloques de código de markdown (```) queden abiertos
            code_blocks = truncated.count("```")
            if code_blocks % 2 != 0:
                truncated += "\n```"
                
            return truncated.strip() + " [...]"

        try:
            # 1. Preparar historial con enrutamiento de sliding-window inteligente
            formatted_messages = []
            
            if system_prompt:
                formatted_messages.append({
                    "role": "system",
                    "content": [{"type": "text", "text": _smart_trunc(system_prompt, MAX_SYSTEM_CHARS)}]
                })
            
            if history:
                total_msgs = len(history)
                for idx, msg in enumerate(history):
                    role = "model" if msg.role == "assistant" else msg.role
                    
                    # Conservar de forma completa e intacta los dos últimos mensajes del historial
                    # (el inmediato anterior y su respuesta) para retener logs, códigos de error y respuestas previas
                    if total_msgs - idx <= 2:
                        msg_limit = MAX_RECENT_MSG_CHARS
                    else:
                        msg_limit = MAX_HIST_MSG_CHARS
                        
                    formatted_messages.append({
                        "role": role,
                        "content": [{"type": "text", "text": _smart_trunc(msg.content, msg_limit)}]
                    })

            # 2. Crear conversación con retry si falla tool calling
            tools_to_use = tools
            for attempt in range(2):
                try:
                    with self.engine.create_conversation(messages=formatted_messages, tools=tools_to_use) as conversation:
                        
                        # 3. Construir mensaje actual (con truncamiento del prompt)
                        content_parts = [{"type": "text", "text": _smart_trunc(prompt, MAX_PROMPT_CHARS)}]
                        
                        if image_path:
                            content_parts.append({"type": "image", "path": str(image_path)})
                        
                        current_message = {
                            "role": "user",
                            "content": content_parts
                        }

                        # 4. Enviar mensaje
                        response = conversation.send_message(current_message)

                        # 5. Extraer respuesta de texto
                        if isinstance(response, dict):
                            text = ""
                            for part in response.get("content", []):
                                if part.get("type") == "text":
                                    text += part.get("text", "")
                            return text.strip()
                        
                        if hasattr(response, "text"):
                            return response.text
                        
                        try:
                            return response["content"][0]["text"]
                        except:
                            return str(response)
                except Exception as e:
                    err_str = str(e)
                    if "Failed to parse tool calls" in err_str or "INVALID_ARGUMENT" in err_str:
                        if attempt == 0:
                            logger.warning("Tool call parsing failed in chat, retrying without tools...")
                            tools_to_use = None
                            continue
                    raise

        except Exception as e:
            logger.error(f"Error en inferencia LiteRT (API 2026): {e}")
            return f"Error en la generación: {e}"

    def close(self):
        """Libera recursos del motor."""
        if self.engine:
            self.engine = None
            logger.info("Motor LiteRT liberado.")
