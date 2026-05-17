"""Tests para src/tts_engine.py"""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.tts_engine import TTSEngine, TTSError


@pytest.fixture
def engine():
    return TTSEngine()


def test_get_default_bluetooth_sink_found(engine):
    wpctl_output = """
Audio
 ├─ Sinks:
 │  *   45. UGREEN Bluetooth Headset            [vol: 0.65]
 │      50. Built-in Audio                      [vol: 0.80]
 ├─ Sources:
"""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=wpctl_output,
            stderr="",
        )
        sink_id = engine._get_default_bluetooth_sink()

    assert sink_id == "45"


def test_get_default_bluetooth_sink_not_found(engine):
    wpctl_output = """
Audio
 ├─ Sinks:
 │  *   50. Built-in Audio                      [vol: 0.80]
"""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=wpctl_output,
            stderr="",
        )
        sink_id = engine._get_default_bluetooth_sink()

    assert sink_id is None


def test_speak_empty_text(engine):
    result = engine.speak("")
    assert result is None

    result = engine.speak("   ")
    assert result is None


def test_play_audio_with_bluetooth_sink(engine):
    engine._default_sink = "45"

    with patch('subprocess.Popen') as mock_popen:
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process
        engine._play_audio("/tmp/test.wav")

    call_args = mock_popen.call_args[0][0]
    assert call_args[0] == "paplay"
    assert "--device" in call_args
    assert "45" in call_args


def test_play_audio_without_bluetooth_sink(engine):
    engine._default_sink = None

    with patch('subprocess.Popen') as mock_popen:
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process
        engine._play_audio("/tmp/test.wav")

    call_args = mock_popen.call_args[0][0]
    assert call_args[0] == "aplay"

def test_stop(engine):
    with patch('subprocess.Popen') as mock_popen:
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        engine._playback_process = mock_process
        engine.stop()
        mock_process.kill.assert_called_once()
        assert engine._playback_process is None
