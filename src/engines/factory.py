"""Factoría de motores de inferencia.

Único punto de construcción del motor. Mantiene LiteRT como default, de modo que
sin configurar nada el comportamiento es idéntico al actual.
"""

from __future__ import annotations

import logging

from src.config import settings as _settings
from src.engines.base import InferenceEngine

logger = logging.getLogger(__name__)


def create_engine(settings=_settings) -> InferenceEngine:
    """Construye el motor según ``settings.AI_ENGINE``.

    - "litert"     -> LiteRTClient (motor local en proceso, por defecto).
    - "exllama"    -> ExLlamaEngine (sidecar TabbyAPI en la GPU).
    - "openrouter" -> OpenRouterEngine (LLM en la nube, sin VRAM).
    - "hermes"     -> HermesEngine (Hermes Agent lleva el bucle; usa el mismo sidecar).
    """
    name = (getattr(settings, "AI_ENGINE", "litert") or "litert").lower()

    if name == "litert":
        from src.litert_client import LiteRTClient

        logger.info("Motor de inferencia: LiteRT")
        return LiteRTClient()

    if name == "exllama":
        from src.engines.exllama_engine import ExLlamaEngine

        logger.info("Motor de inferencia: ExLlama (TabbyAPI)")
        return ExLlamaEngine()

    if name == "openrouter":
        from src.engines.openrouter_engine import OpenRouterEngine

        logger.info("Motor de inferencia: OpenRouter (nube)")
        return OpenRouterEngine()

    if name == "hermes":
        from src.engines.hermes_engine import HermesEngine

        logger.info("Motor de inferencia: Hermes Agent (sobre el sidecar TabbyAPI)")
        return HermesEngine()

    raise ValueError(
        f"AI_ENGINE no válido: {name!r}. "
        "Valores válidos: 'litert', 'exllama', 'openrouter', 'hermes'."
    )
