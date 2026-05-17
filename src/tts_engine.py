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
from typing import Optional, Any

from src.config import settings

logger = logging.getLogger(__name__)

KOKORO_SAMPLE_RATE = 24000


class TTSError(Exception):
    """Errores del motor TTS."""
    pass


class TTSEngine:
    """Motor de texto a voz. Prioriza Kokoro, fallback a gTTS."""

    def __init__(self) -> None:
        self._kokoro_pipeline = None
        self._playback_process: Optional[asyncio.subprocess.Process] = None
        self._init_kokoro()

    def _init_kokoro(self) -> None:
        """Intenta inicializar Kokoro TTS."""
        try:
            from kokoro import KPipeline
            self._kokoro_pipeline = KPipeline(lang_code=settings.KOKORO_LANG)
            logger.info("Kokoro TTS inicializado correctamente")
        except ImportError:
            logger.warning("Kokoro no instalado. Se usará gTTS como fallback.")
        except Exception as e:
            logger.warning(f"Error inicializando Kokoro: {e}. Se usará gTTS como fallback.")

    async def speak(self, text: str, sink_id: Optional[str] = None) -> Optional[str]:
        """Sintetiza texto a voz y lo reproduce de forma asíncrona."""
        if not text.strip():
            logger.warning("Texto vacío para TTS")
            return None

        # Si no se pasa sink_id, intentará reproducir al dispositivo por defecto de PipeWire
        
        if self._kokoro_pipeline is not None:
            return await self._speak_kokoro(text, sink_id)

        return await self._speak_gtts(text, sink_id)

    async def _speak_kokoro(self, text: str, sink_id: Optional[str]) -> Optional[str]:
        """Sintetiza usando Kokoro TTS (operación CPU-intensiva en hilo)."""
        def _generate():
            import soundfile as sf
            import numpy as np

            audio_chunks = []
            generator = self._kokoro_pipeline(text, voice=settings.KOKORO_VOICE, speed=1.0)

            for _, _, audio in generator:
                audio_chunks.append(audio)

            if not audio_chunks:
                return None

            full_audio = np.concatenate(audio_chunks)
            
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=settings.TEMP_DIR) as tmp_file:
                wav_path = tmp_file.name

            sf.write(wav_path, full_audio, KOKORO_SAMPLE_RATE)
            return wav_path

        try:
            wav_path = await asyncio.to_thread(_generate)
            if not wav_path or not Path(wav_path).exists():
                raise TTSError("Kokoro falló al generar audio")

            logger.info(f"Kokoro generó audio: {Path(wav_path).stat().st_size} bytes")
            await self._play_audio(wav_path, sink_id)
            return wav_path

        except Exception as e:
            logger.warning(f"Kokoro falló: {e}, intentando gTTS")
            return await self._speak_gtts(text, sink_id)

    async def _speak_gtts(self, text: str, sink_id: Optional[str]) -> Optional[str]:
        """Sintetiza usando gTTS (requiere internet)."""
        def _generate():
            from gtts import gTTS
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=settings.TEMP_DIR) as tmp_file:
                mp3_path = tmp_file.name
            
            tts = gTTS(text=text, lang="es", slow=False)
            tts.save(mp3_path)
            return mp3_path

        try:
            mp3_path = await asyncio.to_thread(_generate)
            logger.info(f"gTTS generó audio: {Path(mp3_path).stat().st_size} bytes")
            await self._play_audio(mp3_path, sink_id)
            return mp3_path
        except Exception as e:
            logger.error(f"Fallo total en TTS: {e}")
            return None

    async def _play_audio(self, audio_path: str, sink_id: Optional[str] = None, speed: float = 1.0) -> None:
        """Reproduce audio usando paplay o ffplay de forma asíncrona."""
        ext = Path(audio_path).suffix.lower()
        
        try:
            # 1. Ajuste de velocidad si es necesario
            if speed != 1.0:
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=settings.TEMP_DIR) as tmp:
                    sped_path = tmp.name
                
                ffmpeg = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", audio_path, "-af", f"atempo={speed}", sped_path,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                await ffmpeg.wait()
                audio_path = sped_path

            # 2. Selección de comando de reproducción
            if sink_id:
                # Si hay un sink Bluetooth específico
                cmd = ["paplay", "--device", sink_id, audio_path]
            else:
                # Por defecto a PipeWire
                if ext == ".mp3":
                    cmd = ["ffplay", "-nodisp", "-autoexit", "-af", f"atempo={speed}", audio_path]
                else:
                    cmd = ["paplay", audio_path] # paplay es preferido en PipeWire/Pulse

            logger.info(f"Reproduciendo TTS: {' '.join(cmd)}")
            self._playback_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            # Esperar a que termine la reproducción o se cancele
            await self._playback_process.wait()
            self._playback_process = None

        except asyncio.CancelledError:
            if self._playback_process:
                try:
                    self._playback_process.terminate()
                except ProcessLookupError:
                    pass
            raise
        except Exception as e:
            logger.warning(f"Fallo en reproducción TTS: {e}")

    def stop(self) -> None:
        """Detiene la reproducción en curso."""
        if self._playback_process and self._playback_process.returncode is None:
            try:
                self._playback_process.terminate()
                logger.info("Reproducción TTS detenida manualmente")
            except Exception:
                pass

    async def speak_async(self, text: str, sink_id: Optional[str] = None) -> Optional[str]:
        """Mantenemos por compatibilidad con AssistantService pero ahora es nativamente async."""
        return await self.speak(text, sink_id)

    def cleanup_temp_files(self, max_age_seconds: int = 3600) -> int:
        cleaned = 0
        tmp_dir = settings.TEMP_DIR

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
