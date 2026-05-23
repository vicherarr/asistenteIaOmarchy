"""Tests para src/audio_manager.py"""

import asyncio
import subprocess
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.audio_manager import AudioManager, AudioManagerError, DeviceType, AudioDevice


WPCTL_STATUS_OUTPUT = """
PipeWire 'pipewire-0' [0.3.86, victor@ cachyos, cookie:1234567890]
 └─ Clients:
       33. xdg-desktop-portal                  0.3.86
       42. WirePlumber                           0.4.17
       55. wpctl                                 0.3.86

Audio
 ├─ Devices:
 │      45. UGREEN Bluetooth Headset            [bluetooth]
 │      50. Built-in Audio                      [alsa]
 │
 ├─ Sinks:
 │      45. UGREEN Bluetooth Headset            [vol: 0.65 MUTED]
 │  *   50. Built-in Audio Analog Stereo        [vol: 0.80]
 │
 ├─ Sink endpoints:
 │
 ├─ Sources:
 │  *   46. UGREEN Bluetooth Headset Monitor    [vol: 1.00]
 │      51. Built-in Audio Analog Stereo        [vol: 0.75]
 │
 ├─ Source endpoints:
 │
 └─ Streams:
"""


@pytest.fixture
def manager():
    return AudioManager()


@pytest.mark.asyncio
async def test_list_devices_parses_output_correctly(manager):
    with patch.object(manager, '_run_wpctl', return_value=WPCTL_STATUS_OUTPUT):
        devices = await manager.list_devices()

    assert len(devices) > 0

    sinks = [d for d in devices if d.device_type == DeviceType.SINK]
    sources = [d for d in devices if d.device_type == DeviceType.SOURCE]

    bt_sinks = [d for d in sinks if d.is_bluetooth]
    assert len(bt_sinks) >= 1
    assert bt_sinks[0].description == "UGREEN Bluetooth Headset [vol: 0.65 MUTED]"


@pytest.mark.asyncio
async def test_get_default_bluetooth_source(manager):
    with patch.object(manager, '_run_wpctl', return_value=WPCTL_STATUS_OUTPUT):
        source = await manager.get_default_bluetooth_source()

    assert source is not None
    assert source.is_bluetooth is True
    assert source.device_type == DeviceType.SOURCE


@pytest.mark.asyncio
async def test_get_default_bluetooth_sink(manager):
    with patch.object(manager, '_run_wpctl', return_value=WPCTL_STATUS_OUTPUT):
        sink = await manager.get_default_bluetooth_sink()

    assert sink is not None
    assert sink.is_bluetooth is True
    assert sink.device_type == DeviceType.SINK


@pytest.mark.asyncio
async def test_set_default_source(manager):
    with patch.object(manager, '_run_wpctl', return_value="") as mock_wpctl:
        await manager.set_default_source("46")

    mock_wpctl.assert_called_once_with(["set-default", "46"])
    assert manager._default_source == "46"


@pytest.mark.asyncio
async def test_set_default_sink(manager):
    with patch.object(manager, '_run_wpctl', return_value="") as mock_wpctl:
        await manager.set_default_sink("45")

    mock_wpctl.assert_called_once_with(["set-default", "45"])
    assert manager._default_sink == "45"


@pytest.mark.asyncio
async def test_auto_configure_bluetooth(manager):
    with patch.object(manager, '_run_wpctl', return_value=WPCTL_STATUS_OUTPUT):
        source_id, sink_id = await manager.auto_configure_bluetooth()

    assert source_id is not None
    assert sink_id is not None


@pytest.mark.asyncio
async def test_no_bluetooth_devices(manager):
    no_bt_output = """
Audio
 ├─ Sinks:
 │  *   50. Built-in Audio Analog Stereo        [vol: 0.80]
 ├─ Sources:
 │      51. Built-in Audio Analog Stereo        [vol: 0.75]
"""
    with patch.object(manager, '_run_wpctl', return_value=no_bt_output):
        source = await manager.get_default_bluetooth_source()
        sink = await manager.get_default_bluetooth_sink()

    assert source is None
    assert sink is None


@pytest.mark.asyncio
async def test_wpctl_not_found(manager):
    with patch('asyncio.create_subprocess_exec', side_effect=FileNotFoundError):
        with pytest.raises(AudioManagerError, match="wpctl no encontrado"):
            await manager._run_wpctl(["status"])


@pytest.mark.asyncio
async def test_wpctl_timeout(manager):
    mock_process = MagicMock()
    mock_process.communicate.side_effect = asyncio.TimeoutError()

    with patch('asyncio.create_subprocess_exec', return_value=mock_process):
        with pytest.raises(AudioManagerError, match="Timeout"):
            await manager._run_wpctl(["status"])


@pytest.mark.asyncio
async def test_wpctl_error(manager):
    mock_process = AsyncMock()
    mock_process.returncode = 1
    mock_process.communicate.return_value = (b"", b"Error: invalid command")

    with patch('asyncio.create_subprocess_exec', return_value=mock_process):
        with pytest.raises(AudioManagerError, match="wpctl falló"):
            await manager._run_wpctl(["status"])


@pytest.mark.asyncio
async def test_get_status_summary(manager):
    with patch.object(manager, '_run_wpctl', return_value=WPCTL_STATUS_OUTPUT):
        summary = await manager.get_status_summary()

    assert "Bluetooth" in summary
    assert "UGREEN" in summary


def test_audio_device_repr():
    device = AudioDevice(
        node_id="45",
        name="bluez_output",
        description="UGREEN Bluetooth Headset",
        device_type=DeviceType.SINK,
        is_bluetooth=True,
        is_default=True,
    )
    repr_str = repr(device)
    assert "[BT]" in repr_str
    assert "(default)" in repr_str
    assert "45" in repr_str


@pytest.mark.asyncio
async def test_pause_active_players(manager):
    mock_proc_list = AsyncMock()
    mock_proc_list.returncode = 0
    mock_proc_list.communicate.return_value = (b"spotify\nmpv\n", b"")
    
    mock_proc_status_spotify = AsyncMock()
    mock_proc_status_spotify.communicate.return_value = (b"Playing\n", b"")
    
    mock_proc_status_mpv = AsyncMock()
    mock_proc_status_mpv.communicate.return_value = (b"Paused\n", b"")
    
    mock_proc_pause = AsyncMock()
    
    async def side_effect(cmd, *args, **kwargs):
        if "--list-all" in args:
            return mock_proc_list
        if "spotify" in args and "status" in args:
            return mock_proc_status_spotify
        if "mpv" in args and "status" in args:
            return mock_proc_status_mpv
        if "pause" in args:
            return mock_proc_pause
        return AsyncMock()

    with patch('asyncio.create_subprocess_exec', side_effect=side_effect) as mock_exec:
        paused = await manager.pause_active_players()
        
    assert paused == ["spotify"]
    mock_exec.assert_any_call("playerctl", "-p", "spotify", "pause", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.asyncio
async def test_resume_players(manager):
    mock_proc_resume = AsyncMock()
    
    with patch('asyncio.create_subprocess_exec', return_value=mock_proc_resume) as mock_exec:
        await manager.resume_players(["spotify"])
        
    mock_exec.assert_called_once_with("playerctl", "-p", "spotify", "play", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

