"""Módulo de gestión de audio Bluetooth vía PipeWire/WirePlumber."""

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

    def _run_wpctl(self, args: list[str]) -> str:
        """Ejecuta un comando wpctl y devuelve su salida."""
        try:
            result = subprocess.run(
                ["wpctl"] + args,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                raise AudioManagerError(f"wpctl falló: {result.stderr.strip()}")
            return result.stdout
        except FileNotFoundError:
            raise AudioManagerError("wpctl no encontrado. Instalar wireplumber.")
        except subprocess.TimeoutExpired:
            raise AudioManagerError("Timeout ejecutando wpctl")

    def list_devices(self) -> list[AudioDevice]:
        """Lista todos los dispositivos de audio disponibles."""
        output = self._run_wpctl(["status"])
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

    def get_default_bluetooth_source(self) -> Optional[AudioDevice]:
        """Obtiene el dispositivo fuente Bluetooth por defecto."""
        devices = self.list_devices()
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

    def get_default_bluetooth_sink(self) -> Optional[AudioDevice]:
        """Obtiene el dispositivo sumidero Bluetooth por defecto."""
        devices = self.list_devices()
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

    def set_default_source(self, node_id: str) -> None:
        """Establece un nodo como fuente de audio por defecto."""
        self._run_wpctl(["set-default", node_id])
        self._default_source = node_id
        logger.info(f"Fuente por defecto establecida: {node_id}")

    def set_default_sink(self, node_id: str) -> None:
        """Establece un nodo como salida de audio por defecto."""
        self._run_wpctl(["set-default", node_id])
        self._default_sink = node_id
        logger.info(f"Salida por defecto establecida: {node_id}")

    def auto_configure_bluetooth(self) -> tuple[Optional[str], Optional[str]]:
        """
        Detecta y configura automáticamente los dispositivos Bluetooth.
        Devuelve (source_node_id, sink_node_id).
        """
        source = self.get_default_bluetooth_source()
        sink = self.get_default_bluetooth_sink()

        if source:
            self.set_default_source(source.node_id)
            logger.info(f"Fuente BT configurada: {source.description}")

        if sink:
            self.set_default_sink(sink.node_id)
            logger.info(f"Sink BT configurado: {sink.description}")

        return (
            source.node_id if source else None,
            sink.node_id if sink else None,
        )

    def get_status_summary(self) -> str:
        """Devuelve un resumen del estado de audio para inyección de contexto."""
        try:
            devices = self.list_devices()
            bt_devices = [d for d in devices if d.is_bluetooth]

            if not bt_devices:
                return "No hay dispositivos Bluetooth conectados."

            summary = "Dispositivos Bluetooth:\n"
            for d in bt_devices:
                summary += f"  - {d.description} (id={d.node_id}, {'default' if d.is_default else 'no default'})\n"

            return summary
        except AudioManagerError as e:
            return f"Error obteniendo estado de audio: {e}"
