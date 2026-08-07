"""Guardarraíl anti-invención: el asistente no puede afirmar acciones que no ejecutó.

Regresión de un caso real: tras un fallo de parseo de tool call el motor reintentó SIN
herramientas y el modelo, ya incapaz de actuar, dijo "Te he abierto la página de YouTube
para Madonna". Nada se había abierto, y la frase entró en el historial, así que el turno
siguiente la imitó sin ni siquiera intentar la llamada.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.assistant_service import AssistantService
from src.schema import ChatMessage


@pytest.fixture
def engine():
    """Motor falso que puede informar de las tools ejecutadas (como LiteRT real)."""
    client = MagicMock()
    client.engine = MagicMock()
    client.tracks_tool_usage = True
    client.last_turn_tools_used = []
    client.last_turn_tools_disabled = False
    client.streams_clean_text = False
    client.leads_with_reasoning = False
    return client


@pytest.fixture
def service(engine):
    tts = MagicMock()
    tts.synthesize_only = AsyncMock()
    tts.play_audio_array = AsyncMock()
    tts.close_persistent_stream = MagicMock()
    tts.stop = MagicMock()
    stt = MagicMock()
    return AssistantService(litert_client=engine, tts_engine=tts, stt_engine=stt)


def _stream(chunks, engine, tools_ejecutadas=()):
    """chat_stream falso; simula que el motor ejecuta tools durante el stream."""
    async def gen(*args, **kwargs):
        for c in chunks:
            yield c
        for name in tools_ejecutadas:
            engine.last_turn_tools_used.append(name)
    return gen


async def _run(service, texto, historial):
    """Consume el stream y devuelve (texto_hablado, texto_mostrado).

    Los workers reales de TTS se sustituyen por uno que solo guarda la cola; se drena
    al final, cuando el generador ya ha puesto todo (incluido el None de cierre).
    """
    colas = []
    mostrado = []

    async def fake_synth(q_text, q_audio):
        colas.append(q_text)

    service._synth_worker = fake_synth
    service._play_worker = AsyncMock()

    async for chunk in service.process_transcription_stream(texto, historial):
        mostrado.append(chunk)

    # El worker de síntesis es una tarea: hay que cederle el bucle para que arranque
    # (el stream falso no tiene esperas reales que lo hagan por sí solas).
    await asyncio.sleep(0)

    hablado = []
    if colas:
        while not colas[0].empty():
            item = colas[0].get_nowait()
            if item is not None:
                hablado.append(item)
    return " ".join(hablado), "".join(mostrado)


# --- El caso que falló en producción -----------------------------------------

@pytest.mark.asyncio
async def test_afirmacion_sin_tool_no_se_habla(service, engine):
    """"Te he abierto la página de YouTube" sin ninguna tool ejecutada → no se habla."""
    engine.last_turn_tools_disabled = True   # el motor se quedó sin herramientas
    engine.chat_stream = _stream(["Te he abierto la página de YouTube para Madonna."], engine)

    historial = []
    hablado, _ = await _run(service, "pon música en YouTube", historial)

    assert "he abierto" not in hablado.lower()
    assert service._NO_ACTION_MSG in hablado


@pytest.mark.asyncio
async def test_la_mentira_no_entra_en_el_historial(service, engine):
    """Si la afirmación entra en el historial, el turno siguiente la imita."""
    engine.last_turn_tools_disabled = True
    engine.chat_stream = _stream(["Te he abierto la página de YouTube para Madonna."], engine)

    historial = []
    await _run(service, "pon música en YouTube", historial)

    respuestas = [m.content for m in historial if m.role == "assistant"]
    assert respuestas, "debería haberse guardado una respuesta"
    assert "he abierto" not in respuestas[-1].lower()
    assert respuestas[-1] == service._NO_ACTION_MSG


@pytest.mark.asyncio
async def test_afirmacion_con_tool_ejecutada_si_se_habla(service, engine):
    """El camino bueno no se rompe: si la tool corrió, la confirmación se dice tal cual."""
    engine.chat_stream = _stream(
        ["Reproduciendo 'Fear of the Dark' en YouTube."],
        engine,
        tools_ejecutadas=["play_youtube_music"],
    )

    historial = []
    hablado, _ = await _run(service, "pon Iron Maiden en YouTube", historial)

    assert "Reproduciendo" in hablado
    assert service._NO_ACTION_MSG not in hablado


@pytest.mark.asyncio
async def test_respuesta_informativa_sin_tools_se_habla_normal(service, engine):
    """Una respuesta que no afirma ninguna acción no se toca aunque no haya tools."""
    engine.chat_stream = _stream(["La capital de Francia es París, con dos millones."], engine)

    historial = []
    hablado, _ = await _run(service, "cuál es la capital de Francia", historial)

    assert "París" in hablado
    assert service._NO_ACTION_MSG not in hablado


@pytest.mark.asyncio
async def test_guardarrail_desactivado_si_el_motor_no_informa(service, engine):
    """Sin verdad de campo no se puede juzgar: mejor hablar que censurar todo turno."""
    engine.tracks_tool_usage = False
    engine.chat_stream = _stream(["Te he abierto la página de YouTube."], engine)

    historial = []
    hablado, _ = await _run(service, "pon música en YouTube", historial)

    assert "he abierto" in hablado.lower()


# --- La regex de afirmación --------------------------------------------------

@pytest.mark.parametrize("frase", [
    "Te he abierto la página de YouTube para Madonna.",
    "He lanzado Steam.",
    "He ejecutado el comando.",
    "Ya está sonando.",
    "Ya lo tienes en pantalla.",
    "Reproduciendo música.",
    "Estoy abriendo Spotify.",
    "Hecho.",
    "Listo",
])
def test_detecta_afirmaciones_de_accion(service, frase):
    assert service._claims_action(frase) is True


@pytest.mark.parametrize("frase", [
    "Son las tres y media.",
    "El procesador es un Ryzen 9.",
    "De hecho, París es la capital de Francia.",
    "Es un hecho conocido.",
    "¿Qué canción quieres escuchar?",
    "No he encontrado nada en YouTube para eso.",
    "El comando ha fallado con código 127.",
])
def test_no_confunde_respuestas_normales(service, frase):
    assert service._claims_action(frase) is False


# --- Registro de tools ejecutadas (fuente de verdad del guardarraíl) ----------
#
# Regresión seria: el registro se hacía con el `tool_event_handler` del motor. Eso
# destrozaba las RESPUESTAS de las tools —a "¿quién fue Albert Einstein?" contestaba
# "Acción ejecutada correctamente" porque el modelo solo llegaba a emitir "Albert"— y
# encima entregaba '?' en vez del nombre. Ahora se envuelven los callables, que no toca
# la tubería del motor. Estos tests fijan las dos propiedades que se rompieron.

@pytest.fixture
def cliente_litert():
    """LiteRTClient sin cargar el modelo: solo interesa _prepare_tools."""
    from src.litert_client import LiteRTClient

    client = object.__new__(LiteRTClient)
    client.last_turn_tools_used = []
    client.last_turn_tools_disabled = False
    return client


@pytest.mark.asyncio
async def test_el_wrapper_no_toca_el_resultado_de_la_tool(cliente_litert):
    """Lo que devuelve la tool debe llegar al modelo INTACTO."""
    async def web_search(query: str) -> str:
        """Busca en internet."""
        return f"resultado largo y completo para {query}"

    envuelta = cliente_litert._prepare_tools([web_search])[0]
    # Desde un hilo, como hace el motor: el wrapper despacha al bucle y espera. Llamarlo
    # desde el propio bucle se bloquearía a sí mismo.
    resultado = await asyncio.to_thread(envuelta, "Einstein")
    assert resultado == "resultado largo y completo para Einstein"


@pytest.mark.asyncio
async def test_registra_el_nombre_real_no_interrogante(cliente_litert):
    async def play_youtube_music(query: str) -> str:
        """Reproduce música."""
        return "ok"

    envuelta = cliente_litert._prepare_tools([play_youtube_music])[0]
    assert cliente_litert.last_turn_tools_used == []      # aún no se ha llamado
    await asyncio.to_thread(envuelta, "iron maiden")
    assert cliente_litert.last_turn_tools_used == ["play_youtube_music"]


@pytest.mark.asyncio
async def test_conserva_firma_y_docstring_para_el_esquema(cliente_litert):
    """El SDK introspecciona firma y docstring para construir el esquema de la tool."""
    import inspect

    async def music_control(action: str) -> str:
        """Controla la reproducción."""
        return "ok"

    envuelta = cliente_litert._prepare_tools([music_control])[0]
    assert envuelta.__name__ == "music_control"
    assert "Controla la reproducción" in envuelta.__doc__
    assert list(inspect.signature(envuelta).parameters) == ["action"]


def test_tools_sincronas_tambien_se_registran(cliente_litert):
    def tool_sincrona(x: str) -> str:
        """Una tool que no es corrutina."""
        return f"eco {x}"

    envuelta = cliente_litert._prepare_tools([tool_sincrona])[0]
    assert envuelta("hola") == "eco hola"
    assert cliente_litert.last_turn_tools_used == ["tool_sincrona"]


def test_sin_tools_devuelve_lista_vacia(cliente_litert):
    assert cliente_litert._prepare_tools(None) == []
    assert cliente_litert._prepare_tools([]) == []


def test_el_handler_del_motor_solo_con_el_flag():
    """No debe engancharse por defecto: hacerlo rompía el tool calling."""
    import inspect

    from src.litert_client import LiteRTClient

    src = inspect.getsource(LiteRTClient._create_conversation)
    assert "settings.LITERT_TOOL_EVENTS and tools" in src


# --- Fallback por palabras clave --------------------------------------------

def test_fallback_por_tool_no_por_palabras_del_usuario():
    """La frase de respaldo describe la tool que corrió, no lo que el usuario dijo."""
    from src.assistant_service import _FALLBACK_BY_TOOL, _TERMINAL_TOOL_NAMES

    assert "play_youtube_music" in _FALLBACK_BY_TOOL
    assert "YouTube" in _FALLBACK_BY_TOOL["play_youtube_music"]
    # La rama de terminal se decide por tool ejecutada, no por "dime"/"qué".
    assert "execute_system_command" in _TERMINAL_TOOL_NAMES
    assert "read_terminal_screen" in _TERMINAL_TOOL_NAMES
