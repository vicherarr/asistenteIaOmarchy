"""Servicio principal del asistente, encapsula la lógica de negocio."""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

from src.command_executor import CommandExecutor, parse_gemma_response
from src.ollama_client import OllamaClient, OllamaMessage
from src.tts_engine import TTSEngine
from src.vision_tool import VisionTool
from src.stt_engine import STTEngine
from src.utils import strip_markdown

logger = logging.getLogger(__name__)


class AssistantService:
    """Orquesta la comunicación entre LLM, herramientas y TTS."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        command_executor: CommandExecutor,
        vision_tool: VisionTool,
        tts_engine: TTSEngine,
        stt_engine: STTEngine,
        system_prompt: str,
    ):
        self.ollama = ollama_client
        self.executor = command_executor
        self.vision = vision_tool
        self.tts = tts_engine
        self.stt = stt_engine
        self.system_prompt = system_prompt

    async def process_audio(
        self,
        audio_path: Path,
        conversation_history: list[OllamaMessage],
        max_history: int = 10
    ) -> dict:
        """Transcribe el audio y procesa el texto resultante."""
        self.send_notification("Procesando audio...")
        
        text = await self.stt.transcribe(audio_path)
        
        if not text or len(text.strip()) < 2:
            self.send_notification("No se detectó voz o el mensaje es muy corto.")
            return {"status": "error", "message": "No se detectó voz"}

        self.send_notification(f"Has dicho: {text}")
        return await self.process_transcription(text, conversation_history, max_history)

    def send_notification(self, message: str, title: str = "AsistenteIA") -> None:
        """Envía una notificación de escritorio."""
        try:
            subprocess.Popen(["notify-send", title, message])
        except Exception as e:
            logger.warning(f"No se pudo enviar notificación: {e}")

    async def process_transcription(
        self,
        text: str,
        conversation_history: list[OllamaMessage],
        max_history: int = 10
    ) -> dict:
        """
        Procesa el texto del usuario y ejecuta las acciones necesarias.
        Devuelve un diccionario con el resultado.
        """
        logger.info(f"Procesando transcripción: {text[:100]}...")

        conversation_history.append(OllamaMessage(role="user", content=text))

        if len(conversation_history) > max_history:
            conversation_history[:] = conversation_history[-max_history:]

        messages = [OllamaMessage(role="system", content=self.system_prompt)] + conversation_history

        gemma_response = await self.ollama.generate(messages)
        parsed = parse_gemma_response(gemma_response)

        conversation_history.append(OllamaMessage(role="assistant", content=gemma_response))

        commands_executed = 0
        if parsed.commands:
            results = await self.executor.execute_multiple(parsed.commands)
            commands_executed = sum(1 for success, _ in results if success)
            for success, output in results:
                status = "OK" if success else "FALLÓ"
                logger.info(f"Comando [{status}]: {output}")

        if parsed.action_type == "vision":
            parsed.response_text = await self._handle_vision_action()

        audio_file: Optional[str] = None
        if parsed.response_text and parsed.action_type in ("speak", "both", "vision"):
            clean_text = strip_markdown(parsed.response_text)
            try:
                audio_file = await self.tts.speak_async(clean_text)
            except Exception as e:
                logger.error(f"Error en TTS: {e}")

        return {
            "status": "success",
            "response_text": parsed.response_text,
            "commands_executed": commands_executed,
            "audio_file": audio_file,
        }

    async def _handle_vision_action(self) -> str:
        """Maneja la acción de visión de forma modular."""
        try:
            logger.info("Acción de visión: capturando pantalla")
            image_base64 = await asyncio.to_thread(self.vision.get_screen_for_vision)
            vision_response = await self.ollama.generate_with_image(
                text="Describe brevemente qué se ve en esta pantalla. Sé conciso y útil.",
                image_base64=image_base64,
            )
            logger.info(f"Respuesta de visión: {vision_response[:100]}...")
            return vision_response
        except Exception as e:
            logger.error(f"Error en visión: {e}")
            return "No pude capturar la pantalla en este momento."
