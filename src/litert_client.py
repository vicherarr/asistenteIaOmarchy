"""Cliente para LiteRT-LM (Google AI Edge) con soporte para Tool Calling nativo."""

import asyncio
import contextlib
import logging
from typing import List, Callable, Optional, Any, Union, Dict
from pathlib import Path

import litert_lm
from src.config import settings
from src.schema import ChatMessage

logger = logging.getLogger(__name__)


# Comandos que, por seguridad, no auto-ejecutamos sin que el usuario los vea.
# (gating opcional vía ToolEventHandler — Fase 4)
_DANGEROUS_TOOLS: set = set()


class _LoggingToolEventHandler(litert_lm.ToolEventHandler):
    """Observa el ciclo de tool-calling del motor (Fase 4).

    - approve_tool_call: punto de control para aprobar/denegar una llamada
      (devolver False la bloquea). Por defecto aprueba todo salvo lo que esté
      en _DANGEROUS_TOOLS.
    - process_tool_response: punto para inspeccionar/transformar la salida.
    Sirve para emitir al GUI "ejecutando herramienta X" y para seguridad.
    """

    def approve_tool_call(self, tool_call: dict) -> bool:
        name = (tool_call or {}).get("name", "?")
        logger.info(f"[tool] → llamada: {name}  args={(tool_call or {}).get('arguments', {})}")
        if name in _DANGEROUS_TOOLS:
            logger.warning(f"[tool] denegada por política: {name}")
            return False
        return True

    def process_tool_response(self, tool_response: dict) -> dict:
        logger.info(f"[tool] ← respuesta de {(tool_response or {}).get('name', '?')}")
        return tool_response


