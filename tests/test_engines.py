"""Tests de la capa de motor intercambiable (Fase 1)."""

import pytest

from src.engines.base import EngineCapabilities, InferenceEngine
from src.engines.factory import create_engine


class _DummySettings:
    def __init__(self, engine):
        self.AI_ENGINE = engine


def test_capabilities_defaults():
    caps = EngineCapabilities()
    assert caps.tools is True
    assert caps.vision is False and caps.audio is False and caps.gpu is False


def test_factory_exllama_not_implemented_yet():
    with pytest.raises(NotImplementedError):
        create_engine(_DummySettings("exllama"))


def test_factory_invalid_engine():
    with pytest.raises(ValueError):
        create_engine(_DummySettings("foobar"))


def test_factory_default_is_litert(monkeypatch):
    """Sin tocar nada, la factoría construye LiteRT (default retrocompatible).

    No cargamos el modelo real: parcheamos LiteRTClient por un doble que cumple
    el contrato estructural.
    """
    import src.litert_client as lc

    class FakeLiteRT:
        is_ready = True
        capabilities = EngineCapabilities(tools=True, vision=True, audio=True, gpu=True)

        def backend_label(self):
            return "GPU"

        def chat_stream(self, *a, **k):
            ...

        async def chat(self, *a, **k):
            return ""

        async def transcribe_audio(self, p):
            return ""

        def reset_conversation(self):
            ...

        def close(self):
            ...

    monkeypatch.setattr(lc, "LiteRTClient", FakeLiteRT)
    engine = create_engine(_DummySettings("litert"))
    assert isinstance(engine, InferenceEngine)
    assert engine.is_ready is True
    assert engine.backend_label() == "GPU"
