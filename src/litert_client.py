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
        """Carga el motor LiteRT con estrategia de backend flexible."""
        try:
            path = Path(self.model_path)
            if not path.is_absolute():
                path = settings.PROJECT_ROOT / path
            
            if not path.exists():
                logger.error(f"Modelo LiteRT no encontrado en: {path}")
                return

            logger.info(f"Cargando motor LiteRT desde {path}...")
            
            # Intentar carga automática (el SDK elegirá el mejor backend disponible)
            try:
                self.engine = litert_lm.Engine(str(path))
                logger.info("Motor LiteRT cargado exitosamente (Backend automático).")
            except Exception as e:
                logger.warning(f"Carga automática falló ({e}). Intentando fallback a CPU...")
                # Fallback explícito a CPU para máxima compatibilidad
                self.engine = litert_lm.Engine(str(path), vision_backend=litert_lm.Backend.CPU)
                logger.info("Motor LiteRT cargado exitosamente (Fallback CPU).")
                
        except Exception as e:
            logger.error(f"Error fatal cargando motor LiteRT: {e}")
            self.engine = None

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
        """Versión síncrona para ser ejecutada en un thread usando el API 2026."""
        try:
            # 1. Preparar historial
            formatted_messages = []
            
            if system_prompt:
                formatted_messages.append({
                    "role": "system",
                    "content": [{"type": "text", "text": system_prompt}]
                })
            
            if history:
                for msg in history:
                    role = "model" if msg.role == "assistant" else msg.role
                    formatted_messages.append({
                        "role": role,
                        "content": [{"type": "text", "text": msg.content}]
                    })

            # 2. Crear conversación
            with self.engine.create_conversation(messages=formatted_messages, tools=tools) as conversation:
                
                # 3. Construir mensaje actual
                content_parts = [{"type": "text", "text": prompt}]
                
                if image_path:
                    # Según docs 2026, pasar la ruta ('path') es lo más estable
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
            logger.error(f"Error en inferencia LiteRT (API 2026): {e}")
            return f"Error en la generación: {e}"

    def close(self):
        """Libera recursos del motor."""
        if self.engine:
            self.engine = None
            logger.info("Motor LiteRT liberado.")
