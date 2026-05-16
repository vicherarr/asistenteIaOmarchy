"""Módulo de síntesis de voz (TTS).

Usa Kokoro como motor principal (local, español, alta calidad).
Fallback a gTTS (requiere internet) si Kokoro no está disponible.
"""

import asyncio
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

KOKORO_SAMPLE_RATE = 24000
KOKORO_LANG_CODE = "e"  # Spanish
KOKORO_VOICE = "em_alex"


class TTSError(Exception):
    """Errores del motor TTS."""
    pass


class TTSEngine:
    """Motor de texto a voz. Prioriza Kokoro, fallback a gTTS."""

    def __init__(self) -> None:
        self._kokoro_pipeline = None
        self._default_sink: Optional[str] = None
        self._init_kokoro()

    def _init_kokoro(self) -> None:
        """Intenta inicializar Kokoro TTS."""
        try:
            from kokoro import KPipeline
            self._kokoro_pipeline = KPipeline(lang_code=KOKORO_LANG_CODE)
            logger.info("Kokoro TTS inicializado correctamente")
        except ImportError:
            logger.warning("Kokoro no instalado. Se usará gTTS como fallback.")
        except Exception as e:
            logger.warning(f"Error inicializando Kokoro: {e}. Se usará gTTS como fallback.")

    def _get_default_bluetooth_sink(self) -> Optional[str]:
        """Detecta el sink Bluetooth por defecto."""
        try:
            result = subprocess.run(
                ["wpctl", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            in_sinks = False
            for line in result.stdout.splitlines():
                if "Sinks:" in line:
                    in_sinks = True
                    continue
                if in_sinks and ("Sources:" in line or "Filters:" in line):
                    break
                if in_sinks and "bluetooth" in line.lower():
                    cleaned = line.replace("│", "").replace("├", "").replace("─", "").strip()
                    parts = cleaned.split()
                    for part in parts:
                        node_id = part.replace(".", "").replace("*", "")
                        if node_id.isdigit():
                            return node_id
        except Exception as e:
            logger.warning(f"Error detectando sink BT: {e}")

        return None

    def _set_audio_sink(self) -> None:
        sink_id = self._get_default_bluetooth_sink()
        if sink_id:
            self._default_sink = sink_id
            logger.info(f"TTS usará sink Bluetooth: {sink_id}")

    def speak(self, text: str) -> Optional[str]:
        """Sintetiza texto a voz y lo reproduce."""
        if not text.strip():
            logger.warning("Texto vacío para TTS")
            return None

        self._set_audio_sink()

        if self._kokoro_pipeline is not None:
            return self._speak_kokoro(text)

        return self._speak_gtts(text)

    def _speak_kokoro(self, text: str) -> Optional[str]:
        """Sintetiza usando Kokoro TTS (local, alta calidad)."""
        try:
            import soundfile as sf

            audio_chunks = []
            generator = self._kokoro_pipeline(text, voice=KOKORO_VOICE, speed=1.0)

            for _, _, audio in generator:
                audio_chunks.append(audio)

            if not audio_chunks:
                raise TTSError("Kokoro no generó audio")

            import numpy as np
            full_audio = np.concatenate(audio_chunks)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                wav_path = tmp_file.name

            sf.write(wav_path, full_audio, KOKORO_SAMPLE_RATE)

            if not Path(wav_path).exists() or Path(wav_path).stat().st_size == 0:
                raise TTSError("Kokoro generó archivo vacío")

            logger.info(f"Kokoro generó audio: {Path(wav_path).stat().st_size} bytes ({len(full_audio)/KOKORO_SAMPLE_RATE:.1f}s)")
            self._play_audio(wav_path)
            return wav_path

        except ImportError:
            logger.warning("soundfile no disponible, intentando gTTS")
            return self._speak_gtts(text)
        except Exception as e:
            logger.warning(f"Kokoro falló: {e}, intentando gTTS")
            return self._speak_gtts(text)

    def _speak_gtts(self, text: str) -> Optional[str]:
        """Sintetiza usando gTTS (Google TTS API, requiere internet)."""
        try:
            from gtts import gTTS
        except ImportError:
            logger.error("gTTS tampoco disponible")
            raise TTSError("No hay motor TTS disponible")

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
            mp3_path = tmp_file.name

        try:
            tts = gTTS(text=text, lang="es", slow=False)
            tts.save(mp3_path)

            if not Path(mp3_path).exists() or Path(mp3_path).stat().st_size == 0:
                raise TTSError("gTTS generó archivo vacío")

            logger.info(f"gTTS generó audio: {Path(mp3_path).stat().st_size} bytes")
            self._play_audio(mp3_path)
            return mp3_path

        except Exception as e:
            raise TTSError(f"Error en gTTS: {e}")

    def _play_audio(self, audio_path: str, speed: float = 1.0) -> None:
        """Reproduce audio al sink Bluetooth."""
        ext = Path(audio_path).suffix.lower()

        try:
            if self._default_sink:
                if speed != 1.0:
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                        sped_path = tmp.name
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", audio_path, "-af", f"atempo={speed}", sped_path],
                        capture_output=True, timeout=30,
                    )
                    audio_path = sped_path

                subprocess.run(
                    ["paplay", "--device", self._default_sink, audio_path],
                    capture_output=True, timeout=60,
                )
            else:
                if ext == ".mp3":
                    cmd = ["ffplay", "-nodisp", "-autoexit", "-af", f"atempo={speed}", audio_path]
                else:
                    cmd = ["aplay", audio_path]

                result = subprocess.run(cmd, capture_output=True, timeout=60)
                if result.returncode != 0:
                    logger.warning(f"Error reproduciendo: {result.stderr.decode()}")

        except FileNotFoundError:
            logger.warning(f"Reproductor no encontrado para {ext}")
        except Exception as e:
            logger.warning(f"Fallo en reproducción: {e}")

    def speak_async(self, text: str) -> asyncio.Task:
        return asyncio.create_task(self._speak_async(text))

    async def _speak_async(self, text: str) -> Optional[str]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.speak, text)

    def cleanup_temp_files(self, max_age_seconds: int = 3600) -> int:
        cleaned = 0
        tmp_dir = Path(tempfile.gettempdir())

        for pattern in ["*.mp3", "*.wav"]:
            for f in tmp_dir.glob(pattern):
                if f.name.startswith("tmp"):
                    age = time.time() - f.stat().st_mtime
                    if age > max_age_seconds:
                        try:
                            f.unlink()
                            cleaned += 1
                        except OSError:
                            pass

        return cleaned