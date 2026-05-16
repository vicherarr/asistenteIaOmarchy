"""Tests para src/context_injector.py"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.context_injector import (
    get_cpu_info,
    get_gpu_info,
    get_memory_info,
    get_disk_info,
    get_display_info,
    get_window_info,
    get_audio_status,
    get_network_info,
    get_hardware_context,
    get_omarchy_commands,
    get_system_prompt,
    build_full_system_prompt,
    ContextInjectorError,
)


def test_get_cpu_info():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Model name: AMD Ryzen 7 5800X\nArchitecture: x86_64\nCPU(s): 16\n",
            stderr="",
        )
        result = get_cpu_info()

    assert "AMD Ryzen" in result
    mock_run.assert_called_once()


def test_get_gpu_info():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="01:00.0 VGA compatible controller: NVIDIA Corporation RTX 3070\n",
            stderr="",
        )
        result = get_gpu_info()

    assert "NVIDIA" in result
    assert "RTX 3070" in result


def test_get_memory_info():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Mem:           31Gi       8.2Gi        18Gi       2.1Gi       5.0Gi        21Gi\n",
            stderr="",
        )
        result = get_memory_info()

    assert "31Gi" in result


def test_get_disk_info():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="/dev/nvme0n1p2  476G  210G  242G  47% /\n",
            stderr="",
        )
        result = get_disk_info()

    assert "nvme0n1p2" in result


def test_get_display_info_hyprland_available():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"monitors": [{"id": 0, "name": "DP-1", "width": 2560, "height": 1440}]}',
            stderr="",
        )
        result = get_display_info()

    assert "DP-1" in result


def test_get_display_info_hyprland_unavailable():
    with patch('src.context_injector._run_cmd', return_value="Hyprland no disponible"):
        result = get_display_info()

    assert "Hyprland no disponible" in result


def test_get_window_info():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"address": "0x1234", "title": "Terminal", "class": "alacritty"}',
            stderr="",
        )
        result = get_window_info()

    assert "alacritty" in result


def test_get_audio_status():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Audio\n ├─ Sinks:\n │  *   45. Bluetooth Headset\n",
            stderr="",
        )
        result = get_audio_status()

    assert "Bluetooth" in result


def test_get_network_info():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="    inet 192.168.1.100/24 brd 192.168.1.255 scope global wlan0\n",
            stderr="",
        )
        result = get_network_info()

    assert "192.168.1.100" in result


def test_cmd_timeout():
    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired("cmd", 5)):
        result = get_cpu_info()

    assert result == "[Timeout]"


def test_cmd_exception():
    with patch('subprocess.run', side_effect=OSError("test error")):
        result = get_cpu_info()

    assert "Exception" in result


def test_get_hardware_context():
    with patch('src.context_injector.get_cpu_info', return_value="CPU: Ryzen 7"):
        with patch('src.context_injector.get_gpu_info', return_value="GPU: RTX 3070"):
            with patch('src.context_injector.get_memory_info', return_value="RAM: 32Gi"):
                with patch('src.context_injector.get_disk_info', return_value="Disk: 500G"):
                    with patch('src.context_injector.get_display_info', return_value="Display: DP-1"):
                        with patch('src.context_injector.get_window_info', return_value="Window: Terminal"):
                            with patch('src.context_injector.get_audio_status', return_value="Audio: BT"):
                                with patch('src.context_injector.get_network_info', return_value="Net: 192.168.1.1"):
                                    context = get_hardware_context()

    assert "CONTEXTO DE HARDWARE" in context
    assert "CPU" in context
    assert "Ryzen 7" in context
    assert "RTX 3070" in context


def test_get_omarchy_commands_from_file():
    config_path = Path(__file__).parent.parent / "config" / "omarchy_commands.md"
    if config_path.exists():
        result = get_omarchy_commands()
        assert "omarchy launch" in result
        assert "playerctl" in result


def test_get_omarchy_commands_fallback():
    with patch('src.context_injector.OMARCHY_COMMANDS_PATH') as mock_path:
        mock_path.exists.return_value = False
        result = get_omarchy_commands()

    assert "omarchy launch" in result
    assert "playerctl" in result


def test_get_system_prompt_from_file():
    config_path = Path(__file__).parent.parent / "config" / "system_prompt.txt"
    if config_path.exists():
        result = get_system_prompt()
        assert "AsistenteIA" in result


def test_get_system_prompt_fallback():
    with patch('src.context_injector.SYSTEM_PROMPT_PATH') as mock_path:
        mock_path.exists.return_value = False
        result = get_system_prompt()

    assert "CachyOS" in result
    assert "Hyprland" in result


def test_build_full_system_prompt():
    with patch('src.context_injector.get_hardware_context', return_value="HW: test"):
        with patch('src.context_injector.get_omarchy_commands', return_value="CMD: test"):
            with patch('src.context_injector.get_system_prompt', return_value="PROMPT: test"):
                prompt = build_full_system_prompt()

    assert "PROMPT: test" in prompt
    assert "HW: test" in prompt
    assert "CMD: test" in prompt
    assert "FORMATO DE RESPUESTA" in prompt
    assert "response_text" in prompt
    assert "commands" in prompt
    assert "action_type" in prompt
