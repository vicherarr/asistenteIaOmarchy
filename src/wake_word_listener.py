"""
Wake Word Listener basado en Sherpa-ONNX (Next-gen Kaldi).

Reemplaza OpenWakeWord por un sistema de keyword spotting más preciso
que permite wake words personalizadas sin reentrenar el modelo.

Uso:
    from src.wake_word_listener import SherpaWakeWordListener
    
    listener = SherpaWakeWordListener(
        model_dir="models/sherpa-kws",
        keywords_file="models/sherpa-kws/keywords.txt",
        on_wake_word_detected=callback,
    )
    listener.start()
    # ...
    listener.stop()

Requiere: sherpa-onnx
"""

import asyncio
import inspect
import logging
import threading
from typing import Callable, Optional

try:
    import sherpa_onnx
    import sounddevice as sd
    SHERPA_AVAILABLE = True
except ImportError:
    SHERPA_AVAILABLE = False

logger = logging.getLogger(__name__)


class SherpaWakeWordListener:
    """
    Escucha en background el micrófono para detectar la wake word
    usando Sherpa-ONNX KeywordSpotter.
    
    El modelo es un Zipformer entrenado en GigaSpeech (10,000h de inglés).
    La wake word se define en un archivo de texto sin necesidad de reentrenar.
    """

    def __init__(
        self,
        model_dir: str,
        keywords_file: str,
        on_wake_word_detected: Callable[[], None],
        provider: str = "cpu",
        num_threads: int = 1,
        keywords_threshold: float = 0.25,
        keywords_score: float = 1.0,
        max_active_paths: int = 4,
        num_trailing_blanks: int = 1,
    ) -> None:
        """
        Args:
            model_dir: Directorio con encoder.onnx, decoder.onnx, joiner.onnx, tokens.txt
            keywords_file: Ruta al archivo de keywords generado con sherpa-onnx-cli text2token
            on_wake_word_detected: Callback sin argumentos que se ejecuta al detectar la wake word
            provider: "cpu" o "cuda"
            num_threads: Hilos para ONNX Runtime (1 es suficiente para CPU)
            keywords_threshold: Umbral de detección (0.0-1.0). Más alto = más estricto
            keywords_score: Boost score para los tokens de la keyword
            max_active_paths: Beam width durante decoding
            num_trailing_blanks: Blanks necesarios después de la keyword para confirmar detección
        """
        if not SHERPA_AVAILABLE:
            raise ImportError(
                "sherpa-onnx no instalado. Ejecuta: pip install sherpa-onnx"
            )

        self._model_dir = model_dir
        self._keywords_file = keywords_file
        self._on_wake_word_detected = on_wake_word_detected
        self._provider = provider
        self._num_threads = num_threads
        self._keywords_threshold = keywords_threshold
        self._keywords_score = keywords_score
        self._max_active_paths = max_active_paths
        self._num_trailing_blanks = num_trailing_blanks

        self._spotter: Optional[sherpa_onnx.KeywordSpotter] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _create_spotter(self) -> sherpa_onnx.KeywordSpotter:
        """Crea la instancia del KeywordSpotter de Sherpa-ONNX."""
        from pathlib import Path
        base = Path(self._model_dir)
        
        return sherpa_onnx.KeywordSpotter(
            tokens=str(base / "tokens.txt"),
            encoder=str(base / "encoder.onnx"),
            decoder=str(base / "decoder.onnx"),
            joiner=str(base / "joiner.onnx"),
            num_threads=self._num_threads,
            max_active_paths=self._max_active_paths,
            keywords_file=self._keywords_file,
            keywords_score=self._keywords_score,
            keywords_threshold=self._keywords_threshold,
            num_trailing_blanks=self._num_trailing_blanks,
            provider=self._provider,
        )

    def start(self) -> None:
        """Inicia el hilo de escucha en background."""
        if self._running:
            logger.warning("WakeWordListener ya está en ejecución.")
            return

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        self._spotter = self._create_spotter()
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        logger.info("SherpaWakeWordListener iniciado. Escuchando 'LUKA'...")

    def stop(self) -> None:
        """Detiene la escucha y libera recursos."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._spotter = None
        logger.info("SherpaWakeWordListener detenido.")

    def _listen_loop(self) -> None:
        """Loop principal: captura audio del micrófono y detecta la wake word."""
        sample_rate = 16000
        samples_per_read = int(0.1 * sample_rate)  # 100ms chunks

        if self._spotter is None:
            return

        stream = self._spotter.create_stream()

        try:
            with sd.InputStream(
                channels=1,
                dtype="float32",
                samplerate=sample_rate,
                blocksize=samples_per_read,
            ) as s:
                while self._running:
                    samples, overflowed = s.read(samples_per_read)
                    if overflowed:
                        logger.warning("Buffer overflow en micrófono wake word")

                    samples = samples.reshape(-1)
                    stream.accept_waveform(sample_rate, samples)

                    while self._spotter.is_ready(stream):
                        self._spotter.decode_stream(stream)
                        result = self._spotter.get_result(stream)
                        if result:
                            logger.info(f"Wake word detectada: {result}")
                            try:
                                if inspect.iscoroutinefunction(self._on_wake_word_detected):
                                    if self._loop:
                                        asyncio.run_coroutine_threadsafe(
                                            self._on_wake_word_detected(), self._loop
                                        )
                                    else:
                                        logger.error("No hay event loop para ejecutar callback async")
                                else:
                                    self._on_wake_word_detected()
                            except Exception as e:
                                logger.error(f"Error en callback wake word: {e}")
                            # Resetear stream para detectar la próxima
                            self._spotter.reset_stream(stream)
        except Exception as e:
            logger.error(f"Error en loop de wake word: {e}")
        finally:
            logger.debug("Loop de wake word finalizado.")

    @property
    def is_running(self) -> bool:
        return self._running
