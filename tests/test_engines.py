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


# --- Fase 3: motor Hermes (el bucle agéntico lo lleva Hermes, no Luka) ----------

def _hermes_settings(tmp_path, **over):
    """Settings mínimos para construir HermesEngine sin tener Hermes instalado."""
    class S:
        AI_ENGINE = "hermes"
        HERMES_DIR = str(tmp_path / "hermes")
        HERMES_PYTHON = ""
        HERMES_MODEL = "Qwen3.5-9B-exl3-3.00bpw"
        HERMES_TIMEOUT = 5.0
        HERMES_MAX_ITERATIONS = 4
        HERMES_MAX_TOKENS = 4096
        HERMES_ENABLED_TOOLSETS = ""
        HERMES_DISABLED_TOOLSETS = ""
        HERMES_SKIP_MEMORY = True
        EXLLAMA_BASE_URL = "http://127.0.0.1:5000"
        EXLLAMA_API_KEY = ""
    for k, v in over.items():
        setattr(S, k, v)
    return S()


def test_hermes_cumple_el_contrato(tmp_path):
    from src.engines.hermes_engine import HermesEngine

    engine = HermesEngine(_hermes_settings(tmp_path))
    assert isinstance(engine, InferenceEngine)
    assert engine.name == "Hermes"
    # Sin instalar: no está listo, pero se construye igual (no revienta el arranque).
    assert engine.is_ready is False
    assert engine.backend_label() == "Desconectado"
    assert engine.capabilities.audio is False   # el STT cae a Whisper
    assert engine.capabilities.tools is True


def test_hermes_expone_los_atributos_de_facto(tmp_path):
    """assistant_service los lee con getattr; si faltan, degrada en silencio."""
    from src.engines.hermes_engine import HermesEngine

    engine = HermesEngine(_hermes_settings(tmp_path))
    assert engine.tracks_tool_usage is True      # habilita el guardarraíl anti-invención
    assert engine.streams_clean_text is True     # el puente no emite marcadores de tools
    assert engine.leads_with_reasoning is False  # el razonamiento va por otro canal
    assert engine.last_turn_tools_used == []
    assert engine.model_label == "Qwen3.5-9B-exl3-3.00bpw"


def test_hermes_sin_instalar_avisa_en_vez_de_romper(tmp_path):
    """El turno devuelve un mensaje hablable, no una excepción."""
    import asyncio
    from src.engines.hermes_engine import HermesEngine

    engine = HermesEngine(_hermes_settings(tmp_path))

    async def run():
        return [c async for c in engine.chat_stream("hola")]

    out = "".join(asyncio.run(run()))
    assert "no está instalado" in out.lower()


def test_hermes_reconstruye_tools_del_historial():
    """Cinturón y tirantes del guardarraíl: si no llegan callbacks, se leen los mensajes.

    Es LA pieza que impide que Luka afirme acciones que no ejecutó, así que se prueba
    con la forma real que devuelve Hermes en `messages`.
    """
    from scripts.hermes_bridge import _tools_from_messages

    messages = [
        {"role": "user", "content": "ejecuta echo hola"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "terminal", "arguments": '{"command": "echo hola"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "hola"},
        {"role": "assistant", "content": "Imprimió hola"},
    ]
    assert _tools_from_messages(messages) == ["terminal"]
    assert _tools_from_messages([]) == []
    assert _tools_from_messages(None) == []


def test_factory_hermes_returns_engine(tmp_path, monkeypatch):
    from src.engines import hermes_engine as he

    monkeypatch.setattr(he, "_settings", _hermes_settings(tmp_path))
    engine = create_engine(_DummySettings("hermes"))
    assert isinstance(engine, InferenceEngine)
    assert engine.name == "Hermes"


def test_hermes_normaliza_los_nombres_de_tools_mcp():
    """Hermes prefija las tools MCP; el resto del asistente busca el nombre pelado.

    Sin esto, _TERMINAL_TOOL_NAMES no detectaría la rama de terminal y las frases de
    _FALLBACK_BY_TOOL no dispararían nunca (ambos en assistant_service).
    """
    from src.engines.hermes_engine import _luka_tool_name

    assert _luka_tool_name("mcp__luka__music_control") == "music_control"
    assert _luka_tool_name("mcp__luka__execute_system_command") == "execute_system_command"
    # Las propias de Hermes no llevan prefijo y no deben tocarse.
    assert _luka_tool_name("terminal") == "terminal"
    assert _luka_tool_name("execute_code") == "execute_code"
    # Casos raros: no romper.
    assert _luka_tool_name("mcp__luka__") == "mcp__luka__"
    assert _luka_tool_name("mcp__solo_dos") == "mcp__solo_dos"


def test_hermes_los_nombres_normalizados_casan_con_la_rama_de_terminal():
    """La normalización tiene que servir para lo que existe: el fallback de terminal."""
    from src.assistant_service import _TERMINAL_TOOL_NAMES
    from src.engines.hermes_engine import _luka_tool_name

    assert _luka_tool_name("mcp__luka__execute_system_command") in _TERMINAL_TOOL_NAMES
    assert _luka_tool_name("mcp__luka__read_terminal_screen") in _TERMINAL_TOOL_NAMES


def test_resolve_path_expande_la_virgulilla():
    """Sin esto, HERMES_DIR='~/...' acababa como '<PROJECT_ROOT>/~/...' y el motor se
    declaraba 'no instalado' por muchas veces que se instalara. Fallo silencioso."""
    from pathlib import Path

    from src.config import resolve_path, settings

    assert resolve_path("~/.asistenteia/hermes") == Path.home() / ".asistenteia/hermes"
    # Lo de siempre no cambia: relativas contra PROJECT_ROOT, absolutas intactas.
    assert resolve_path("config/certs/cert.pem") == settings.PROJECT_ROOT / "config/certs/cert.pem"
    assert resolve_path("/etc/hosts") == Path("/etc/hosts")
    assert resolve_path("") is None
    assert resolve_path(None) is None


def test_hermes_encuentra_su_instalacion_con_ruta_de_home(tmp_path, monkeypatch):
    """El motor debe darse por instalado cuando HERMES_DIR usa '~'."""
    from src.engines.hermes_engine import HermesEngine

    fake_home = tmp_path / "home"
    venv_bin = fake_home / ".asistenteia/hermes/.venv/bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    settings_obj = _hermes_settings(tmp_path, HERMES_DIR="~/.asistenteia/hermes")
    engine = HermesEngine(settings_obj)
    assert engine.hermes_dir == str(fake_home / ".asistenteia/hermes")
    assert engine.is_ready is True
