"""Servicio principal del asistente, encapsula la lógica de negocio."""

import asyncio
import logging
import re
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
    open_terminal_and_run_command,
    read_terminal_screen,
    control_local_browser,
    send_input_to_terminal,
    interrupt_terminal_command
)
from src.litert_client import LiteRTClient
from src.tts_engine import TTSEngine
from src.stt_engine import STTEngine
from src.utils import strip_markdown
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
        self._current_play_task: Optional[asyncio.Task] = None
        # Lista de herramientas para LiteRT
        self.tools = [
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
            open_terminal_and_run_command,
            read_terminal_screen,
            control_local_browser,
            send_input_to_terminal,
            interrupt_terminal_command
        ]

    def _extract_sentences(self, text_buffer: str) -> tuple[List[str], str]:
        """
        Extrae frases completas del buffer basadas en signos de puntuación (.!?:\n)
        seguidos de un espacio o fin de cadena, protegiendo decimales y abreviaciones comunes.
        También corta por coma cuando el buffer supera ~80 caracteres, pero SOLO si
        el fragmento resultante parece texto natural (no código/JSON).
        """
        # Si el buffer es largo, también cortar por coma para frases más cortas
        # pero SOLO para texto natural, no para código
        if len(text_buffer) > 80:
            pattern = re.compile(r'([.!?:])(?=\s|$)|(\n)')
        else:
            pattern = re.compile(r'([.!?:])(?=\s|$)|(\n)')
        
        sentences = []
        start = 0
        for match in pattern.finditer(text_buffer):
            end = match.end()
            sentence = text_buffer[start:end].strip()
            if sentence:
                sentences.append(sentence)
            start = end
            
        remaining = text_buffer[start:]
        return sentences, remaining

    async def _synth_worker(self, queue_text: asyncio.Queue, queue_audio: asyncio.Queue) -> None:
        """
        Consumidor de frases de texto: sintetiza cada frase a numpy array
        y lo encola para reproducción. Permite solapamiento con _play_worker.
        """
        logger.info("Worker de síntesis TTS iniciado")
        try:
            while True:
                sentence = await queue_text.get()
                if sentence is None:
                    queue_text.task_done()
                    logger.info("Worker de síntesis recibió señal de finalización")
                    await queue_audio.put(None)
                    break
                
                logger.info(f"Sintetizando: '{sentence}'")
                try:
                    audio_np = await self.tts.synthesize_only(sentence)
                    if audio_np is not None:
                        await queue_audio.put(audio_np)
                except Exception as e:
                    logger.error(f"Error sintetizando '{sentence}': {e}")
                finally:
                    queue_text.task_done()
        except asyncio.CancelledError:
            logger.info("Worker de síntesis cancelado activamente")
            raise
        except Exception as e:
            logger.error(f"Error inesperado en worker de síntesis: {e}")

    async def _play_worker(self, queue_audio: asyncio.Queue) -> None:
        """
        Consumidor de audio: reproduce arrays numpy usando un OutputStream persistente.
        Se ejecuta en paralelo con _synth_worker para eliminar gaps entre frases.
        """
        logger.info("Worker de reproducción TTS iniciado")
        try:
            while True:
                audio_np = await queue_audio.get()
                if audio_np is None:
                    queue_audio.task_done()
                    logger.info("Worker de reproducción recibió señal de finalización")
                    break
                
                try:
                    await self.tts.play_audio_array(audio_np)
                except Exception as e:
                    logger.error(f"Error reproduciendo audio: {e}")
                finally:
                    queue_audio.task_done()
        except asyncio.CancelledError:
            logger.info("Worker de reproducción cancelado activamente")
            raise
        except Exception as e:
            logger.error(f"Error inesperado en worker de reproducción: {e}")
        finally:
            # Cerrar el stream persistente al terminar
            self.tts.close_persistent_stream()

    async def process_audio(
        self,
        audio_path: Path,
        conversation_history: list[ChatMessage],
        sink_id: Optional[str] = None,
        max_history: int = 10
    ) -> dict:
        """Transcribe el audio y procesa el texto resultante."""
        await self.send_notification_async("Procesando audio...")
        
        text = await self.stt.transcribe(audio_path)
        
        if not text or len(text.strip()) < 2:
            await self.send_notification_async("No se detectó voz o el mensaje es muy corto.")
            return {"status": "error", "message": "No se detectó voz"}

        await self.send_notification_async(f"Has dicho: {text}")
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

    async def send_notification_async(self, message: str, title: str = "AsistenteIA") -> None:
        """Envía una notificación de escritorio de forma asíncrona (no bloquea el loop)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", title, message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            # No esperamos a que termine, solo lo lanzamos y seguimos
            asyncio.create_task(proc.wait())

            if message == "Escuchando...":
                beep_sound = "/usr/share/sounds/freedesktop/stereo/message.oga"
                if not Path(beep_sound).exists():
                    beep_sound = "/usr/share/sounds/freedesktop/stereo/complete.oga"

                if Path(beep_sound).exists():
                    proc_sound = await asyncio.create_subprocess_exec(
                        "paplay", beep_sound,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    asyncio.create_task(proc_sound.wait())
        except Exception as e:
            logger.warning(f"No se pudo enviar notificación o bip: {e}")

    def _is_speakable(self, text: str) -> bool:
        """Filtra fragmentos que parecen código/JSON y no deben ir al TTS."""
        if not text or len(text) < 3:
            return False
        # Rechazar fragmentos que empiezan con caracteres de código
        if text[0] in ('[', '{', '(', '<', '`', '}', ']', ')', '>', '*', '#', '-', '|', '\\'):
            return False
        # Rechazar si tiene más símbolos que letras (código/JSON)
        alpha_count = sum(1 for c in text if c.isalpha())
        symbol_count = sum(1 for c in text if c in '[]{}()<>`\'"=;:,')
        if symbol_count > alpha_count:
            return False
        # Rechazar si es solo números y puntuación
        if not any(c.isalpha() for c in text):
            return False
        return True

    async def process_transcription_stream(
        self,
        text: str,
        conversation_history: list[ChatMessage],
        sink_id: Optional[str] = None,
        max_history: int = 10
    ):
        """
        Procesa el texto del usuario usando LiteRT y herramientas de forma reactiva,
        transmitiendo los chunks resultantes en tiempo real y sintetizando voz frase a frase
        con pipeline de doble cola (síntesis || reproducción en paralelo).
        """
        logger.info(f"Procesando transcripción con LiteRT (streaming): {text[:100]}...")

        # Cancelar TTS previo si existe
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
        if self._current_play_task and not self._current_play_task.done():
            self._current_play_task.cancel()
        self.tts.stop() # Asegurar parada inmediata del proceso
        self.tts._is_playing = True  # Re-activar para nueva síntesis

        # Pipeline de doble cola: texto → síntesis → audio → reproducción
        queue_text = asyncio.Queue()
        queue_audio = asyncio.Queue()
        
        # Arrancar ambos workers en paralelo
        synth_task = asyncio.create_task(self._synth_worker(queue_text, queue_audio))
        play_task = asyncio.create_task(self._play_worker(queue_audio))
        self._current_tts_task = synth_task
        self._current_play_task = play_task

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

        accumulated_text = ""
        sentence_buffer = ""

        try:
            # Primera llamada al modelo
            async for chunk in self.litert.chat_stream(
                prompt=text,
                tools=self.tools,
                system_prompt=system_prompt,
                history=conversation_history[:-1] # Pasamos el historial previo
            ):
                accumulated_text += chunk
                sentence_buffer += chunk
                
                # Extraer y encolar frases completas
                sentences, sentence_buffer = self._extract_sentences(sentence_buffer)
                for s in sentences:
                    clean = strip_markdown(s)
                    if self._is_speakable(clean):
                        await queue_text.put(clean)
                
                yield chunk

            # Encolar lo que quede en el buffer
            if sentence_buffer.strip():
                clean = strip_markdown(sentence_buffer)
                if self._is_speakable(clean):
                    await queue_text.put(clean)

            # Guardar respuesta final acumulada en el historial
            conversation_history.append(ChatMessage(role="assistant", content=accumulated_text))

        except asyncio.CancelledError:
            logger.info("process_transcription_stream cancelado. Deteniendo workers de TTS...")
            if self._current_tts_task and not self._current_tts_task.done():
                self._current_tts_task.cancel()
            if self._current_play_task and not self._current_play_task.done():
                self._current_play_task.cancel()
            self.tts.stop()
            raise
        except Exception as e:
            logger.error(f"Error en process_transcription_stream: {e}")
            self.tts.stop()
            raise
        finally:
            # Enviar señal de fin a workers pero NO esperarlos
            # El stream debe cerrarse inmediatamente para que la GUI actualice el texto
            await queue_text.put(None)
            # Los workers continúan en background reproduciendo audio pendiente

    async def process_transcription(
        self,
        text: str,
        conversation_history: list[ChatMessage],
        sink_id: Optional[str] = None,
        max_history: int = 10
    ) -> dict:
        """
        Versión síncrona para compatibilidad: acumula el stream y devuelve el resultado completo.
        """
        response_text = ""
        async for chunk in self.process_transcription_stream(
            text=text,
            conversation_history=conversation_history,
            sink_id=sink_id,
            max_history=max_history
        ):
            response_text += chunk

        return {
            "status": "success",
            "response_text": response_text,
            "commands_executed": 1 if "Éxito" in response_text else 0,
            "audio_file": None,
        }

    async def cancel_audio_tasks(self) -> None:
        """Cancela activamente las tareas de audio y síntesis en curso."""
        cancelled = False
        if self._current_tts_task and not self._current_tts_task.done():
            logger.info("Cancelando tarea de síntesis TTS en AssistantService...")
            self._current_tts_task.cancel()
            cancelled = True
            
        if self._current_play_task and not self._current_play_task.done():
            logger.info("Cancelando tarea de reproducción TTS en AssistantService...")
            self._current_play_task.cancel()
            cancelled = True
            
        # Esperar a que terminen si es posible
        if cancelled:
            try:
                tasks = [t for t in (self._current_tts_task, self._current_play_task) if t and not t.done()]
                if tasks:
                    await asyncio.wait(tasks, timeout=2.0)
            except Exception as e:
                logger.warning(f"Error esperando cancelación de tareas de audio: {e}")

    async def wait_for_tts_complete(self) -> None:
        """Espera hasta que la reproducción de voz del TTS actual haya terminado por completo."""
        if self._current_play_task and not self._current_play_task.done():
            try:
                await self._current_play_task
                logger.info("Espera de reproducción de voz completada.")
            except asyncio.CancelledError:
                logger.info("La espera de TTS completo fue cancelada.")
            except Exception as e:
                logger.error(f"Error esperando a que termine el TTS: {e}")

    async def cleanup(self) -> None:
        """Cierra de forma segura todos los recursos del servicio de negocio."""
        logger.info("Iniciando cleanup de AssistantService...")
        await self.cancel_audio_tasks()
        if self.tts:
            self.tts.stop()
            self.tts.close_persistent_stream()

