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

    - "litert"  -> LiteRTClient (motor actual, por defecto).
    - "exllama" -> Fase 2 (aún no implementado).
    """
    name = (getattr(settings, "AI_ENGINE", "litert") or "litert").lower()

    if name == "litert":
        from src.litert_client import LiteRTClient

        logger.info("Motor de inferencia: LiteRT")
        return LiteRTClient()

    if name == "exllama":
        raise NotImplementedError(
            "El motor 'exllama' (ExLlamaV3 vía TabbyAPI) llega en la Fase 2."
        )

    raise ValueError(
        f"AI_ENGINE no válido: {name!r}. Valores válidos: 'litert', 'exllama'."
    )
