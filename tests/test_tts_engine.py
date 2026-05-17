"""Tests para src/tts_engine.py"""

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

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
