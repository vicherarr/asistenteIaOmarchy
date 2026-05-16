"""Tests para src/tts_engine.py"""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.tts_engine import TTSEngine, TTSError, PIPER_VOICES_DIR, DEFAULT_VOICE


@pytest.fixture
def engine():
    return TTSEngine()


@pytest.fixture
def engine_with_mock_voice(tmp_path):
    voice_file = tmp_path / f"{DEFAULT_VOICE}.onnx"
    voice_file.touch()
    return TTSEngine(voice_dir=tmp_path)


def test_get_voice_model_path(engine):
    expected = PIPER_VOICES_DIR / f"{DEFAULT_VOICE}.onnx"
    assert engine._get_voice_model_path() == expected


def test_get_default_bluetooth_sink_found():
    wpctl_output = """
Audio
 ├─ Sinks:
 │  *   45. UGREEN Bluetooth Headset            [vol: 0.65]
 │      50. Built-in Audio                      [vol: 0.80]
 ├─ Sources:
"""
    engine = TTSEngine()
    with patch('src.tts_engine.subprocess.run') as mock_run:
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


def test_speak_success(engine_with_mock_voice):
    with patch('subprocess.run') as mock_run:
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == "piper":
                output_file_idx = cmd.index("--output_file") + 1
                output_path = cmd[output_file_idx]
                Path(output_path).write_bytes(b"RIFF....WAVE....")
                return MagicMock(returncode=0, stdout="", stderr="")
            elif cmd[0] in ("aplay", "paplay"):
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        result = engine_with_mock_voice.speak("Hola mundo")

    assert result is not None
    assert result.endswith(".wav")


def test_speak_piper_not_found(engine_with_mock_voice):
    with patch('subprocess.run', side_effect=FileNotFoundError):
        with pytest.raises(TTSError, match="piper no encontrado"):
            engine_with_mock_voice.speak("Hola")


def test_speak_piper_failure(engine_with_mock_voice):
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Piper error: model incompatible",
        )
        with pytest.raises(TTSError, match="Piper falló"):
            engine_with_mock_voice.speak("Hola")


def test_speak_missing_model(engine):
    with pytest.raises(TTSError, match="Modelo de voz no encontrado"):
        engine.speak("Hola")


def test_speak_empty_output_file(engine_with_mock_voice):
    with patch('subprocess.run') as mock_run:
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == "piper":
                output_file_idx = cmd.index("--output_file") + 1
                output_path = cmd[output_file_idx]
                Path(output_path).touch()
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        with pytest.raises(TTSError, match="(?i)archivo de audio vacío"):
            engine_with_mock_voice.speak("Hola")


def test_play_audio_with_bluetooth_sink(engine):
    engine._default_sink = "45"

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        engine._play_audio("/tmp/test.wav")

    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "paplay"
    assert "--device" in call_args
    assert "45" in call_args


def test_play_audio_without_bluetooth_sink(engine):
    engine._default_sink = None

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        engine._play_audio("/tmp/test.wav")

    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "aplay"


def test_cleanup_temp_files(tmp_path):
    import time

    engine = TTSEngine()

    old_file = tmp_path / "tmp_old.wav"
    old_file.touch()

    old_mtime = time.time() - 10000

    with patch('tempfile.gettempdir', return_value=str(tmp_path)):
        with patch('time.time', return_value=time.time()):
            orig_stat = Path.stat
            def mocked_stat(self):
                result = orig_stat(self)
                if self.name == "tmp_old.wav":
                    class MockStatResult:
                        st_mtime = old_mtime
                    return MockStatResult()
                return result

            with patch.object(Path, 'stat', mocked_stat):
                cleaned = engine.cleanup_temp_files(max_age_seconds=5000)

    assert cleaned >= 1


def test_tts_engine_custom_voice(tmp_path):
    engine = TTSEngine(voice_name="custom_voice", voice_dir=tmp_path)
    assert engine.voice_name == "custom_voice"
    assert engine._get_voice_model_path() == tmp_path / "custom_voice.onnx"
