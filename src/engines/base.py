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
    def name(self) -> str:
        """Nombre legible del motor para la UI/estado: 'LiteRT' | 'ExLlama'."""
        ...

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


# --- Rasgos OPCIONALES ---------------------------------------------------------------
# Fuera del Protocol a propósito. Es `runtime_checkable`, así que isinstance() comprueba
# que estén TODOS sus miembros: meter aquí algo que solo declara un motor haría que los
# demás dejaran de validar (lo comprueba tests/test_engines.py). Se leen siempre con
# `getattr(motor, "<rasgo>", <defecto>)`, y quien no lo declare se comporta como siempre.
#
#   streams_clean_text   (bool, False)  El stream ya viene sin llamadas ni marcadores de
#                                       tool, así que no hay que filtrarlo antes del TTS.
#   leads_with_reasoning (bool, False)  El contenido abre con el razonamiento y lo cierra
#                                       un </think>; el TTS espera a pasarlo.
#   tracks_tool_usage    (bool, False)  El motor sabe de primera mano qué tools corrieron
#                                       (last_turn_tools_used), verdad de campo para el
#                                       guardarraíl anti-invención.
#   owns_agent_loop      (bool, False)  El motor ES un agente: lleva él el bucle con SU
#                                       catálogo, en vez de recibir las tools de Luka e
#                                       iterar assistant_service. Solo Hermes hoy.
#
#                                       Manda en qué system prompt se carga. El de Luka
#                                       (config/system_prompt.txt) es un árbol de decisión
#                                       que enruta por nombre de tool ("comando de
#                                       terminal -> execute_system_command"), y eso solo
#                                       sirve para quien recibe ESAS tools. A un motor con
#                                       las suyas se le estaría mandando llamar a cosas
#                                       que no tiene delante: medido el 23/08/2026, dos
#                                       turnos enteros buscando execute_system_command sin
#                                       ejecutar nada. Con True se carga
#                                       config/system_prompt_hermes.txt, solo persona y
#                                       voz; el enrutado es de quien lleve el bucle.
#                                       Ver AssistantService._load_system_prompt.
