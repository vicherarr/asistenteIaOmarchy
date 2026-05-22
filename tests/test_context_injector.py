"""Tests para src/context_injector.py - versión async."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

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
    get_system_context,
    ContextInjectorError,
    _run_cmd,
)


@pytest.mark.asyncio
async def test_get_cpu_info():
    with patch('asyncio.create_subprocess_shell') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            b"Model name: AMD Ryzen 7 5800X\nArchitecture: x86_64\nCPU(s): 16\n",
            b""
        )
        mock_exec.return_value = mock_proc

        result = await get_cpu_info()

    assert "AMD Ryzen" in result
    mock_exec.assert_called_once()


@pytest.mark.asyncio
async def test_get_gpu_info():
    with patch('asyncio.create_subprocess_shell') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            b"01:00.0 VGA compatible controller: NVIDIA Corporation RTX 3070\n",
            b""
        )
        mock_exec.return_value = mock_proc

        result = await get_gpu_info()

    assert "NVIDIA" in result
    assert "RTX 3070" in result


@pytest.mark.asyncio
async def test_get_memory_info():
    with patch('asyncio.create_subprocess_shell') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            b"Mem:           31Gi       8.2Gi        18Gi       2.1Gi       5.0Gi        21Gi\n",
            b""
        )
        mock_exec.return_value = mock_proc

        result = await get_memory_info()

    assert "31Gi" in result


@pytest.mark.asyncio
async def test_get_disk_info():
    with patch('asyncio.create_subprocess_exec') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            b"/dev/nvme0n1p2  476G  210G  242G  47% /\n",
            b""
        )
        mock_exec.return_value = mock_proc

        result = await get_disk_info()

    assert "nvme0n1p2" in result


@pytest.mark.asyncio
async def test_get_display_info_hyprland_available():
    with patch('asyncio.create_subprocess_shell') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            b'{"monitors": [{"id": 0, "name": "DP-1", "width": 2560, "height": 1440}]}',
            b""
        )
        mock_exec.return_value = mock_proc

        result = await get_display_info()

    assert "DP-1" in result


@pytest.mark.asyncio
async def test_get_window_info():
    with patch('asyncio.create_subprocess_shell') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            b'{"address": "0x1234", "title": "Terminal", "class": "alacritty"}',
            b""
        )
        mock_exec.return_value = mock_proc

        result = await get_window_info()

    assert "alacritty" in result


@pytest.mark.asyncio
async def test_get_audio_status():
    with patch('asyncio.create_subprocess_shell') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            "Audio\n Sinks:\n * 45. Bluetooth Headset\n".encode(),
            b""
        )
        mock_exec.return_value = mock_proc

        result = await get_audio_status()

    assert "Bluetooth" in result


@pytest.mark.asyncio
async def test_get_network_info():
    with patch('asyncio.create_subprocess_shell') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            b"    inet 192.168.1.100/24 brd 192.168.1.255 scope global wlan0\n",
            b""
        )
        mock_exec.return_value = mock_proc

        result = await get_network_info()

    assert "192.168.1.100" in result


@pytest.mark.asyncio
async def test_cmd_timeout():
    with patch('asyncio.create_subprocess_shell') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_exec.return_value = mock_proc

        result = await get_cpu_info()

    assert "[Timeout]" in result


@pytest.mark.asyncio
async def test_cmd_exception():
    with patch('asyncio.create_subprocess_shell', side_effect=OSError("test error")):
        result = await get_cpu_info()

    assert "Exception" in result


@pytest.mark.asyncio
async def test_cmd_uses_shell_for_pipes():
    """Verifica que comandos con pipes usan create_subprocess_shell."""
    with patch('asyncio.create_subprocess_shell') as mock_shell:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"output", b"")
        mock_shell.return_value = mock_proc

        result = await _run_cmd("lscpu | grep Model")

    assert "output" in result
    mock_shell.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_uses_exec_for_simple():
    """Verifica que comandos simples usan create_subprocess_exec."""
    with patch('asyncio.create_subprocess_exec') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"output", b"")
        mock_exec.return_value = mock_proc

        result = await _run_cmd("free -h")

    assert "output" in result
    mock_exec.assert_called_once()


@pytest.mark.asyncio
async def test_get_hardware_context_parallel():
    """Verifica que get_hardware_context ejecuta todas las consultas en paralelo."""
    async def fake_cpu(): return "CPU: Ryzen 7"
    async def fake_gpu(): return "GPU: RTX 3070"
    async def fake_ram(): return "RAM: 32Gi"
    async def fake_disk(): return "Disk: 500G"
    async def fake_display(): return "Display: DP-1"
    async def fake_window(): return "Window: Terminal"
    async def fake_audio(): return "Audio: BT"
    async def fake_net(): return "Net: 192.168.1.1"

    with patch('src.context_injector.get_cpu_info', fake_cpu):
        with patch('src.context_injector.get_gpu_info', fake_gpu):
            with patch('src.context_injector.get_memory_info', fake_ram):
                with patch('src.context_injector.get_disk_info', fake_disk):
                    with patch('src.context_injector.get_display_info', fake_display):
                        with patch('src.context_injector.get_window_info', fake_window):
                            with patch('src.context_injector.get_audio_status', fake_audio):
                                with patch('src.context_injector.get_network_info', fake_net):
                                    context = await get_hardware_context()

    assert "CONTEXTO DE HARDWARE" in context
    assert "CPU" in context
    assert "Ryzen 7" in context
    assert "RTX 3070" in context


@pytest.mark.asyncio
async def test_get_system_context():
    """Verifica que get_system_context es alias de get_hardware_context."""
    with patch('src.context_injector.get_hardware_context', return_value="test context"):
        result = await get_system_context()
    assert result == "test context"
