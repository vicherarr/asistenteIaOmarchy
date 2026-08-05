"""Protocolo binario entre el asistente y los dispositivos satélite (ESP32-S3).

Formato deliberadamente mínimo: **1 byte de tipo + carga útil**. Los tipos con el
bit alto puesto (`0x8x`) van del servidor al dispositivo; los demás, al revés.
Las tramas de control llevan JSON en UTF-8; las de audio, PCM16 little-endian sin
cabecera (el formato se acuerda en el HELLO y no cambia durante la sesión).

Está en un módulo aparte, sin dependencias de FastAPI ni de red, para poder
probarlo entero con tests normales: un fallo de encuadre en un dispositivo
empotrado es de los que cuesta horas diagnosticar desde el otro lado del cable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# --- Dispositivo -> Servidor ---
HELLO = 0x01          # {device, fw, sample_rate, channels}: abre la sesión
AUDIO = 0x02          # PCM16LE del micrófono
END = 0x03            # fin del turno de habla: procesa lo acumulado
CANCEL = 0x04         # aborta el turno en curso
PING = 0x05           # latido
TELEMETRY = 0x06      # {rssi, uptime_s, free_heap}: latido con diagnóstico

# --- Servidor -> Dispositivo ---
STATE = 0x81          # {state}: idle|listening|thinking|speaking
TRANSCRIPT = 0x82     # {text}: lo que el STT entendió
REPLY = 0x83          # {text}: la respuesta de Luka, para mostrarla
TTS_AUDIO = 0x84      # PCM16LE de la voz de Luka
TTS_END = 0x85        # fin de la respuesta hablada
ERROR = 0x86          # {code, message}
PONG = 0x87           # respuesta al latido

# Nombres legibles, para logs y mensajes de error que se entiendan.
NAMES: dict[int, str] = {
    HELLO: "HELLO", AUDIO: "AUDIO", END: "END", CANCEL: "CANCEL", PING: "PING",
    TELEMETRY: "TELEMETRY",
    STATE: "STATE", TRANSCRIPT: "TRANSCRIPT", REPLY: "REPLY",
    TTS_AUDIO: "TTS_AUDIO", TTS_END: "TTS_END", ERROR: "ERROR", PONG: "PONG",
}

# Estados que el servidor comunica al dispositivo. Coinciden con los que pinta el
# anillo de LEDs del firmware.
STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"

# Tope de una trama entrante. Evita que un dispositivo defectuoso (o alguien con
# el token) haga crecer la memoria del servidor sin límite.
MAX_FRAME_BYTES = 64 * 1024


class ProtocolError(ValueError):
    """Trama mal formada. El servidor responde con ERROR y cierra la sesión."""


@dataclass(frozen=True)
class Frame:
    """Una trama decodificada: su tipo y su carga útil en crudo."""

    kind: int
    payload: bytes

    @property
    def name(self) -> str:
        return NAMES.get(self.kind, f"0x{self.kind:02x}")

    def json(self) -> dict[str, Any]:
        """Interpreta la carga útil como JSON. Vacía => diccionario vacío.

        Un dispositivo puede mandar HELLO sin cuerpo; tratarlo como `{}` es más
        útil que reventar, porque los valores tienen defaults razonables.
        """
        if not self.payload:
            return {}
        try:
            value = json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ProtocolError(f"{self.name}: la carga útil no es JSON válido: {e}") from e
        if not isinstance(value, dict):
            raise ProtocolError(f"{self.name}: se esperaba un objeto JSON, no {type(value).__name__}")
        return value


def decode(data: bytes) -> Frame:
    """Decodifica una trama binaria recibida del dispositivo."""
    if not data:
        raise ProtocolError("trama vacía")
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError(f"trama de {len(data)} bytes; el máximo es {MAX_FRAME_BYTES}")
    return Frame(kind=data[0], payload=data[1:])


def encode(kind: int, payload: bytes = b"") -> bytes:
    """Construye una trama binaria para enviar al dispositivo."""
    return bytes([kind]) + payload


def encode_json(kind: int, **fields: Any) -> bytes:
    """Construye una trama de control con cuerpo JSON."""
    body = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return encode(kind, body.encode("utf-8"))


def state(name: str) -> bytes:
    return encode_json(STATE, state=name)


def error(code: str, message: str) -> bytes:
    return encode_json(ERROR, code=code, message=message)
