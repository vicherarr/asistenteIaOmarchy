import asyncio
import logging
import subprocess
from typing import Optional, Callable
import numpy as np

logger = logging.getLogger(__name__)


class WakeWordListener:
    """Escucha de forma continua el micrófono en segundo plano buscando la palabra de activación (Wake Word)."""

    def __init__(self, on_wake_word_detected: Callable, app_state, model_name: str = "hey_jarvis", threshold: float = 0.5) -> None:
        self.on_wake_word_detected = on_wake_word_detected
        self.app_state = app_state
        self.model_name = model_name
        self.threshold = threshold
        self.task: Optional[asyncio.Task] = None
        self.is_running = False
        self.oww_model = None

    def start(self) -> None:
        """Inicia el bucle de escucha en segundo plano."""
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self._listen_loop())
        logger.info("WakeWordListener: Hilo de escucha en segundo plano registrado.")

    def stop(self) -> None:
        """Detiene el bucle de escucha."""
        self.is_running = False
        if self.task:
            self.task.cancel()
            self.task = None
        logger.info("WakeWordListener: Hilo de escucha detenido.")

    async def _listen_loop(self) -> None:
        """Bucle principal de captura y análisis de audio para Wake Word."""
        try:
            # Importación diferida para acelerar el inicio de la app
            from openwakeword.model import Model
            self.oww_model = Model(wakeword_models=[self.model_name])
            logger.info(f"WakeWordListener: Modelo '{self.model_name}' cargado con éxito en memoria.")
        except Exception as e:
            logger.error(f"WakeWordListener: No se pudo inicializar openwakeword: {e}")
            self.is_running = False
            return

        # openwakeword procesa trozos de 1280 muestras (80ms a 16000Hz 16-bit)
        chunk_samples = 1280
        frame_bytes = chunk_samples * 2  # 2 bytes por muestra (s16le)
        
        while self.is_running:
            # Si el asistente ya está grabando o procesando, suspendemos la captura
            # Esto libera ciclos de CPU y evita la auto-activación con el TTS
            if self.app_state.is_recording or self.app_state.processing:
                await asyncio.sleep(0.5)
                continue

            # Iniciar captura de micrófono a 16kHz PCM Mono
            source_id = self.app_state.audio_manager.default_source
            device = source_id if source_id else "@DEFAULT_SOURCE@"
            pacat_cmd = ["pacat", "--record", "--rate=16000", "--channels=1", "--format=s16le", "--device", device]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *pacat_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                logger.debug("WakeWordListener: Stream de captura pacat iniciado.")
            except Exception as e:
                logger.error(f"WakeWordListener: Error lanzando pacat: {e}")
                await asyncio.sleep(5)  # Reintentar en 5s
                continue

            try:
                while self.is_running:
                    # Detener captura si el estado del asistente pasa a grabación/procesamiento activo
                    if self.app_state.is_recording or self.app_state.processing:
                        break

                    try:
                        data = await proc.stdout.readexactly(frame_bytes)
                    except asyncio.IncompleteReadError as e:
                        if len(e.partial) > 0:
                            data = e.partial
                        else:
                            break
                    except Exception:
                        break

                    if len(data) < frame_bytes:
                        break

                    # Convertir bytes crudos a numpy array s16 y aplicar amplificación digital (8x)
                    # Esto compensa los filtros silenciosos del sistema sin distorsionar el reconocimiento
                    audio_float = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                    boosted = np.clip(audio_float * 8.0, -32768, 32767).astype(np.int16)

                    # Realizar inferencia con openwakeword
                    prediction = self.oww_model.predict(boosted)
                    score = prediction.get(self.model_name, 0.0)

                    if score >= self.threshold:
                        logger.info(f"WakeWordListener: Palabra clave '{self.model_name}' detectada con probabilidad {score:.2f}!")
                        
                        # 1. Parar de inmediato el proceso pacat para liberar el hardware de audio
                        if proc.returncode is None:
                            proc.terminate()
                            await proc.wait()
                        
                        # 2. Invocar callback para iniciar la conversación
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(self.on_wake_word_detected())
                        except Exception as e:
                            logger.error(f"WakeWordListener: Error ejecutando callback: {e}")
                        break

                    # Pequeña tregua para el planificador
                    await asyncio.sleep(0.005)

            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"WakeWordListener: Error en bucle de análisis: {e}")
            finally:
                # Asegurar cierre de pacat
                if proc.returncode is None:
                    try:
                        proc.terminate()
                        await proc.wait()
                    except:
                        pass
                logger.debug("WakeWordListener: Captura pacat detenida.")
                await asyncio.sleep(0.1)
