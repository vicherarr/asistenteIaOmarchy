"""Módulo para la transcripción de voz a texto (STT)."""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import whisper

from src.config import settings

logger = logging.getLogger(__name__)


class STTEngine:
    """Motor de Speech-To-Text que utiliza openai-whisper de forma asíncrona."""

    def __init__(self, model_name: str = "base") -> None:
        self.model_name = model_name
        self._model: Optional[whisper.Whisper] = None

    def _load_model(self) -> whisper.Whisper:
        if self._model is None:
            logger.info(f"Cargando modelo Whisper: {self.model_name} (CPU)")
            self._model = whisper.load_model(self.model_name, device="cpu")
        return self._model

    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe un archivo de audio a texto, con limpieza previa."""
        if not audio_path.exists():
            logger.error(f"Archivo de audio no encontrado: {audio_path}")
            return "[Error: Archivo de audio no encontrado]"

        logger.info(f"Procesando audio para STT: {audio_path}")

        try:
            # 1. Normalización suave con FFmpeg
            cleaned_path = audio_path.with_suffix(".cleaned.wav")
            
            ffmpeg_process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(audio_path),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar", "16000", "-ac", "1", str(cleaned_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _, ffmpeg_stderr = await ffmpeg_process.communicate()

            if ffmpeg_process.returncode == 0:
                audio_to_transcribe = cleaned_path
            else:
                audio_to_transcribe = audio_path

            # 2. Transcripción con openai-whisper (Python API)
            loop = asyncio.get_event_loop()
            model = self._load_model()
            result = await loop.run_in_executor(
                None,
                lambda: model.transcribe(
                    str(audio_to_transcribe),
                    language="es",
                    beam_size=5,
                )
            )

            # Limpiar archivo temporal normalizado
            if audio_to_transcribe != audio_path:
                audio_to_transcribe.unlink(missing_ok=True)

            transcription = result.get("text", "").strip()
            logger.info(f"Transcripción obtenida: '{transcription}'")
            return transcription

        except Exception as e:
            logger.error(f"Error en STT: {e}")
            return f"[Error STT: {e}]"
        finally:
            # Limpiar el archivo de audio después de procesarlo
            try:
                audio_path.unlink(missing_ok=True)
            except Exception:
                pass
