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
        self._load_engine()

    def _load_engine(self):
        """Carga el motor LiteRT. Operación síncrona al inicio."""
        try:
            path = Path(self.model_path)
            if not path.is_absolute():
                path = settings.PROJECT_ROOT / path
            
            if not path.exists():
                logger.error(f"Modelo LiteRT no encontrado en: {path}")
                return

            logger.info(f"Cargando motor LiteRT desde {path}...")
            # La inicialización del Engine se mantiene estándar
            self.engine = litert_lm.Engine(str(path))
            logger.info("Motor LiteRT cargado exitosamente.")
        except Exception as e:
            logger.error(f"Error cargando motor LiteRT: {e}")

    async def chat(
        self, 
        prompt: str, 
        tools: Optional[List[Callable]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[List[ChatMessage]] = None,
        image: Optional[Any] = None
    ) -> str:
        """
        Envía un mensaje al modelo y devuelve la respuesta de texto.
        """
        if not self.engine:
            return "Error: Motor LiteRT no inicializado."

        # Ejecutamos la inferencia en un hilo separado para no bloquear el event loop
        return await asyncio.to_thread(
            self._chat_sync, 
            prompt, 
            tools, 
            system_prompt, 
            history,
            image
        )

    def _chat_sync(
        self, 
        prompt: str, 
        tools: Optional[List[Callable]], 
        system_prompt: Optional[str],
        history: Optional[List[ChatMessage]],
        image: Optional[Any]
    ) -> str:
        """Versión síncrona para ser ejecutada en un thread usando el API 2026."""
        try:
            # 1. Preparar historial en el formato oficial de LiteRT-LM (2026)
            # Cada mensaje es un dict: {"role": "...", "content": [{"type": "text", "text": "..."}]}
            formatted_messages = []
            
            if system_prompt:
                formatted_messages.append({
                    "role": "system",
                    "content": [{"type": "text", "text": system_prompt}]
                })
            
            if history:
                for msg in history:
                    # Mapear roles internos a los esperados por LiteRT: assistant -> model
                    role = "model" if msg.role == "assistant" else msg.role
                    formatted_messages.append({
                        "role": role,
                        "content": [{"type": "text", "text": msg.content}]
                    })

            # 2. Crear conversación con historial y herramientas
            # En la API 2026, tools se registra en create_conversation
            with self.engine.create_conversation(messages=formatted_messages, tools=tools) as conversation:
                # 3. Enviar mensaje actual
                # Según docs, si hay imagen se pasa como parte del contenido o argumento
                if image:
                    # Formato multimodal: send_message puede aceptar la imagen directamente
                    response = conversation.send_message(prompt, image=image)
                else:
                    # send_message es el método estándar en 2026 (sustituye a .chat)
                    response = conversation.send_message(prompt)

                # 4. Extraer texto de la respuesta estructurada
                # La respuesta es un diccionario o objeto con content[0][text]
                if isinstance(response, dict):
                    return response["content"][0]["text"]
                elif hasattr(response, "text"):
                    return response.text
                else:
                    # Fallback para diferentes versiones de la respuesta estructurada
                    try:
                        return response.content[0].text
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
