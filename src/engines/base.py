"""Contrato común de motor de inferencia (LiteRT, ExLlama, …).

Define la interfaz que consume el resto del sistema (assistant_service, stt_engine,
main) para que el motor sea intercambiable. LiteRTClient cumple este Protocol de
forma estructural (sin heredar), por lo que la introducción de esta capa es
puramente aditiva y no cambia el comportamiento existente.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Callable,
    List,
    Optional,
    Protocol,
    TYPE_CHECKING,
    runtime_checkable,
)

if TYPE_CHECKING:  # evita import circular en runtime
    from src.schema import ChatMessage


@dataclass(frozen=True)
class EngineCapabilities:
    """Modalidades soportadas por un motor. El resto del sistema actúa por
    capacidades (no por nombre de motor): p.ej. el STT cae a Whisper si
    ``audio`` es False, y /status reporta GPU según ``gpu``."""

    tools: bool = True
    vision: bool = False
    audio: bool = False
    gpu: bool = False


@runtime_checkable
class InferenceEngine(Protocol):
    """Contrato mínimo que cualquier motor debe cumplir.

    Coincide con la superficie pública que ya expone LiteRTClient hoy, más tres
    miembros que sellan fugas específicas de LiteRT (``is_ready``,
    ``backend_label`` y ``capabilities``) para que main.py no dependa de litert_lm.
    """

    @property
    def is_ready(self) -> bool:
        """True si el motor cargó y puede inferir."""
        ...

    @property
    def capabilities(self) -> EngineCapabilities:
        ...

    def backend_label(self) -> str:
        """Etiqueta legible del backend activo: 'GPU' | 'CPU' | 'Auto' | 'Desconectado'."""
        ...

    def chat_stream(
        self,
        prompt: str,
        tools: Optional[List[Callable]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[List["ChatMessage"]] = None,
        image_path: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Genera la respuesta en streaming (async generator de chunks de texto)."""
        ...

    async def chat(
        self,
        prompt: str,
        tools: Optional[List[Callable]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[List["ChatMessage"]] = None,
        image_path: Optional[str] = None,
    ) -> str:
        """Genera la respuesta completa como texto."""
        ...

    async def transcribe_audio(self, audio_path: str) -> str:
        """Transcribe audio si el motor lo soporta; '' si no."""
        ...

    def reset_conversation(self) -> None:
        """Resetea la conversación persistente / KV-cache si la hubiera."""
        ...

    def close(self) -> None:
        """Libera recursos del motor."""
        ...
