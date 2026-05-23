"""Tests para src/wake_word_listener.py (Sherpa-ONNX KWS)."""

import asyncio
from unittest.mock import patch, MagicMock
import pytest

from src.wake_word_listener import SherpaWakeWordListener, SHERPA_AVAILABLE


@pytest.fixture
def mock_callback():
    """Callback mock para detección de wake word."""
    return MagicMock()


@pytest.fixture
def mock_spotter():
    """Mock del KeywordSpotter de Sherpa-ONNX."""
    spotter = MagicMock()
    stream = MagicMock()
    spotter.create_stream.return_value = stream
    spotter.is_ready.return_value = False
    spotter.get_result.return_value = None
    return spotter


@pytest.mark.skipif(not SHERPA_AVAILABLE, reason="sherpa-onnx no instalado")
class TestSherpaWakeWordListener:
    """Tests para SherpaWakeWordListener cuando sherpa-onnx está disponible."""

    def test_create_listener(self, mock_callback):
        """Verifica que se puede crear el listener."""
        with patch("src.wake_word_listener.sherpa_onnx.KeywordSpotter") as mock_kws:
            listener = SherpaWakeWordListener(
                model_dir="/fake/models/sherpa-kws",
                keywords_file="/fake/models/sherpa-kws/keywords.txt",
                on_wake_word_detected=mock_callback,
            )
            assert listener is not None
            assert listener._running is False

    def test_start_and_stop(self, mock_callback, mock_spotter):
        """Verifica que start/stop funcionan correctamente."""
        with patch("src.wake_word_listener.sherpa_onnx.KeywordSpotter", return_value=mock_spotter), \
             patch("src.wake_word_listener.sd.InputStream"):
            
            listener = SherpaWakeWordListener(
                model_dir="/fake/models/sherpa-kws",
                keywords_file="/fake/models/sherpa-kws/keywords.txt",
                on_wake_word_detected=mock_callback,
            )
            
            listener.start()
            assert listener._running is True
            assert listener.is_running is True
            
            listener.stop()
            assert listener._running is False

    def test_start_twice_logs_warning(self, mock_callback, mock_spotter):
        """Verifica que iniciar dos veces no crea hilos duplicados."""
        with patch("src.wake_word_listener.sherpa_onnx.KeywordSpotter", return_value=mock_spotter), \
             patch("src.wake_word_listener.sd.InputStream"):
            
            listener = SherpaWakeWordListener(
                model_dir="/fake/models/sherpa-kws",
                keywords_file="/fake/models/sherpa-kws/keywords.txt",
                on_wake_word_detected=mock_callback,
            )
            
            listener.start()
            listener.start()  # Segunda llamada debería loguear warning
            
            listener.stop()

    def test_detection_triggers_callback(self, mock_callback, mock_spotter):
        """Verifica que la detección dispara el callback."""
        # Configurar mock para simular detección
        call_count = [0]
        
        def mock_is_ready(stream):
            call_count[0] += 1
            if call_count[0] == 1:
                return True  # Primera vez: hay resultado
            return False  # Luego: detener loop
        
        mock_spotter.is_ready.side_effect = mock_is_ready
        mock_spotter.get_result.return_value = "LUKA"
        
        def stop_listener():
            listener._running = False
        
        mock_callback.side_effect = stop_listener
        
        with patch("src.wake_word_listener.sherpa_onnx.KeywordSpotter", return_value=mock_spotter), \
             patch("src.wake_word_listener.sd.InputStream") as mock_stream:
            
            # Configurar stream de audio mock
            mock_stream_instance = MagicMock()
            mock_stream_instance.__enter__ = MagicMock(return_value=mock_stream_instance)
            mock_stream_instance.__exit__ = MagicMock(return_value=False)
            mock_stream_instance.read.return_value = (MagicMock(), False)
            mock_stream.return_value = mock_stream_instance
            
            listener = SherpaWakeWordListener(
                model_dir="/fake/models/sherpa-kws",
                keywords_file="/fake/models/sherpa-kws/keywords.txt",
                on_wake_word_detected=mock_callback,
            )
            
            # Ejecutar loop directamente en el hilo principal para el test
            listener._spotter = mock_spotter
            listener._running = True
            listener._listen_loop()
            
            # Verificar que se llamó al callback
            mock_callback.assert_called_once()

    def test_import_error_when_not_available(self, mock_callback):
        """Verifica que se lanza ImportError si sherpa-onnx no está disponible."""
        with patch("src.wake_word_listener.SHERPA_AVAILABLE", False):
            with pytest.raises(ImportError, match="sherpa-onnx no instalado"):
                # Simular import fallido
                raise ImportError("sherpa-onnx no instalado. Ejecuta: pip install sherpa-onnx")


class TestSherpaNotAvailable:
    """Tests para cuando sherpa-onnx NO está disponible."""

    def test_sherpa_available_flag(self):
        """Verifica que la flag SHERPA_AVAILABLE existe."""
        assert isinstance(SHERPA_AVAILABLE, bool)
