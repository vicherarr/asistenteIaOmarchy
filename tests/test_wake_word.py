import asyncio
import subprocess
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
import numpy as np

from src.wake_word_listener import WakeWordListener


class DummyAppState:
    def __init__(self):
        self.is_recording = False
        self.processing = False
        self.audio_manager = MagicMock()
        self.audio_manager.default_source = "mock_source"


@pytest.fixture
def app_state():
    return DummyAppState()


@pytest.mark.asyncio
async def test_listener_starts_and_stops(app_state):
    callback = AsyncMock()
    listener = WakeWordListener(on_wake_word_detected=callback, app_state=app_state)
    
    # Parchear el loop principal para que no corra pacat real
    with patch.object(listener, '_listen_loop', return_value=None):
        listener.start()
        assert listener.is_running is True
        assert listener.task is not None
        
        listener.stop()
        assert listener.is_running is False
        assert listener.task is None


@pytest.mark.asyncio
async def test_listener_skips_when_app_busy(app_state):
    callback = AsyncMock()
    listener = WakeWordListener(on_wake_word_detected=callback, app_state=app_state)
    
    # Simulamos que la app está ocupada
    app_state.is_recording = True
    
    # Parcheamos la importación diferida de openwakeword
    mock_model_class = MagicMock()
    
    with patch('openwakeword.model.Model', return_value=mock_model_class), \
         patch('asyncio.sleep') as mock_sleep:
        
        # Hacemos que corra solo una iteración cortando el bucle de inmediato
        async def mock_sleep_side_effect(delay):
            listener.is_running = False  # Detiene el bucle
            return None
        
        mock_sleep.side_effect = mock_sleep_side_effect
        
        listener.is_running = True
        await listener._listen_loop()
        
        # Debe haber dormido esperando a que se libere el estado de grabación
        mock_sleep.assert_any_call(0.5)


@pytest.mark.asyncio
async def test_listener_detects_wakeword_correctly(app_state):
    callback = AsyncMock()
    listener = WakeWordListener(on_wake_word_detected=callback, app_state=app_state, threshold=0.5)
    
    # Detener el bucle del listener cuando se detecta el wake word
    async def mock_callback():
        listener.is_running = False
    callback.side_effect = mock_callback
    
    # Mockear openwakeword Model
    mock_model_instance = MagicMock()
    # Simular una detección exitosa de 'hey_jarvis'
    mock_model_instance.predict.return_value = {"hey_jarvis": 0.85}
    
    # Mockear pacat subprocess
    mock_proc = AsyncMock()
    mock_proc.returncode = None
    mock_proc.stdout.readexactly.return_value = b"\x00" * 2560 # 1280 samples * 2 bytes
    
    # proc.terminate es un método síncrono en asyncio.subprocess.Process
    mock_proc.terminate = MagicMock()
    
    # Simular que al esperar (wait), el proceso se marca como terminado (returncode = 0)
    async def mock_wait():
        mock_proc.returncode = 0
        return 0
    mock_proc.wait.side_effect = mock_wait
    
    with patch('openwakeword.model.Model', return_value=mock_model_instance), \
         patch('asyncio.create_subprocess_exec', return_value=mock_proc) as mock_exec:
        
        # Iniciamos bucle
        listener.is_running = True
        
        # Bucle debe romperse después de la detección
        await listener._listen_loop()
        
        # 1. Se debió consultar a openwakeword
        mock_model_instance.predict.assert_called()
        
        # 2. Se debió parar pacat para liberar hardware
        mock_proc.terminate.assert_called_once()
        
        # 3. El callback debió ser invocado
        callback.assert_called_once()



