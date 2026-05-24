import asyncio
import logging
import subprocess
import tempfile
import time
import os
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class AudioRecorder:
    """Gestiona la captura de audio usando comandos del sistema con auto-stop por silencio (VAD)."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._current_file: Optional[Path] = None
        self._monitor_task: Optional[asyncio.Task] = None
        
        # Parámetros VAD
        self._sample_rate = 16000
        self._frame_duration_ms = 30  # ms
        self._silence_timeout = 1.5   # Segundos de silencio para cortar

    def start_recording(self, source_id: Optional[str] = None, on_silence_callback: Optional[Callable] = None) -> Path:
        """Inicia la grabación y un monitor VAD en segundo plano."""
        if self._process and self._process.poll() is None:
            logger.warning("Ya hay una grabación en curso")
            return self._current_file

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="asistente_rec_")
        os.close(fd)
        
        self._current_file = Path(path)
        logger.info(f"Iniciando grabación en: {self._current_file}")
        
        # 1. Iniciar grabación real con parecord
        cmd = ["parecord", "--rate=16000", "--channels=1", "--file-format=wav"]
        if source_id:
            cmd.extend(["--device", source_id])
        cmd.append(str(self._current_file))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            
            # 2. Iniciar monitor VAD usando pacat (para leer el stream en paralelo)
            if on_silence_callback:
                # Importante: usar loop.create_task para que corra en el loop de FastAPI
                self._monitor_task = asyncio.create_task(
                    self._monitor_vad(source_id, on_silence_callback)
                )
                logger.info("Monitor VAD iniciado.")
                
        except Exception as e:
            logger.error(f"Error iniciando grabación: {e}")
            raise

        return self._current_file

    async def _monitor_vad(self, source_id: Optional[str], callback: Callable):
        """Vigila el stream de audio usando webrtcvad para detectar el fin de la voz."""
        try:
            import webrtcvad
            vad = webrtcvad.Vad(2) # Agresividad media (0-3)
            logger.info("Librería webrtcvad cargada.")
        except ImportError:
            logger.warning("webrtcvad no instalado. Auto-stop desactivado.")
            return

        # Comando para leer audio crudo (PCM 16bit 16kHz Mono)
        device = source_id if source_id else "@DEFAULT_SOURCE@"
        # Aseguramos formato crudo para VAD
        pacat_cmd = ["pacat", "--record", "--rate=16000", "--channels=1", "--format=s16le", "--device", device]

        logger.info(f"Arrancando pacat para VAD: {' '.join(pacat_cmd)}")
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *pacat_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        except Exception as e:
            logger.error(f"Fallo al arrancar pacat: {e}")
            return

        # 30ms de audio a 16000Hz = 480 muestras * 2 bytes = 960 bytes
        frame_size = int(self._sample_rate * self._frame_duration_ms / 1000) * 2
        
        silence_start = None
        has_spoken = False

        try:
            while self.is_recording:
                try:
                    data = await proc.stdout.readexactly(frame_size)
                except asyncio.IncompleteReadError as e:
                    if len(e.partial) > 0:
                        data = e.partial
                    else:
                        break
                except Exception:
                    break

                if len(data) < frame_size:
                    break

                # VALIDACIÓN CRÍTICA: webrtcvad solo acepta frames de 10, 20 o 30ms
                is_speech = vad.is_speech(data, self._sample_rate)

                if is_speech:
                    if not has_spoken:
                        logger.info("¡Voz detectada por VAD!")
                        has_spoken = True
                    silence_start = None
                else:
                    if has_spoken:
                        if silence_start is None:
                            silence_start = time.time()
                        
                        silence_duration = time.time() - silence_start
                        if silence_duration >= self._silence_timeout:
                            logger.info(f"Silencio detectado ({silence_duration:.1f}s). Disparando auto-stop.")
                            callback() # Ejecutar callback sincrónico (el del main.py que lanza httpx)
                            break
                
                await asyncio.sleep(0.001)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error en bucle VAD: {e}")
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await proc.wait()
                except:
                    pass
            logger.info("Proceso pacat de VAD finalizado.")

    def stop_recording(self) -> Optional[Path]:
        """Detiene la grabación y el monitor."""
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None

        if not self._process:
            return None

        logger.info("Deteniendo grabación...")
        self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
        
        self._process = None
        
        if self._current_file and self._current_file.exists():
            if self._current_file.stat().st_size > 44:
                return self._current_file
            self._current_file.unlink(missing_ok=True)
        
        return None

    @property
    def is_recording(self) -> bool:
        return self._process is not None and self._process.poll() is None
