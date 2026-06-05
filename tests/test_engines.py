"""Tests de la capa de motor intercambiable (Fases 1 y 2)."""

import pytest

from src.engines.base import EngineCapabilities, InferenceEngine
from src.engines.factory import create_engine


class _DummySettings:
    def __init__(self, engine):
        self.AI_ENGINE = engine


# --- Fase 1: contrato y factoría ------------------------------------------------

def test_capabilities_defaults():
    caps = EngineCapabilities()
    assert caps.tools is True
    assert caps.vision is False and caps.audio is False and caps.gpu is False


def test_factory_invalid_engine():
    with pytest.raises(ValueError):
        create_engine(_DummySettings("foobar"))


def test_factory_default_is_litert(monkeypatch):
    """La factoría construye LiteRT por defecto (sin cargar el modelo real)."""
    import src.litert_client as lc

    class FakeLiteRT:
        name = "LiteRT"
        is_ready = True
        capabilities = EngineCapabilities(tools=True, vision=True, audio=True, gpu=True)

        def backend_label(self):
            return "GPU"

        def chat_stream(self, *a, **k): ...
        async def chat(self, *a, **k): return ""
        async def transcribe_audio(self, p): return ""
        def reset_conversation(self): ...
        def close(self): ...

    monkeypatch.setattr(lc, "LiteRTClient", FakeLiteRT)
    engine = create_engine(_DummySettings("litert"))
    assert isinstance(engine, InferenceEngine)
    assert engine.backend_label() == "GPU"


def test_factory_exllama_returns_engine(monkeypatch):
    from src.engines import exllama_engine as ee

    monkeypatch.setattr(ee.ExLlamaEngine, "_ping", lambda self: False)
    engine = create_engine(_DummySettings("exllama"))
    assert isinstance(engine, InferenceEngine)
    assert engine.capabilities.audio is False  # ExLlama no hace audio
    assert engine.backend_label() == "Desconectado"


# --- Fase 2: conversor de schema -----------------------------------------------

def test_callable_to_schema():
    from src.engines.exllama_engine import callable_to_schema

    def get_weather(city: str, unit: str = "c") -> str:
        """Obtiene el tiempo de una ciudad."""
        return ""

    s = callable_to_schema(get_weather)
    f = s["function"]
    assert s["type"] == "function"
    assert f["name"] == "get_weather"
    assert f["description"].startswith("Obtiene el tiempo")
    assert f["parameters"]["properties"]["city"] == {"type": "string"}
    assert f["parameters"]["required"] == ["city"]  # 'unit' tiene default -> opcional


# --- Fase 2: filtro de streaming (<think> / <tool_call>) ------------------------

def _feed_all(flt, chunks):
    out = []
    for c in chunks:
        out += flt.feed(c)
    out += flt.flush()
    return "".join(out)


def test_streamfilter_plain_text():
    from src.engines.exllama_engine import _StreamFilter

    flt = _StreamFilter()
    assert _feed_all(flt, ["Hola ", "mundo"]) == "Hola mundo"
    assert flt.tool_calls == []


def test_streamfilter_strips_think():
    from src.engines.exllama_engine import _StreamFilter

    flt = _StreamFilter()
    txt = _feed_all(flt, ["<think>\nrazonando\n</think>\n\n", "Respuesta"])
    assert txt.strip() == "Respuesta"
    assert flt.tool_calls == []


def test_streamfilter_captures_toolcall():
    from src.engines.exllama_engine import _StreamFilter

    flt = _StreamFilter()
    txt = _feed_all(flt, ['<tool_call>\n{"name": "get_x", "arguments": {"a": 1}}\n</tool_call>'])
    assert txt == ""
    assert flt.tool_calls == [{"name": "get_x", "arguments": {"a": 1}}]


def test_streamfilter_split_markers_across_chunks():
    from src.engines.exllama_engine import _StreamFilter

    flt = _StreamFilter()
    chunks = ["Antes <to", "ol_call>", '{"name":"f","arguments":{}}', "</tool_", "call>", " despues"]
    txt = _feed_all(flt, chunks)
    assert txt.startswith("Antes") and "despues" in txt
    assert flt.tool_calls == [{"name": "f", "arguments": {}}]


# --- Fase 2: bucle agéntico -----------------------------------------------------

@pytest.mark.asyncio
async def test_agentic_loop_executes_tool(monkeypatch):
    from src.engines import exllama_engine as ee

    monkeypatch.setattr(ee.ExLlamaEngine, "_ping", lambda self: True)
    engine = ee.ExLlamaEngine()
    engine._ready = True

    state = {"n": 0, "tool_args": []}

    async def fake_stream(messages, tool_schemas):
        state["n"] += 1
        if state["n"] == 1:
            # tool call estructurada (formato OpenAI streaming), con args partidos
            yield {"tool_calls": [{"index": 0, "function": {"name": "get_x", "arguments": '{"a":'}}]}
            yield {"tool_calls": [{"index": 0, "function": {"arguments": " 5}"}}]}
        else:
            yield {"content": "Resultado: "}
            yield {"content": "listo"}

    monkeypatch.setattr(engine, "_stream_deltas", fake_stream)

    def get_x(a: int) -> str:
        """Tool de prueba."""
        state["tool_args"].append(a)
        return "OK42"

    chunks = []
    async for c in engine.chat_stream("hola", tools=[get_x], system_prompt="sys"):
        chunks.append(c)

    assert "".join(chunks) == "Resultado: listo"  # tool_call suprimido del output
    assert state["tool_args"] == [5]               # la tool se ejecutó con a=5
    assert state["n"] == 2                          # 1 ronda de tool + respuesta final


# --- Fase 4: nombre de motor y fallback de STT por capacidades ------------------

def test_engine_names(monkeypatch):
    from src.engines import exllama_engine as ee

    # No queremos pingar a TabbyAPI en el test.
    monkeypatch.setattr(ee.ExLlamaEngine, "_ping", lambda self: False)
    assert ee.ExLlamaEngine().name == "ExLlama"


def test_stt_falls_back_to_whisper_without_engine_audio(monkeypatch):
    """STT_USE_GEMMA_AUDIO=True pero el motor no hace audio (ExLlama) -> Whisper."""
    from src import stt_engine as se

    monkeypatch.setattr(se.settings, "STT_USE_GEMMA_AUDIO", True)

    class NoAudioEngine:
        name = "ExLlama"
        capabilities = EngineCapabilities(tools=True, vision=False, audio=False, gpu=True)

    stt = se.STTEngine(litert_client=NoAudioEngine())
    assert stt._use_gemma is False


def test_stt_uses_engine_audio_when_supported(monkeypatch):
    """STT_USE_GEMMA_AUDIO=True y el motor sí hace audio (LiteRT) -> audio nativo."""
    from src import stt_engine as se

    monkeypatch.setattr(se.settings, "STT_USE_GEMMA_AUDIO", True)

    class AudioEngine:
        name = "LiteRT"
        capabilities = EngineCapabilities(tools=True, vision=True, audio=True, gpu=True)

    stt = se.STTEngine(litert_client=AudioEngine())
    assert stt._use_gemma is True
