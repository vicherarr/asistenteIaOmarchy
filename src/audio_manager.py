"""Módulo de gestión de audio Bluetooth vía PipeWire/WirePlumber."""

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class DeviceType(Enum):
    SOURCE = "source"
    SINK = "sink"


@dataclass
class AudioDevice:
    node_id: str
    name: str
    description: str
    device_type: DeviceType
    is_bluetooth: bool
    is_default: bool = False

    def __repr__(self) -> str:
        bt_tag = " [BT]" if self.is_bluetooth else ""
        default_tag = " (default)" if self.is_default else ""
        return f"{self.description}{bt_tag}{default_tag} (id={self.node_id})"


class AudioManagerError(Exception):
    """Errores específicos del gestor de audio."""
    pass


class AudioManager:
    """Gestiona dispositivos de audio Bluetooth mediante PipeWire/WirePlumber."""

    def __init__(self) -> None:
        self._default_source: Optional[str] = None
        self._default_sink: Optional[str] = None

    @property
    def default_source(self) -> Optional[str]:
        return self._default_source

    @property
    def default_sink(self) -> Optional[str]:
        return self._default_sink

    async def _run_wpctl(self, args: list[str]) -> str:
        """Ejecuta un comando wpctl de forma asíncrona y devuelve su salida."""
        try:
            process = await asyncio.create_subprocess_exec(
                "wpctl", *args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            
            if process.returncode != 0:
                raise AudioManagerError(f"wpctl falló: {stderr.decode().strip()}")
            return stdout.decode()
        except FileNotFoundError:
            raise AudioManagerError("wpctl no encontrado. Instalar wireplumber.")
        except asyncio.TimeoutError:
            raise AudioManagerError("Timeout ejecutando wpctl")

    async def list_devices(self) -> list[AudioDevice]:
        """Lista todos los dispositivos de audio disponibles de forma asíncrona."""
        output = await self._run_wpctl(["status"])
        devices: list[AudioDevice] = []

        current_type: Optional[DeviceType] = None
        in_audio_section = False

        for line in output.splitlines():
            stripped = line.strip()

            if stripped == "Audio":
                in_audio_section = True
                continue
            elif in_audio_section and stripped in ("Video", "Settings"):
                in_audio_section = False
                current_type = None
                continue

            if not in_audio_section:
                continue

            if "Sinks:" in stripped:
                current_type = DeviceType.SINK
                continue
            elif "Sources:" in stripped:
                current_type = DeviceType.SOURCE
                continue
            elif "Filters:" in stripped:
                current_type = DeviceType.SOURCE
                continue
            elif "Streams:" in stripped or "Devices:" in stripped:
                current_type = None
                continue

            if current_type is None or not stripped:
                continue

            cleaned = stripped.replace("│", "").replace("├", "").replace("─", "").replace("└", "").strip()
            if not cleaned:
                continue

            parts = cleaned.split()
            if len(parts) < 2:
                continue

            is_default = False
            node_id = ""

            if parts[0] == "*":
                is_default = True
                if len(parts) < 3:
                    continue
                node_id = parts[1].replace(".", "").replace("*", "")
                description = " ".join(parts[2:]).strip()
            else:
                node_id = parts[0].replace(".", "").replace("*", "")
                is_default = "*" in parts[0]
                description = " ".join(parts[1:]).strip()

            if not node_id.isdigit():
                continue

            bt_keywords = ["bluetooth", "bluez", "bluez5", "bt", "jbl", "sony", "bose", "airpods", "galaxy buds", "wh-", "wf-", "xm"]
            is_bluetooth = any(kw in description.lower() for kw in bt_keywords)

            device = AudioDevice(
                node_id=node_id,
                name=parts[1].strip("*"),
                description=description,
                device_type=current_type,
                is_bluetooth=is_bluetooth,
                is_default=is_default,
            )
            devices.append(device)

        return devices

    async def get_default_bluetooth_source(self) -> Optional[AudioDevice]:
        """Obtiene el dispositivo fuente Bluetooth por defecto."""
        devices = await self.list_devices()
        bt_sources = [
            d for d in devices
            if d.device_type == DeviceType.SOURCE and d.is_bluetooth
        ]

        for device in bt_sources:
            if device.is_default:
                return device

        if bt_sources:
            return bt_sources[0]

        return None

    async def get_default_bluetooth_sink(self) -> Optional[AudioDevice]:
        """Obtiene el dispositivo sumidero Bluetooth por defecto."""
        devices = await self.list_devices()
        bt_sinks = [
            d for d in devices
            if d.device_type == DeviceType.SINK and d.is_bluetooth
        ]

        for device in bt_sinks:
            if device.is_default:
                return device

        if bt_sinks:
            return bt_sinks[0]

        return None

    async def set_default_source(self, node_id: str) -> None:
        """Establece un nodo como fuente de audio por defecto."""
        await self._run_wpctl(["set-default", node_id])
        self._default_source = node_id
        logger.info(f"Fuente por defecto establecida: {node_id}")

    async def set_default_sink(self, node_id: str) -> None:
        """Establece un nodo como salida de audio por defecto."""
        await self._run_wpctl(["set-default", node_id])
        self._default_sink = node_id
        logger.info(f"Salida por defecto establecida: {node_id}")

    async def set_volume(self, node_id: str, volume: float) -> None:
        """Establece el volumen de un nodo (0.0 = mudo, 1.0 = 100%, >1.0 amplifica)."""
        await self._run_wpctl(["set-volume", node_id, str(volume)])
        logger.info(f"Volumen de nodo {node_id} establecido a {volume}")

    async def auto_configure_bluetooth(self) -> tuple[Optional[str], Optional[str]]:
        """
        Detecta y configura automáticamente los dispositivos Bluetooth.
        Devuelve (source_node_id, sink_node_id).
        """
        source = await self.get_default_bluetooth_source()
        sink = await self.get_default_bluetooth_sink()

        if source:
            await self.set_default_source(source.node_id)
            logger.info(f"Fuente BT configurada: {source.description}")

        if sink:
            await self.set_default_sink(sink.node_id)
            logger.info(f"Sink BT configurado: {sink.description}")

        return (
            source.node_id if source else None,
            sink.node_id if sink else None,
        )

    def play_system_sound(self, sound_name: str = "audio-volume-change") -> None:
        """Reproduce un sonido de sistema de forma asíncrona usando pw-play."""
        sound_path = f"/usr/share/sounds/freedesktop/stereo/{sound_name}.oga"
        
        cmd = ["pw-play"]
        if self._default_sink:
            # En PipeWire, usamos --target para especificar el nodo de salida
            cmd.extend(["--target", self._default_sink])
        cmd.append(sound_path)

        try:
            logger.info(f"Reproduciendo sonido de sistema: {sound_name} (Target: {self._default_sink or 'default'})")
            # Lo lanzamos sin esperar (background)
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.warning(f"No se pudo reproducir sonido de sistema: {e}")

    async def get_status_summary(self) -> str:
        """Devuelve un resumen del estado de audio para inyección de contexto."""
        try:
            devices = await self.list_devices()
            bt_devices = [d for d in devices if d.is_bluetooth]

            if not bt_devices:
                return "No hay dispositivos Bluetooth conectados."

            summary = "Dispositivos Bluetooth:\n"
            for d in bt_devices:
                summary += f"  - {d.description} (id={d.node_id}, {'default' if d.is_default else 'no default'})\n"

            return summary
        except AudioManagerError as e:
            return f"Error obteniendo estado de audio: {e}"

    async def pause_active_players(self) -> list[str]:
        """Detecta qué reproductores de música MPRIS están activos, los pausa y los devuelve."""
        paused_players = []
        try:
            # Obtener la lista de todos los reproductores
            proc = await asyncio.create_subprocess_exec(
                "playerctl", "--list-all",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return []
            
            players = [p.strip() for p in stdout.decode().splitlines() if p.strip()]
            for player in players:
                # Consultar estado de cada reproductor
                status_proc = await asyncio.create_subprocess_exec(
                    "playerctl", "-p", player, "status",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                status_out, _ = await status_proc.communicate()
                status = status_out.decode().strip()
                
                if status == "Playing":
                    # Pausar y registrar
                    pause_proc = await asyncio.create_subprocess_exec(
                        "playerctl", "-p", player, "pause",
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    await pause_proc.wait()
                    paused_players.append(player)
                    logger.info(f"Reproductor '{player}' pausado temporalmente.")
        except Exception as e:
            logger.error(f"Error pausando reproductores activos: {e}")
        
        return paused_players

    async def resume_players(self, players: list[str]) -> None:
        """Reanuda los reproductores especificados."""
        for player in players:
            try:
                resume_proc = await asyncio.create_subprocess_exec(
                    "playerctl", "-p", player, "play",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                await resume_proc.wait()
                logger.info(f"Reproductor '{player}' reanudado.")
            except Exception as e:
                logger.error(f"Error reanudando reproductor '{player}': {e}")