class LiteRTClient:
    """
    Gestor del motor LiteRT-LM conforme a la API 2026. 
    Encapsula el Engine y facilita la creación de conversaciones con herramientas.
    """

    def __init__(self, model_path: str = settings.LITERT_MODEL_PATH):
        self.model_path = model_path
        self.engine: Optional[litert_lm.Engine] = None
        self._lock = asyncio.Lock()  # Bloqueo para evitar sesiones concurrentes
        self.max_num_tokens: Optional[int] = None
        # Fase 3: conversación persistente (KV-cache reutilizable entre turnos)
        self._persistent_conv = None
        self._persistent_sig = None  # (system_prompt, tuple(tool_names)) para invalidar
        self._load_engine()
        self._log_capabilities()

    def _log_capabilities(self):
        """Loguea las capacidades reales del motor (Fase 0): ventana de contexto, EOS…"""
        if not self.engine:
            return
        try:
            self.max_num_tokens = getattr(self.engine, "max_num_tokens", None)
            eos = getattr(self.engine, "eos_token_ids", None)
            bos = getattr(self.engine, "bos_token_id", None)
            logger.info(
                f"Capacidades del motor: max_num_tokens={self.max_num_tokens}, "
                f"bos={bos}, eos={eos}"
            )
            # Footprint real del system prompt frente a la ventana (Fase 3).
            try:
                sp = (settings.PROJECT_ROOT / "config" / "system_prompt.txt").read_text(encoding="utf-8")
                sp_tokens = self._count_tokens(sp)
                pct = f"{100*sp_tokens/self.max_num_tokens:.0f}%" if self.max_num_tokens else "?"
                logger.info(f"System prompt: {sp_tokens} tokens ({pct} de la ventana)")
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"No se pudieron leer las capacidades del motor: {e}")

    def _count_tokens(self, text: str) -> int:
        """Cuenta tokens reales con el tokenizador del motor (fallback: aprox. por chars)."""
        if self.engine and text:
            try:
                return len(self.engine.tokenize(text))
            except Exception:  # noqa: BLE001
                pass
        return len(text) // 3

    def _sampler_config(self, agentic: bool):
        """Construye un SamplerConfig (Fase 2) según el perfil.

        agentic=True (hay herramientas): temperatura baja + seed → tool-calls fiables.
        agentic=False (charla/redacción): más natural.
        """
        SamplerConfig = getattr(litert_lm, "SamplerConfig", None)
        if SamplerConfig is None:
            return None
        top_k = settings.LITERT_TOP_K if settings.LITERT_TOP_K >= 0 else None
        seed = settings.LITERT_SEED if settings.LITERT_SEED >= 0 else None
        temperature = settings.LITERT_AGENTIC_TEMPERATURE if agentic else settings.LITERT_TEMPERATURE
        try:
            return SamplerConfig(
                top_k=top_k,
                top_p=settings.LITERT_TOP_P,
                temperature=temperature,
                seed=seed,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"No se pudo construir SamplerConfig: {e}")
            return None

    def _create_conversation(self, messages, tools):
        """Crea una conversación pasando sampler_config (y tool_event_handler si procede).

        Filtra kwargs no soportados por la build instalada para no romper.
        """
        kwargs = {"messages": messages, "tools": tools}
        sampler = self._sampler_config(agentic=bool(tools))
        if sampler is not None:
            kwargs["sampler_config"] = sampler
        if settings.LITERT_TOOL_EVENTS and tools:
            handler = _LoggingToolEventHandler()
            kwargs["tool_event_handler"] = handler
        try:
            import inspect
            params = inspect.signature(self.engine.create_conversation).parameters
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (ValueError, TypeError):
            pass
        return self.engine.create_conversation(**kwargs)

    def _get_persistent_conversation(self, system_prompt, tools):
        """Devuelve una Conversation viva reutilizable (Fase 3, KV-cache).

        Se recrea solo si cambia el system prompt o el conjunto de herramientas.
        """
        sig = (
            system_prompt or "",
            tuple(getattr(t, "__name__", "?") for t in (tools or [])),
        )
        if self._persistent_conv is not None and self._persistent_sig == sig:
            return self._persistent_conv
        self._invalidate_persistent()
        system_messages = []
        if system_prompt:
            system_messages.append({
                "role": "system",
                "content": [{"type": "text", "text": system_prompt}],
            })
        self._persistent_conv = self._create_conversation(system_messages, tools)
        self._persistent_sig = sig
        logger.info("Conversación persistente (KV-cache) creada/reseteada.")
        return self._persistent_conv

    def _invalidate_persistent(self):
        """Cierra y olvida la conversación persistente (al resetear o ante error)."""
        if self._persistent_conv is not None:
            try:
                self._persistent_conv.close()
            except Exception:  # noqa: BLE001
                pass
        self._persistent_conv = None
        self._persistent_sig = None

    def reset_conversation(self):
        """Resetea el estado conversacional persistente (lo llama /reset)."""
        self._invalidate_persistent()

    @contextlib.contextmanager
    def _conversation_ctx(self, system_prompt, formatted_messages, tools):
        """Context manager unificado: en modo persistente reutiliza la conversación
        (sin cerrarla); en modo normal crea una efímera con `with`."""
        if settings.LITERT_PERSISTENT_CONVERSATION:
            try:
                yield self._get_persistent_conversation(system_prompt, tools)
            except Exception:
                self._invalidate_persistent()
                raise
        else:
            with self._create_conversation(formatted_messages, tools) as conv:
                yield conv

    def _engine_kwargs(self) -> dict:
        """Kwargs comunes para optimizar el motor (Fase 1): MTP, caché y hint.

        Se aplican a cualquier backend. Cada uno es opcional/configurable por .env
        para poder revertir sin tocar código.
        """
        kwargs: dict = {}

        # Multi-Token Prediction (speculative decoding): hasta 2.2x decode en GPU,
        # recomendado para Gemma E4B. Universalmente recomendado en GPU.
        if settings.LITERT_SPECULATIVE_DECODING:
            kwargs["enable_speculative_decoding"] = True

        # Caché de artefactos compilados → arranque en frío más rápido.
        cache_dir = settings.LITERT_CACHE_DIR.strip() if settings.LITERT_CACHE_DIR else ""
        if not cache_dir:
            cache_dir = str(Path.home() / ".cache" / "asistenteia" / "litert")
        try:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            kwargs["cache_dir"] = cache_dir
        except OSError as e:
            logger.warning(f"No se pudo preparar cache_dir '{cache_dir}': {e}")

        # input_prompt_as_hint: prefijo estable (system prompt) → mejor TTFT del 1er turno.
        if settings.LITERT_PROMPT_HINT:
            try:
                prompt_path = settings.PROJECT_ROOT / "config" / "system_prompt.txt"
                hint = prompt_path.read_text(encoding="utf-8").strip()
                if hint:
                    kwargs["input_prompt_as_hint"] = hint
            except OSError as e:
                logger.warning(f"No se pudo leer system_prompt.txt para el hint: {e}")

        # Filtrar contra la firma real del Engine instalado: si esta build no soporta
        # algún parámetro, lo descartamos en vez de tumbar la carga del motor.
        try:
            import inspect
            params = inspect.signature(litert_lm.Engine).parameters
            unsupported = [k for k in kwargs if k not in params]
            for k in unsupported:
                logger.warning(f"litert_lm.Engine no soporta '{k}' en esta versión; se omite.")
                kwargs.pop(k)
        except (ValueError, TypeError):
            # Si no se puede introspeccionar la firma, seguimos con los kwargs tal cual.
            pass

        return kwargs

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
            opt_kwargs = self._engine_kwargs()
            logger.info(f"Optimizaciones del motor (Fase 1): {list(opt_kwargs.keys())}")

            # Estrategia 1: Carga automática (Recomendada y 100% estable)
            if backend_mode == "auto":
                try:
                    logger.info("Cargando motor LiteRT de forma automática (segura e interna)...")
                    self.engine = litert_lm.Engine(str(path), **opt_kwargs)
                    logger.info("Motor LiteRT cargado exitosamente (Backend automático).")
                    return
                except Exception as auto_err:
                    logger.warning(f"Carga automática falló ({auto_err}). Intentando fallback a CPU...")
                    backend_mode = "cpu"

            # Estrategia 2: Forzar GPU (Para rendimiento avanzado)
            # vision_backend=CPU: la visión en GPU crashea por fallo de compilación de
            # shaders WebGPU/NVVM en NVIDIA. Ejecutar el vision encoder en CPU lo evita
            # y mantiene el LLM en GPU (un solo motor hace texto, tools, audio y visión).
            if backend_mode == "gpu":
                try:
                    logger.info("Intentando forzar motor LiteRT con Backend GPU (visión en CPU)...")
                    self.engine = litert_lm.Engine(
                        str(path),
                        backend=litert_lm.Backend.GPU,
                        vision_backend=litert_lm.Backend.CPU,
                        audio_backend=litert_lm.Backend.CPU,
                        **opt_kwargs
                    )
                    logger.info("Motor LiteRT cargado exitosamente (LLM en GPU, visión en CPU).")
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
                        vision_backend=litert_lm.Backend.CPU,
                        audio_backend=litert_lm.Backend.CPU,
                        **opt_kwargs
                    )
                else:
                    self.engine = litert_lm.Engine(str(path), **opt_kwargs)
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
                        with self._conversation_ctx(system_prompt, formatted_messages, tools_to_use) as conversation:
                            stream = conversation.send_message_async(current_message)
                            for chunk in stream:
                                text_parts = []
                                if isinstance(chunk, dict):
                                    for part in chunk.get("content", []):
                                        if part.get("type") == "text":
                                            text_parts.append(part.get("text", ""))
                                elif hasattr(chunk, "text"):
                                    text_parts.append(chunk.text)
                                
                                text_to_send = "".join(text_parts).replace("\u2581", " ")
                                if text_to_send:
                                    loop.call_soon_threadsafe(queue.put_nowait, ("text", text_to_send))
                            break  # Éxito
                    except Exception as e:
                        err_str = str(e)
                        if "Failed to parse tool calls" in err_str or "INVALID_ARGUMENT" in err_str:
                            if attempt == 0:
                                logger.warning(f"Tool call parsing failed ({e}), retrying without tools...", exc_info=True)
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
                    try:
                        msg_type, val = await asyncio.wait_for(queue.get(), timeout=settings.LITERT_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.critical(
                            f"¡TIMEOUT CRÍTICO! LiteRT chat_stream superó el límite de {settings.LITERT_TIMEOUT}s "
                            "sin emitir respuesta. El modelo LLM parece colgado ('cao'). Reiniciando el servicio..."
                        )
                        try:
                            import subprocess
                            subprocess.Popen(
                                ["notify-send", "AsistenteIA - ERROR CRÍTICO", "El modelo LLM se ha bloqueado. Reiniciando servicio..."],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL
                            )
                        except Exception as notify_err:
                            logger.warning(f"No se pudo enviar notificación de reinicio: {notify_err}")
                        
                        import os
                        os._exit(1)
                        raise RuntimeError("LiteRT chat_stream timed out")

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
        async def _run_chat_under_lock():
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

        try:
            return await asyncio.wait_for(_run_chat_under_lock(), timeout=settings.LITERT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.critical(
                f"¡TIMEOUT CRÍTICO! La inferencia de LiteRT chat() superó el límite de {settings.LITERT_TIMEOUT}s. "
                "El modelo LLM parece colgado ('cao'). Reiniciando el servicio..."
            )
            try:
                import subprocess
                subprocess.Popen(
                    ["notify-send", "AsistenteIA - ERROR CRÍTICO", "El modelo LLM se ha bloqueado. Reiniciando servicio..."],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except Exception as notify_err:
                logger.warning(f"No se pudo enviar notificación de reinicio: {notify_err}")
            
            import os
            os._exit(1)
            raise RuntimeError("LiteRT chat timed out")

    async def transcribe_audio(self, audio_path: str) -> str:
        """Transcribe un audio usando el encoder de audio nativo de Gemma (LiteRT).

        Alternativa a Whisper. Ejecuta la inferencia en un hilo, bajo el lock del
        motor, con el mismo timeout que el resto de llamadas."""
        if not self.engine:
            logger.error("transcribe_audio: motor LiteRT no inicializado.")
            return ""

        async def _run_under_lock():
            async with self._lock:
                return await asyncio.to_thread(self._transcribe_audio_sync, audio_path)

        try:
            return await asyncio.wait_for(_run_under_lock(), timeout=settings.LITERT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(f"Timeout transcribiendo audio con Gemma ({settings.LITERT_TIMEOUT}s).")
            return ""

    def _transcribe_audio_sync(self, audio_path: str) -> str:
        """Versión síncrona (en hilo) de la transcripción por audio de Gemma."""
        system_message = {
            "role": "system",
            "content": [{"type": "text", "text": (
                "Eres un transcriptor de voz. Devuelve únicamente, en español y sin "
                "ningún comentario ni explicación, el texto exacto de lo que dice el "
                "usuario en el audio."
            )}],
        }
        user_message = {
            "role": "user",
            "content": [
                {"type": "audio", "path": str(audio_path)},
                {"type": "text", "text": "Transcribe este audio."},
            ],
        }
        try:
            with self.engine.create_conversation(messages=[system_message], tools=None) as conversation:
                response = conversation.send_message(user_message)

            if isinstance(response, dict):
                text = "".join(
                    part.get("text", "")
                    for part in response.get("content", [])
                    if part.get("type") == "text"
                )
            elif hasattr(response, "text"):
                text = response.text
            else:
                text = str(response)
            return text.replace("▁", " ").strip()
        except Exception as e:
            logger.error(f"Error transcribiendo audio con Gemma: {e}", exc_info=True)
            return ""

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
                    with self._conversation_ctx(system_prompt, formatted_messages, tools_to_use) as conversation:
                        
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
                            return text.replace("\u2581", " ").strip()
                        
                        if hasattr(response, "text"):
                            return response.text.replace("\u2581", " ")
                        
                        try:
                            return response["content"][0]["text"].replace("\u2581", " ")
                        except:
                            return str(response).replace("\u2581", " ")
                except Exception as e:
                    err_str = str(e)
                    if "Failed to parse tool calls" in err_str or "INVALID_ARGUMENT" in err_str:
                        if attempt == 0:
                            logger.warning(f"Tool call parsing failed in chat ({e}), retrying without tools...", exc_info=True)
                            tools_to_use = None
                            continue
                    raise

        except Exception as e:
            logger.error(f"Error en inferencia LiteRT (API 2026): {e}")
            return f"Error en la generación: {e}"

    def close(self):
        """Libera recursos del motor."""
        self._invalidate_persistent()
        if self.engine:
            self.engine = None
            logger.info("Motor LiteRT liberado.")
