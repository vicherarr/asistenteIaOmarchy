"""Tests para src/tts_engine.py"""

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import numpy as np
import pytest

from src.tts_engine import TTSEngine, TTSError


@pytest.fixture
def engine():
    # Evitamos que intente inicializar Kokoro de verdad si no está instalado
    with patch('src.tts_engine.TTSEngine._init_kokoro'):
        return TTSEngine()


@pytest.mark.asyncio
async def test_speak_empty_text(engine):
    result = await engine.speak("")
    assert result is None


@pytest.mark.asyncio
async def test_play_audio_with_bluetooth_sink(engine):
    with patch('asyncio.create_subprocess_exec') as mock_exec:
        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_exec.return_value = mock_process
        
        await engine._play_audio("/tmp/test.wav", sink_id="45")
    
    # Verificamos que se usó paplay con el dispositivo correcto
    args = mock_exec.call_args[0]
    assert "paplay" in args
    assert "--device" in args
    assert "45" in args


@pytest.mark.asyncio
async def test_play_audio_without_bluetooth_sink(engine):
    with patch('asyncio.create_subprocess_exec') as mock_exec:
        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_exec.return_value = mock_process
        
        # Para archivos wav sin sink usa paplay por defecto en nuestra nueva implementación
        await engine._play_audio("/tmp/test.wav", sink_id=None)
    
    args = mock_exec.call_args[0]
    assert "paplay" in args


@pytest.mark.asyncio
async def test_stop(engine):
    mock_process = AsyncMock()
    mock_process.returncode = None
    engine._playback_process = mock_process
    
    engine.stop()
    mock_process.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_speak_gtts_fallback(engine):
    # Simulamos que Kokoro no está disponible
    engine._kokoro_pipeline = None
    
    with patch('src.tts_engine.TTSEngine._speak_gtts', new_callable=AsyncMock) as mock_gtts:
        mock_gtts.return_value = "/tmp/gtts.mp3"
        await engine.speak("Hola")
        mock_gtts.assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_only_no_kokoro(engine):
    """synthesize_only devuelve None si Kokoro no está disponible."""
    engine._kokoro_pipeline = None
    result = await engine.synthesize_only("Hola")
    assert result is None


@pytest.mark.asyncio
async def test_synthesize_only_empty_text(engine):
    """synthesize_only devuelve None para texto vacío."""
    result = await engine.synthesize_only("")
    assert result is None


@pytest.mark.asyncio
async def test_synthesize_only_returns_numpy(engine):
    """synthesize_only genera un array numpy cuando Kokoro está disponible."""
    # Mock del pipeline de Kokoro
    mock_pipeline = MagicMock()
    # Simular generator que produce chunks de audio
    audio_chunk = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    mock_pipeline.return_value = [
        ("phonemes", "graphemes", audio_chunk),
    ]
    engine._kokoro_pipeline = mock_pipeline
    engine._is_playing = True  # Necesario para que el loop no se cancele

    result = await engine.synthesize_only("Hola")

    assert result is not None
    assert isinstance(result, np.ndarray)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_synthesize_only_cancellation(engine):
    """synthesize_only respeta _is_playing para cancelación."""
    mock_pipeline = MagicMock()
    audio_chunk = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    # Generator que produce muchos chunks pero se cancela
    def generator():
        engine._is_playing = False  # Simular cancelación inmediata
        yield ("p", "g", audio_chunk)
    mock_pipeline.return_value = generator()
    engine._kokoro_pipeline = mock_pipeline

    result = await engine.synthesize_only("Hola")
    # Debería ser None porque se canceló antes de procesar
    assert result is None


@pytest.mark.asyncio
async def test_play_audio_array_empty(engine):
    """play_audio_array no hace nada con arrays vacíos."""
    result = await engine.play_audio_array(np.array([]))
    # No debería lanzar excepción
    assert result is None


@pytest.mark.asyncio
async def test_play_audio_array_opens_persistent_stream(engine):
    """play_audio_array abre un OutputStream persistente la primera vez."""
    mock_stream = MagicMock()
    with patch('sounddevice.OutputStream') as mock_os:
        mock_os.return_value.__enter__ = MagicMock(return_value=mock_stream)
        mock_os.return_value.__exit__ = MagicMock(return_value=False)
        mock_stream.closed = False

        audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        await engine.play_audio_array(audio)

        # Verificar que se abrió el stream persistente
        assert engine._persistent_stream is not None


@pytest.mark.asyncio
async def test_play_audio_array_reuses_stream(engine):
    """play_audio_array reutiliza el stream persistente abierto."""
    mock_stream = MagicMock()
    mock_stream.closed = False
    engine._persistent_stream = mock_stream

    audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    await engine.play_audio_array(audio)

    # Verificar que se escribió al stream existente (no se abrió uno nuevo)
    mock_stream.write.assert_called_once()


@pytest.mark.asyncio
async def test_close_persistent_stream(engine):
    """close_persistent_stream cierra y limpia el stream."""
    mock_stream = MagicMock()
    engine._persistent_stream = mock_stream

    engine.close_persistent_stream()

    mock_stream.stop.assert_called_once()
    mock_stream.close.assert_called_once()
    assert engine._persistent_stream is None


@pytest.mark.asyncio
async def test_tensor_to_numpy(engine):
    """_tensor_to_numpy convierte correctamente diferentes tipos de tensor."""
    # Test con objeto que tiene .cpu()
    mock_tensor = MagicMock()
    mock_tensor.cpu.return_value.numpy.return_value = np.array([1.0])
    result = engine._tensor_to_numpy(mock_tensor)
    assert isinstance(result, np.ndarray)

    # Test con objeto que tiene .numpy()
    mock_tensor2 = MagicMock()
    mock_tensor2.numpy.return_value = np.array([2.0])
    del mock_tensor2.cpu
    result2 = engine._tensor_to_numpy(mock_tensor2)
    assert isinstance(result2, np.ndarray)

    # Test con array numpy directo
    arr = np.array([3.0])
    result3 = engine._tensor_to_numpy(arr)
    assert np.array_equal(result3, arr)
