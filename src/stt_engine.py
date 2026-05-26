"""Módulo para la transcripción de voz a texto (STT)."""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

# Ubicación del modelo por defecto (configurable vía STT_MODEL_PATH)
DEFAULT_MODEL_PATH = Path(settings.STT_MODEL_PATH)


class STTEngine:
    """Motor de Speech-To-Text que utiliza whisper-cli de forma asíncrona."""

    def __init__(self, model_path: Optional[Path] = None) -> None:
        self.model_path = model_path or DEFAULT_MODEL_PATH
        if not self.model_path.exists():
            logger.warning(f"Modelo de Whisper no encontrado en {self.model_path}")

    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe un archivo de audio a texto, con limpieza previa."""
        if not self.model_path.exists():
            return "[Error: Modelo Whisper no encontrado]"

        logger.info(f"Procesando audio para STT: {audio_path}")

        try:
            # 1. Normalización suave con FFmpeg (configuración óptima demostrada en lab)
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

            # 2. Transcripción con whisper-cli (Beam Size 5: el cambio clave)
            process = await asyncio.create_subprocess_exec(
                "whisper-cli",
                "--model", str(self.model_path),
                "--file", str(audio_to_transcribe),
                "--language", settings.STT_LANGUAGE,
                "--no-timestamps",
                "--beam-size", "5",
                "--threads", str(settings.STT_THREADS),
                "--prompt", settings.STT_PROMPT,
                "--no-gpu",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            # Limpiar archivo temporal normalizado
            if audio_to_transcribe != audio_path:
                audio_to_transcribe.unlink(missing_ok=True)

            if process.returncode != 0:
                error_msg = stderr.decode().strip()
                logger.error(f"Error en whisper-cli: {error_msg}")
                return ""

            # El output de whisper-cli suele tener líneas de log y la transcripción
            # La transcripción suele ser la línea que empieza por un espacio y no tiene ':'
            output = stdout.decode().strip()
            lines = output.splitlines()

            transcription = ""
            for line in lines:
                # Filtrar líneas de metadatos (que suelen tener formato [00:00:00.000 -> 00:00:00.000])
                # o líneas de inicialización del modelo.
                # Con --no-timestamps, whisper-cli suele imprimir la línea limpia con un indentado.
                clean_line = line.strip()
                if clean_line and not clean_line.startswith("[") and not ":" in line[:10]:
                    transcription = clean_line
                    break

            # Fallback: si no detectamos la línea limpia, tomamos la última línea no vacía que no parezca un log
            if not transcription and lines:
                for line in reversed(lines):
                    if line.strip() and not "whisper" in line.lower():
                        transcription = line.strip()
                        break

            logger.info(f"Transcripción obtenida: '{transcription}'")
            return transcription

        except FileNotFoundError:
            logger.error("whisper-cli no encontrado. Instalar whisper.cpp.")
            return "[Error: whisper-cli no instalado]"
        except Exception as e:
            logger.error(f"Error inesperado en STT: {e}")
            return f"[Error STT: {e}]"
        finally:
            # Limpiar el archivo de audio después de procesarlo
            try:
                audio_path.unlink(missing_ok=True)
            except Exception:
                pass
