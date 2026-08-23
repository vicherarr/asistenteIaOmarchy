"""Tests para src/assistant_service.py"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.assistant_service import AssistantService
from src.schema import ChatMessage


@pytest.fixture
def mock_litert():
    client = MagicMock()
    client.engine = MagicMock()
    client.chat_stream = MagicMock()
    return client


@pytest.fixture
def mock_tts():
    engine = MagicMock()
    engine.synthesize_only = AsyncMock()
    engine.play_audio_array = AsyncMock()
    engine.close_persistent_stream = MagicMock()
    engine.stop = MagicMock()
    return engine


@pytest.fixture
def mock_stt():
    engine = MagicMock()
    engine.transcribe = AsyncMock(return_value="texto transcrito")
    return engine


@pytest.fixture
def service(mock_litert, mock_tts, mock_stt):
    return AssistantService(
        litert_client=mock_litert,
        tts_engine=mock_tts,
        stt_engine=mock_stt,
    )


def test_extract_sentences_basic(service):
    """Extrae frases por puntuación básica."""
    text = "Hola mundo. ¿Cómo estás? ¡Bien! Gracias: adiós."
    sentences, remaining = service._extract_sentences(text)
    
    # ":" seguido de espacio también corta (5 frases)
    assert len(sentences) == 5
    assert sentences[0] == "Hola mundo."
    assert sentences[1] == "¿Cómo estás?"
    assert sentences[2] == "¡Bien!"
    assert sentences[3] == "Gracias:"


def test_extract_sentences_with_newlines(service):
    """Extrae frases por saltos de línea."""
    text = "Primera línea\nSegunda línea\n"
    sentences, remaining = service._extract_sentences(text)
    
    assert len(sentences) == 2


def test_extract_sentences_protects_decimals(service):
    """No corta decimales como frases separadas."""
    text = "El precio es 3.14 euros."
    sentences, remaining = service._extract_sentences(text)
    
    # "3.14" no debería separarse
    assert any("3.14" in s for s in sentences)


def test_extract_sentences_cuts_by_comma_when_long(service):
    """Corta por coma cuando el buffer supera 80 caracteres."""
    # Buffer largo (>80 chars) → debe cortar por coma
    text = "Esto es una frase muy larga que supera los ochenta caracteres, y por lo tanto debería cortarse por la coma."
    assert len(text) > 80
    sentences, remaining = service._extract_sentences(text)
    
    # Debería haber cortado por la coma
    assert len(sentences) >= 1
    assert any("," in s for s in sentences)


def test_extract_sentences_no_comma_when_short(service):
    """NO corta por coma cuando el buffer es corto (<80 chars)."""
    text = "Hola, mundo."
    assert len(text) < 80
    sentences, remaining = service._extract_sentences(text)
    
    # No debería haber cortado por coma (solo una frase)
    assert len(sentences) == 1
    assert sentences[0] == "Hola, mundo."


def test_extract_sentences_remaining(service):
    """Devuelve el texto restante sin terminar."""
    text = "Frase completa. Texto incompleto"
    sentences, remaining = service._extract_sentences(text)
    
    assert len(sentences) == 1
    assert remaining == " Texto incompleto"


def test_strip_think_removes_closed_block(service):
    """Quita un bloque <think>...</think> cerrado, deja solo la respuesta."""
    out = service._strip_think_for_tts("<think>razono mucho</think>Hola mundo")
    assert out == "Hola mundo"


def test_strip_think_drops_unclosed_block(service):
    """Un <think> abierto sin cerrar (razonamiento aún en streaming) se descarta entero."""
    assert service._strip_think_for_tts("<think>razonando todavía") == ""


def test_strip_think_holds_partial_open_tag(service):
    """Una etiqueta <think> partida entre chunks no debe hablarse a medias."""
    assert service._strip_think_for_tts("Respuesta<thi") == "Respuesta"


def test_strip_think_passthrough_without_reasoning(service):
    """Sin razonamiento, el texto pasa intacto."""
    assert service._strip_think_for_tts("Hola, ¿qué tal?") == "Hola, ¿qué tal?"


def test_strip_think_incremental_only_speaks_answer(service):
    """Simula el stream chunk a chunk: solo se habla lo posterior a </think>."""
    chunks = ["<th", "ink>razo", "no mu", "cho</thi", "nk>Ho", "la, ¿qué", " tal?"]
    acc, fed, spoken = "", 0, []
    for c in chunks:
        acc += c
        sp = service._strip_think_for_tts(acc)
        if len(sp) > fed:
            spoken.append(sp[fed:])
            fed = len(sp)
    assert "".join(spoken) == "Hola, ¿qué tal?"


def test_strip_think_removes_channel_block(service):
    """Quita el razonamiento de Gemma-4 delimitado por <|channel>...<channel|>."""
    out = service._strip_think_for_tts("<|channel>razono mucho<channel|>Hola mundo")
    assert out == "Hola mundo"


def test_strip_think_drops_unclosed_channel(service):
    """Un <|channel> abierto sin cerrar (razonamiento en streaming) se descarta."""
    assert service._strip_think_for_tts("<|channel>razonando todavía") == ""


def test_strip_think_holds_partial_channel_tag(service):
    """Un marcador <|channel> partido entre chunks no debe hablarse a medias."""
    assert service._strip_think_for_tts("Respuesta<|chan") == "Respuesta"


def test_strip_think_channel_incremental(service):
    """Stream con razonamiento de canal: solo se habla lo posterior al cierre."""
    chunks = ["<|channel>El usuario quiere música.", " Usaré funk.<channel|>",
              "Ya está, ", "funk sonando."]
    acc, fed, spoken = "", 0, []
    for c in chunks:
        acc += c
        sp = service._strip_think_for_tts(acc)
        if len(sp) > fed:
            spoken.append(sp[fed:])
            fed = len(sp)
    assert "".join(spoken) == "Ya está, funk sonando."


@pytest.mark.asyncio
async def test_synth_worker_processes_sentences(service, mock_tts):
    """El synth worker sintetiza frases y las encola como audio."""
    queue_text = asyncio.Queue()
    queue_audio = asyncio.Queue()
    
    # Encolar frases y señal de fin
    await queue_text.put("Hola mundo.")
    await queue_text.put("Adiós.")
    await queue_text.put(None)
    
    # Mock: synthesize_only devuelve un array numpy
    import numpy as np
    mock_tts.synthesize_only.return_value = np.array([0.1, 0.2])
    
    await service._synth_worker(queue_text, queue_audio)
    
    # Verificar que se sintetizaron ambas frases
    assert mock_tts.synthesize_only.call_count == 2
    
    # Verificar que se encolaron 2 arrays de audio y la señal None al final (total 3)
    assert queue_audio.qsize() == 3


@pytest.mark.asyncio
async def test_synth_worker_handles_error(service, mock_tts):
    """El synth worker continúa aunque una síntesis falle."""
    queue_text = asyncio.Queue()
    queue_audio = asyncio.Queue()
    
    await queue_text.put("Frase válida.")
    await queue_text.put("Frase con error.")
    await queue_text.put("Otra frase válida.")
    await queue_text.put(None)
    
    # Primera y tercera OK, segunda falla
    import numpy as np
    call_count = 0
    async def mock_synthesize(text):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise Exception("Error de síntesis")
        return np.array([0.1])
    
    mock_tts.synthesize_only = mock_synthesize
    
    await service._synth_worker(queue_text, queue_audio)
    
    # 2 arrays encolados y la señal None al final (total 3)
    assert queue_audio.qsize() == 3


@pytest.mark.asyncio
async def test_play_worker_reproduces_audio(service, mock_tts):
    """El play worker reproduce arrays de audio."""
    queue_audio = asyncio.Queue()
    
    import numpy as np
    await queue_audio.put(np.array([0.1, 0.2]))
    await queue_audio.put(np.array([0.3, 0.4]))
    await queue_audio.put(None)
    
    await service._play_worker(queue_audio)
    
    # Verificar que se reprodujeron ambos arrays
    assert mock_tts.play_audio_array.call_count == 2
    
    # Verificar que se cerró el stream persistente
    mock_tts.close_persistent_stream.assert_called_once()


@pytest.mark.asyncio
async def test_play_worker_handles_error(service, mock_tts):
    """El play worker continúa aunque una reproducción falle."""
    queue_audio = asyncio.Queue()
    
    import numpy as np
    await queue_audio.put(np.array([0.1]))
    await queue_audio.put(np.array([0.2]))  # Esta fallará
    await queue_audio.put(np.array([0.3]))
    await queue_audio.put(None)
    
    call_count = 0
    async def mock_play(audio):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise Exception("Error de reproducción")
    
    mock_tts.play_audio_array = mock_play
    
    # No debería lanzar excepción
    await service._play_worker(queue_audio)
    
    # Se intentaron reproducir los 3
    assert call_count == 3


@pytest.mark.asyncio
async def test_play_worker_avisa_al_dispositivo_al_agotarse(service, mock_tts):
    """La voz que salió hacia el satélite se cierra con el aviso de fin, venga el
    turno de donde venga: sin él el aparato se queda «hablando» y sordo hasta el
    timeout del firmware."""
    import numpy as np

    enviado = []
    fin = asyncio.Event()

    async def sink(audio, rate):
        enviado.append((audio, rate))

    async def sink_end():
        fin.set()

    service.audio_sink = sink
    service.audio_sink_end = sink_end
    service.audio_target = "both"

    queue_audio = asyncio.Queue()
    await queue_audio.put(np.array([0.1, 0.2]))
    await queue_audio.put(None)

    await service._play_worker(queue_audio)

    assert len(enviado) == 1, "el audio no salió hacia el dispositivo"
    assert fin.is_set(), "el dispositivo no recibió el aviso de fin de audio"
    # Y el PC sigue sonando como siempre (target "both").
    mock_tts.play_audio_array.assert_awaited_once()


@pytest.mark.asyncio
async def test_play_worker_cancelado_tambien_cierra_el_audio(service, mock_tts):
    """Cancelar (/cancel, otra petición que pisa, barge-in) no puede dejar al
    satélite «hablando» hasta el timeout del firmware."""
    import numpy as np

    llamado = asyncio.Event()
    fin = asyncio.Event()

    async def sink(_audio, _rate):
        llamado.set()

    async def sink_end():
        fin.set()

    service.audio_sink = sink
    service.audio_sink_end = sink_end
    service.audio_target = "both"

    queue_audio = asyncio.Queue()
    await queue_audio.put(np.array([0.1]))

    task = asyncio.create_task(service._play_worker(queue_audio))
    # Que procese el primer audio y se quede esperando más: ahí es donde llega
    # la cancelación en la vida real.
    await asyncio.wait_for(llamado.wait(), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fin.is_set(), "una cancelación dejó al dispositivo sin fin de audio"


@pytest.mark.asyncio
async def test_play_worker_sin_dispositivo_no_avisa_ni_envia(service, mock_tts):
    """Con salida solo por el PC no se envía audio ni fin de audio al satélite."""
    import numpy as np

    enviado = []
    fin = asyncio.Event()

    async def sink(audio, _rate):
        enviado.append(audio)

    async def sink_end():
        fin.set()

    service.audio_sink = sink
    service.audio_sink_end = sink_end
    service.audio_target = "pc"

    queue_audio = asyncio.Queue()
    await queue_audio.put(np.array([0.1]))
    await queue_audio.put(None)

    await service._play_worker(queue_audio)

    assert enviado == []
    assert not fin.is_set()
    mock_tts.play_audio_array.assert_awaited_once()


@pytest.mark.skip(reason="Loop multimodal desactivado - analyze_screen no está registrado en self.tools")
@pytest.mark.asyncio
async def test_process_transcription_stream_multimodal_second_pass(service, mock_litert):
    """VER: Loop multimodal desactivado según CONTEXT.md sección 10.1 y 15.20.
    Este test se mantiene como referencia histórica pero está desactivado."""
    pass


# --- Fase 4.1: fallback "significativo" según el tipo de stream del motor --------

def test_meaningful_response_clean_stream_accepts_short():
    """Motor de stream limpio (ExLlama): respuestas cortas válidas se aceptan."""
    f = AssistantService._is_meaningful_response
    assert f("42", clean_stream=True) is True
    assert f("París", clean_stream=True) is True
    assert f("   ", clean_stream=True) is False   # vacío sigue sin valer
    assert f("", clean_stream=True) is False


def test_meaningful_response_litert_requires_length():
    """LiteRT (fuga tool calls): se exige longitud mínima para descartar residuos."""
    f = AssistantService._is_meaningful_response
    assert f("42", clean_stream=False) is False           # corto -> fallback (como antes)
    assert f("Una respuesta más larga y real.", clean_stream=False) is True


def test_engine_streams_clean_text_flags(monkeypatch):
    """ExLlama declara stream limpio; LiteRT no."""
    from src.engines import exllama_engine as ee
    monkeypatch.setattr(ee.ExLlamaEngine, "_ping", lambda self: False)
    assert ee.ExLlamaEngine().streams_clean_text is True


# --- Punto 2: persona vs enrutado ----------------------------------------------------
# El árbol de decisión de system_prompt.txt enruta por NOMBRE de tool
# ("execute_system_command", "read_terminal_screen"...). Eso solo sirve para un motor que
# recibe esas tools de Luka. Hermes trae las suyas y lleva su propio bucle, así que el
# árbol le manda llamar a herramientas que no tiene delante. Se le da persona y voz.

def test_motor_normal_recibe_el_arbol_de_decision(service, mock_litert):
    """Sin owns_agent_loop: el prompt de siempre, con su árbol. Nada cambia."""
    mock_litert.owns_agent_loop = False
    prompt = service._load_system_prompt()

    assert "execute_system_command" in prompt      # el árbol sigue ahí
    assert "ÁRBOL DE DECISIÓN" in prompt
    assert "FECHA Y HORA ACTUAL" in prompt         # _now_context se sigue añadiendo


def test_hermes_recibe_persona_sin_enrutado(service, mock_litert):
    """Con owns_agent_loop: persona y voz, sin un solo nombre de tool de Luka."""
    mock_litert.owns_agent_loop = True
    prompt = service._load_system_prompt()

    assert "ÁRBOL DE DECISIÓN" not in prompt
    for enrutado in ("execute_system_command", "read_terminal_screen",
                     "play_specific_music", "analyze_screen", "gmail_manager",
                     "calendar_manager"):
        assert enrutado not in prompt, f"{enrutado} es enrutado: no va en el de persona"

    # Lo que sí tiene que llevar: identidad, que se pronuncia, y no inventarse acciones.
    assert "Luka" in prompt
    assert "PRONUNCIA" in prompt
    assert "NUNCA AFIRMES UNA ACCIÓN QUE NO HAYAS EJECUTADO" in prompt
    assert "FECHA Y HORA ACTUAL" in prompt


def test_hermes_avisa_de_que_no_tiene_correo_ni_agenda(service, mock_litert):
    """Gmail/Calendar salen del MCP por privacidad; el prompt no debe prometerlos."""
    mock_litert.owns_agent_loop = True
    prompt = service._load_system_prompt()
    assert "No tienes acceso al correo ni a la agenda" in prompt


def test_el_modo_enfocado_se_aplica_a_los_dos_prompts(service, mock_litert):
    """La selección de tools de la UI se sigue respetando con cualquier motor."""
    service.active_tool_names = {"take_screenshot"}
    for owns in (False, True):
        mock_litert.owns_agent_loop = owns
        assert "[MODO ENFOCADO]" in service._load_system_prompt()


def test_hermes_engine_declara_owns_agent_loop():
    """El flag que dispara todo lo anterior."""
    from src.engines.hermes_engine import HermesEngine
    assert HermesEngine.owns_agent_loop.fget(HermesEngine.__new__(HermesEngine)) is True


def test_los_demas_motores_no_lo_declaran(monkeypatch):
    """getattr(..., False) -> se quedan con el árbol de siempre. Retrocompatible."""
    from src.engines import exllama_engine as ee
    from src.engines import openrouter_engine as oe
    monkeypatch.setattr(ee.ExLlamaEngine, "_ping", lambda self: False)
    assert getattr(ee.ExLlamaEngine(), "owns_agent_loop", False) is False
    assert getattr(oe.OpenRouterEngine, "owns_agent_loop", False) is False
