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

import numpy as np

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
        self._is_playing = False
        self._active_stream = None
        self._persistent_stream = None  # OutputStream reutilizado para pipeline
        self._init_kokoro()

    def _init_kokoro(self) -> None:
        """Intenta inicializar Kokoro TTS."""
        try:
            from kokoro import KPipeline
            # Forzamos CPU para evitar colisión de VRAM y contextos CUDA con LiteRT (Dawn/WebGPU)
            device = "cpu"
            self._kokoro_pipeline = KPipeline(lang_code=settings.KOKORO_LANG, device=device)
            logger.info("Kokoro TTS inicializado correctamente en CPU (Optimizado para liberar GPU)")
        except ImportError:
            logger.warning("Kokoro no instalado. Se usará gTTS como fallback.")
        except Exception as e:
            logger.warning(f"Error inicializando Kokoro: {e}. Se usará gTTS como fallback.")

    def _resolve_voice(self) -> str:
        """Resuelve la ruta de la voz local si existe."""
        voice_val = settings.KOKORO_VOICE
        if not voice_val.endswith('.pt'):
            local_path = settings.PROJECT_ROOT / "models" / f"{voice_val}.pt"
            if local_path.exists():
                return str(local_path)
        return voice_val

    def _tensor_to_numpy(self, audio) -> np.ndarray:
        """Convierte un tensor de audio a numpy array."""
        if hasattr(audio, "cpu"):
            return audio.cpu().numpy()
        elif hasattr(audio, "numpy"):
            return audio.numpy()
        else:
            return np.array(audio)

    async def synthesize_only(self, text: str) -> Optional[np.ndarray]:
        """
        Genera audio como np.ndarray sin reproducir ni guardar.
        Diseñado para el pipeline de doble cola (síntesis || reproducción).
        """
        if not text.strip() or self._kokoro_pipeline is None:
            return None

        def _synthesize():
            voice = self._resolve_voice()
            audio_chunks = []
            generator = self._kokoro_pipeline(text, voice=voice, speed=1.0)
            for _, _, audio in generator:
                if not self._is_playing:
                    break
                if audio is not None and len(audio) > 0:
                    audio_chunks.append(self._tensor_to_numpy(audio))
            return np.concatenate(audio_chunks) if audio_chunks else None

        try:
            return await asyncio.to_thread(_synthesize)
        except Exception as e:
            logger.warning(f"Error en synthesize_only: {e}")
            return None

    async def play_audio_array(self, audio_np: np.ndarray) -> None:
        """
        Reproduce un array numpy usando un OutputStream persistente reutilizado.
        Elimina la latencia de abrir/cerrar stream por cada frase.
        """
        if audio_np is None or len(audio_np) == 0:
            return

        def _play():
            # Abrir stream persistente si no existe
            if self._persistent_stream is None:
                try:
                    import sounddevice as sd
                    self._persistent_stream = sd.OutputStream(
                        samplerate=KOKORO_SAMPLE_RATE,
                        channels=1,
                        dtype='float32'
                    )
                    self._persistent_stream.start()
                except Exception as e:
                    logger.error(f"No se pudo abrir OutputStream persistente: {e}")
                    return

            if self._persistent_stream and not self._persistent_stream.closed:
                try:
                    self._persistent_stream.write(audio_np.astype('float32'))
                except Exception as e:
                    logger.warning(f"Error escribiendo al stream persistente: {e}")
                    # Intentar recrear el stream
                    try:
                        self._persistent_stream.close()
                    except Exception:
                        pass
                    self._persistent_stream = None

        try:
            await asyncio.to_thread(_play)
        except Exception as e:
            logger.warning(f"Error en play_audio_array: {e}")

    def close_persistent_stream(self) -> None:
        """Cierra el OutputStream persistente al finalizar."""
        if self._persistent_stream:
            try:
                self._persistent_stream.stop()
                self._persistent_stream.close()
            except Exception:
                pass
            self._persistent_stream = None

    async def speak(self, text: str, sink_id: Optional[str] = None) -> Optional[str]:
        """Sintetiza texto a voz y lo reproduce de forma asíncrona."""
        if not text.strip():
            logger.warning("Texto vacío para TTS")
            return None

        # Si no se pasa sink_id, intentará reproducir al dispositivo por defecto de PipeWire
        self._is_playing = True
        
        if self._kokoro_pipeline is not None:
            return await self._speak_kokoro(text, sink_id)

        return await self._speak_gtts(text, sink_id)

    async def _speak_kokoro(self, text: str, sink_id: Optional[str]) -> Optional[str]:
        """Sintetiza usando Kokoro TTS con reproducción en streaming de latencia ultra baja."""
        def _stream_and_save():
            import sounddevice as sd
            import soundfile as sf

            voice = self._resolve_voice()
            audio_chunks = []
            generator = self._kokoro_pipeline(text, voice=voice, speed=1.0)
            
            # Usamos el dispositivo predeterminado de PipeWire (None), el cual cambia automáticamente
            # al dispositivo de salida activo (incluyendo auriculares bluetooth seleccionados)
            with sd.OutputStream(samplerate=KOKORO_SAMPLE_RATE, channels=1, dtype='float32') as stream:
                self._active_stream = stream
                for _, _, audio in generator:
                    if not self._is_playing:
                        logger.info("Generación de audio Kokoro cancelada por interrupción")
                        break
                    if audio is not None and len(audio) > 0:
                        audio_np = self._tensor_to_numpy(audio)
                        audio_chunks.append(audio_np)
                        # Reproducir chunk de audio en tiempo real
                        stream.write(audio_np.astype('float32'))
                self._active_stream = None
            
            if not audio_chunks:
                return None
                
            # Guardamos el wav completo por compatibilidad y registro histórico
            full_audio = np.concatenate(audio_chunks)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=settings.TEMP_DIR) as tmp_file:
                wav_path = tmp_file.name
            sf.write(wav_path, full_audio, KOKORO_SAMPLE_RATE)
            return wav_path

        try:
            wav_path = await asyncio.to_thread(_stream_and_save)
            if not wav_path or not Path(wav_path).exists():
                raise TTSError("Kokoro falló al generar o reproducir audio")

            logger.info(f"Kokoro finalizó streaming y guardó: {Path(wav_path).stat().st_size} bytes")
            return wav_path

        except Exception as e:
            if not self._is_playing:
                logger.info("Reproducción de Kokoro fue cancelada/interrumpida por el usuario. No se aplica fallback a gTTS.")
                return None
            logger.warning(f"Kokoro streaming falló: {e}, intentando gTTS")
            return await self._speak_gtts(text, sink_id)
        finally:
            self._is_playing = False

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
            if not self._is_playing:
                logger.info("gTTS abortado antes de la generación por cancelación")
                return None
            mp3_path = await asyncio.to_thread(_generate)
            if not self._is_playing:
                logger.info("gTTS abortado antes de la reproducción por cancelación")
                return None
            logger.info(f"gTTS generó audio: {Path(mp3_path).stat().st_size} bytes")
            await self._play_audio(mp3_path, sink_id)
            return mp3_path
        except Exception as e:
            logger.error(f"Fallo total en TTS: {e}")
            return None
        finally:
            self._is_playing = False

    async def _play_audio(self, audio_path: str, sink_id: Optional[str] = None, speed: float = 1.0) -> None:
        """Reproduce audio usando paplay de forma asíncrona."""
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

            # 2. Transcodificar MP3 a WAV si usamos paplay (ya que paplay no soporta MP3 directamente)
            if ext == ".mp3":
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=settings.TEMP_DIR) as tmp:
                    wav_path = tmp.name
                
                ffmpeg = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", audio_path, wav_path,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                await ffmpeg.wait()
                audio_path = wav_path

            # 3. Selección de comando de reproducción
            if sink_id:
                # Si hay un sink Bluetooth específico
                cmd = ["paplay", "--device", sink_id, audio_path]
            else:
                # Por defecto a PipeWire/Pulse
                cmd = ["paplay", audio_path]

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
        self._is_playing = False
        
        # Parar stream activo de Kokoro
        if self._active_stream:
            try:
                self._active_stream.stop()
                self._active_stream.close()
            except Exception:
                pass
            self._active_stream = None
        
        # Parar stream persistente del pipeline
        if self._persistent_stream:
            try:
                self._persistent_stream.stop()
                self._persistent_stream.close()
            except Exception:
                pass
            self._persistent_stream = None
        
        try:
            import sounddevice as sd
            sd.stop()
        except Exception as e:
            logger.warning(f"No se pudo detener el dispositivo de audio: {e}")
            
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
