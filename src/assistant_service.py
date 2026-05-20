"""Servicio principal del asistente, encapsula la lógica de negocio."""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional, List

from src.schema import ChatMessage
from src.command_executor import (
    execute_system_command, 
    get_system_status, 
    read_log_file, 
    clipboard_manager,
    web_search,
    manage_windows,
    system_diagnostics,
    read_web_page,
    interact_web,
    play_specific_music,
    open_terminal_and_run_command
)
from src.vision_tool import analyze_screen
from src.litert_client import LiteRTClient
from src.tts_engine import TTSEngine
from src.stt_engine import STTEngine
from src.utils import strip_markdown, get_pending_image
from src.config import settings
from PIL import Image

logger = logging.getLogger(__name__)


class AssistantService:
    """Orquesta la comunicación entre LLM, herramientas y TTS usando LiteRT."""

    def __init__(
        self,
        litert_client: LiteRTClient,
        tts_engine: TTSEngine,
        stt_engine: STTEngine,
    ):
        self.litert = litert_client
        self.tts = tts_engine
        self.stt = stt_engine
        self._current_tts_task: Optional[asyncio.Task] = None
        # Lista de herramientas para LiteRT
        self.tools = [
            execute_system_command,
            get_system_status,
            analyze_screen,
            read_log_file,
            clipboard_manager,
            web_search,
            manage_windows,
            system_diagnostics,
            read_web_page,
            interact_web,
            play_specific_music,
            open_terminal_and_run_command
        ]

    async def process_audio(
        self,
        audio_path: Path,
        conversation_history: list[ChatMessage],
        sink_id: Optional[str] = None,
        max_history: int = 10
    ) -> dict:
        """Transcribe el audio y procesa el texto resultante."""
        self.send_notification("Procesando audio...")
        
        text = await self.stt.transcribe(audio_path)
        
        if not text or len(text.strip()) < 2:
            self.send_notification("No se detectó voz o el mensaje es muy corto.")
            return {"status": "error", "message": "No se detectó voz"}

        self.send_notification(f"Has dicho: {text}")
        return await self.process_transcription(text, conversation_history, sink_id, max_history)

    def send_notification(self, message: str, title: str = "AsistenteIA") -> None:
        """Envía una notificación de escritorio y emite un bip si es inicio de escucha."""
        try:
            subprocess.Popen(["notify-send", title, message])
            
            # Si el mensaje es de inicio de escucha, emitir un bip sonoro
            if message == "Escuchando...":
                # Intentar reproducir un sonido de sistema estándar
                beep_sound = "/usr/share/sounds/freedesktop/stereo/message.oga"
                if not Path(beep_sound).exists():
                    beep_sound = "/usr/share/sounds/freedesktop/stereo/complete.oga"
                
                if Path(beep_sound).exists():
                    subprocess.Popen(["paplay", beep_sound], 
                                   stdout=subprocess.DEVNULL, 
                                   stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.warning(f"No se pudo enviar notificación o bip: {e}")

    async def process_transcription(
        self,
        text: str,
        conversation_history: list[ChatMessage],
        sink_id: Optional[str] = None,
        max_history: int = 10
    ) -> dict:
        """
        Procesa el texto del usuario usando LiteRT y herramientas nativas.
        """
        logger.info(f"Procesando transcripción con LiteRT: {text[:100]}...")

        # Añadir mensaje del usuario al historial
        conversation_history.append(ChatMessage(role="user", content=text))
        if len(conversation_history) > max_history:
            conversation_history[:] = conversation_history[-max_history:]

        # Cargar system prompt desde archivo
        prompt_path = settings.PROJECT_ROOT / "config" / "system_prompt.txt"
        try:
            system_prompt = prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Error cargando system prompt: {e}")
            system_prompt = "Eres un asistente de voz para Linux llamado AsistenteIA."

        # Primera llamada al modelo
        # LiteRT ejecutará las herramientas automáticamente si lo considera necesario
        response_text = await self.litert.chat(
            prompt=text,
            tools=self.tools,
            system_prompt=system_prompt,
            history=conversation_history[:-1] # Pasamos el historial previo
        )

        logger.info(f"Respuesta de LiteRT: {response_text}")

        # Comprobar si una herramienta de visión dejó una imagen pendiente
        pending_image_path = get_pending_image()
        if pending_image_path:
            logger.info("Imagen detectada de tool 'analyze_screen'. Realizando segunda pasada visual...")
            try:
                # Segunda llamada incluyendo la RUTA de la imagen (más estable en 2026)
                response_text = await self.litert.chat(
                    prompt="Describe qué ves en esta imagen y responde a la petición original del usuario.",
                    image_path=pending_image_path,
                    system_prompt=system_prompt,
                    history=conversation_history # Incluimos el turno actual
                )
                Path(pending_image_path).unlink()
            except Exception as e:
                logger.error(f"Error procesando imagen pendiente: {e}")
                response_text = "Lo siento, tuve un problema al procesar la imagen de tu pantalla."

        # Guardar respuesta en el historial
        conversation_history.append(ChatMessage(role="assistant", content=response_text))

        if response_text:
            clean_text = strip_markdown(response_text)
            
            # Cancelar TTS previo si existe
            if self._current_tts_task and not self._current_tts_task.done():
                self._current_tts_task.cancel()
                self.tts.stop() # Asegurar parada inmediata del proceso

            # Iniciamos la síntesis y reproducción en SEGUNDO PLANO
            # Así la interfaz visual puede mostrar el texto de inmediato
            self._current_tts_task = asyncio.create_task(self.tts.speak(clean_text, sink_id=sink_id))

        return {
            "status": "success",
            "response_text": response_text,
            "commands_executed": 1 if "Éxito" in response_text else 0,
            "audio_file": None, # Ya no esperamos por el path
        }
