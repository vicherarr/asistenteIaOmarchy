"""Módulo de síntesis de voz (TTS).

Usa Kokoro como motor principal (local, español, alta calidad).
Fallback a gTTS (requiere internet) si Kokoro no está disponible.
"""

import asyncio
import logging
import subprocess
import tempfile
import threading
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

    # Frecuencia de los arrays que devuelve `synthesize_only`. Expuesta como
    # atributo de clase para que los consumidores (p.ej. el remuestreo hacia el
    # dispositivo satélite) no tengan que importar la constante del módulo ni,
    # peor, suponerla.
    SAMPLE_RATE = KOKORO_SAMPLE_RATE

    # Los OutputStream de PortAudio NO son thread-safe, y aquí se tocan desde dos hilos:
    # las escrituras van en un worker (asyncio.to_thread) y `stop()` llega desde el hilo
    # del bucle de eventos cuando el usuario interrumpe. Cerrar un stream mientras otro
    # hilo está dentro de write() libera las estructuras que el bucle de ALSA sigue
    # usando: SIGSEGV en snd_pcm_poll_descriptors_revents. Pasó en producción el
    # 23/08/2026 — el proceso murió 4 s después de un /cancel y systemd lo reinició.
    #
    # `_stream_lock` serializa abrir/escribir/cerrar. `_stop_requested` es la señal de
    # corte: `stop()` la levanta SIN pedir el lock, para no quedarse esperando, y el que
    # escribe la mira entre bloque y bloque. Por eso se escribe troceado (ver
    # _BLOQUE_ESCRITURA): con la escritura entera de una frase, el lock quedaba retenido
    # segundos y `stop()` bloquearía el bucle de eventos.
    _BLOQUE_ESCRITURA = 2048          # ~85 ms a 24 kHz: corte fino sin trocear de más
    # Cuánto espera `stop()` al que escribe. Se queda corto a propósito: `stop()` corre
    # en el hilo del bucle de eventos, así que esta espera lo BLOQUEA. Lo normal es que
    # el que escribe salga en ~85 ms (un bloque), así que 1 s ya es diez veces el peor
    # caso esperado; agotarlo solo significa dejar un stream abierto, que se reutiliza o
    # se cierra luego. Barato, comparado con congelar el asistente.
    _ESPERA_CIERRE = 1.0

    def __init__(self) -> None:
        self._kokoro_pipeline = None
        self._playback_process: Optional[asyncio.subprocess.Process] = None
        self._is_playing = False
        self._active_stream = None
        self._persistent_stream = None  # OutputStream reutilizado para pipeline
        self._stream_lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._init_kokoro()

    # ---- acceso serializado a los streams de PortAudio ----
    def _escribir_troceado(self, stream, audio_np: np.ndarray) -> bool:
        """Escribe en bloques, mirando la señal de corte entre uno y otro.

        Devuelve False si se cortó a medias. El troceado NO es por rendimiento: es lo
        que acota cuánto puede tardar `stop()` en hacerse con el lock. Debe llamarse ya
        con `_stream_lock` cogido.
        """
        datos = audio_np.astype('float32')
        for i in range(0, len(datos), self._BLOQUE_ESCRITURA):
            if self._stop_requested.is_set():
                return False
            stream.write(datos[i:i + self._BLOQUE_ESCRITURA])
        return True

    def _cerrar(self, stream) -> None:
        """Cierra un OutputStream una sola vez. Debe llamarse con `_stream_lock` cogido.

        El guard de `closed` importa: antes, `stop()` cerraba el stream de Kokoro y el
        bloque `with sd.OutputStream(...)` del worker lo volvía a cerrar al salir. Dos
        Pa_CloseStream sobre el mismo handle, el segundo ya liberado.
        """
        if stream is None:
            return
        try:
            if not stream.closed:
                stream.stop()
                stream.close()
        except Exception as e:  # noqa: BLE001 — cerrar nunca debe tumbar al que llama
            logger.debug(f"Error cerrando OutputStream (se ignora): {e}")

    def _cerrar_todos(self) -> None:
        """Cierra los dos streams. Debe llamarse con `_stream_lock` cogido."""
        self._cerrar(self._active_stream)
        self._active_stream = None
        self._cerrar(self._persistent_stream)
        self._persistent_stream = None

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
            # Todo el ciclo abrir/escribir/cerrar va bajo el lock: `stop()` no puede
            # cerrarlo por debajo mientras se escribe (era el segfault). Ver la nota de
            # _stream_lock en la cabecera de la clase.
            with self._stream_lock:
                if self._stop_requested.is_set():
                    return
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
                        self._escribir_troceado(self._persistent_stream, audio_np)
                    except Exception as e:
                        logger.warning(f"Error escribiendo al stream persistente: {e}")
                        # Intentar recrear el stream en la siguiente frase
                        self._cerrar(self._persistent_stream)
                        self._persistent_stream = None

        try:
            await asyncio.to_thread(_play)
        except Exception as e:
            logger.warning(f"Error en play_audio_array: {e}")

    def close_persistent_stream(self) -> None:
        """Cierra el OutputStream persistente al finalizar."""
        with self._stream_lock:
            self._cerrar(self._persistent_stream)
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
            #
            # Sin `with` a propósito: ese bloque cerraba el stream al salir, y si `stop()`
            # ya lo había cerrado desde el hilo del bucle de eventos eran dos
            # Pa_CloseStream sobre el mismo handle. El cierre va ahora por `_cerrar`, que
            # mira `closed`, y todo el ciclo bajo `_stream_lock`.
            with self._stream_lock:
                if self._stop_requested.is_set():
                    return None
                stream = sd.OutputStream(
                    samplerate=KOKORO_SAMPLE_RATE, channels=1, dtype='float32')
                stream.start()
                self._active_stream = stream
                try:
                    for _, _, audio in generator:
                        if not self._is_playing or self._stop_requested.is_set():
                            logger.info("Generación de audio Kokoro cancelada por interrupción")
                            break
                        if audio is not None and len(audio) > 0:
                            audio_np = self._tensor_to_numpy(audio)
                            audio_chunks.append(audio_np)
                            # Reproducir chunk de audio en tiempo real
                            if not self._escribir_troceado(stream, audio_np):
                                break
                finally:
                    self._cerrar(stream)
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
        """Detiene la reproducción en curso.

        Llega desde el hilo del bucle de eventos (POST /cancel, una petición nueva),
        mientras un worker puede estar dentro de write(). El orden importa:

        1. Levantar `_stop_requested` SIN pedir el lock. El que escribe la mira entre
           bloques y sale en ~85 ms, así que esto corta el audio ya, no al final.
        2. Coger el lock y cerrar. Como el que escribía ya salió, nadie está dentro de
           PortAudio cuando se libera el stream: es lo que evita el SIGSEGV.

        Si el lock no llega en `_ESPERA_CIERRE` no se fuerza el cierre. Un stream que
        sigue abierto es un recurso colgando; cerrarlo por debajo de quien lo usa es un
        core dump y se lleva el proceso entero. Se reutiliza o se cierra más tarde.
        """
        self._is_playing = False
        self._stop_requested.set()

        if self._stream_lock.acquire(timeout=self._ESPERA_CIERRE):
            try:
                self._cerrar_todos()
            finally:
                self._stream_lock.release()
        else:
            logger.warning(
                "TTS: no se pudo cerrar el audio en %.1f s (alguien sigue escribiendo). "
                "Se deja abierto a propósito: cerrarlo ahora sería un segfault.",
                self._ESPERA_CIERRE,
            )

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

    def rearm(self) -> None:
        """Deja el motor listo para un turno nuevo después de un `stop()`.

        `_stop_requested` es pegajosa a propósito —una vez levantada, todo lo que quede
        en la cola de audio del turno viejo se descarta en vez de sonar a destiempo—,
        así que hay que bajarla explícitamente al empezar el siguiente. Sin esto, el
        primer stop() dejaría el TTS mudo para siempre.
        """
        self._stop_requested.clear()
        self._is_playing = True

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
