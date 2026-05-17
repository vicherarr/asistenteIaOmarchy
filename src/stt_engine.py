"""Módulo para la transcripción de voz a texto (STT)."""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

# Intentar encontrar el modelo en la ubicación por defecto de la instalación
DEFAULT_MODEL_PATH = Path.home() / ".cache" / "whisper" / "ggml-base.bin"


class STTEngine:
    """Motor de Speech-To-Text que utiliza whisper-cli de forma asíncrona."""

    def __init__(self, model_path: Optional[Path] = None) -> None:
        self.model_path = model_path or DEFAULT_MODEL_PATH
        if not self.model_path.exists():
            logger.warning(f"Modelo de Whisper no encontrado en {self.model_path}")

    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe un archivo de audio a texto."""
        if not self.model_path.exists():
            return "[Error: Modelo Whisper no encontrado]"

        logger.info(f"Transcribiendo audio: {audio_path}")

        try:
            # whisper-cli --model <model> --file <file> --language es --no-timestamps
            process = await asyncio.create_subprocess_exec(
                "whisper-cli",
                "--model", str(self.model_path),
                "--file", str(audio_path),
                "--language", "es",
                "--no-timestamps",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

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
            logger.error(f"Error inesperated en STT: {e}")
            return f"[Error STT: {e}]"
        finally:
            # Limpiar el archivo de audio después de procesarlo
            try:
                audio_path.unlink(missing_ok=True)
            except Exception:
                pass
