"""Tests para la reproducción en YouTube y el control de música agnóstico.

El invariante que se protege aquí: una tool NUNCA devuelve un mensaje de éxito sin
haber comprobado el efecto real. Si miente la tool, miente el asistente por voz.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.browser.youtube import youtube_control, youtube_play, youtube_search
from src.command_executor import MUSIC_CONTROL_MAP, music_control


def _page(playing: bool, muted: bool = False, consent: bool = False):
    """Página falsa: el estado del reproductor se sirve desde el JS de sondeo."""
    page = MagicMock()

    async def evaluate(js, *args):
        if "getPlayerState" in js:                       # _JS_STATE
            return {"state": 1 if playing else 2, "paused": not playing,
                    "time": 3.0 if playing else 0.0, "muted": muted, "hasVideo": True}
        if "ytd-consent-bump" in js:                     # _JS_HAS_CONSENT / dismiss
            return consent
        if "h1.ytd-watch-metadata" in js:                # _JS_TITLE
            return ""
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    page.frames = []
    page.keyboard.press = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=MagicMock(first=locator))
    return page


# --- youtube_play ------------------------------------------------------------

@pytest.mark.asyncio
async def test_play_sin_resultados_no_afirma_exito():
    """Si yt-dlp no encuentra nada, se dice; nunca 'reproduciendo'."""
    with patch("src.browser.youtube.youtube_search", AsyncMock(return_value=None)):
        result = await youtube_play(_page(True), "askdjhaskjdh")

    assert "No he encontrado" in result
    assert "Reproduciendo" not in result


@pytest.mark.asyncio
async def test_play_video_en_pausa_dice_la_verdad():
    """Si el <video> nunca arranca, el mensaje lo refleja en vez de fingir éxito."""
    page = _page(playing=False)
    with patch("src.browser.youtube.youtube_search",
               AsyncMock(return_value=("abcdefghijk", "Fear of the Dark"))), \
         patch("src.browser.navigation.browser_navigate", AsyncMock(return_value="ok")), \
         patch("src.browser.youtube._PLAYBACK_TIMEOUT", 0.3):
        result = await youtube_play(page, "iron maiden fear of the dark")

    assert "no ha arrancado" in result
    assert "Reproduciendo" not in result


@pytest.mark.asyncio
async def test_play_muteado_no_cuenta_como_exito():
    """Un vídeo 'reproduciéndose' en silencio no es música: el usuario no oye nada."""
    page = _page(playing=True, muted=True)
    with patch("src.browser.youtube.youtube_search",
               AsyncMock(return_value=("abcdefghijk", "Fear of the Dark"))), \
         patch("src.browser.navigation.browser_navigate", AsyncMock(return_value="ok")), \
         patch("src.browser.youtube._PLAYBACK_TIMEOUT", 0.3):
        result = await youtube_play(page, "iron maiden")

    assert "silencio" in result
    assert "Reproduciendo" not in result


@pytest.mark.asyncio
async def test_play_confirmado_solo_si_el_video_suena():
    page = _page(playing=True)
    with patch("src.browser.youtube.youtube_search",
               AsyncMock(return_value=("abcdefghijk", "Fear of the Dark"))), \
         patch("src.browser.navigation.browser_navigate", AsyncMock(return_value="ok")):
        result = await youtube_play(page, "iron maiden fear of the dark")

    assert "Reproduciendo" in result
    assert "Fear of the Dark" in result


@pytest.mark.asyncio
async def test_play_navega_a_la_mix_para_tener_cola():
    """Se navega a la radio (list=RD...), no al vídeo suelto: si no, 'siguiente' no
    tiene a dónde ir y la música se para al acabar el tema."""
    navigate = AsyncMock(return_value="ok")
    with patch("src.browser.youtube.youtube_search",
               AsyncMock(return_value=("abcdefghijk", "Tema"))), \
         patch("src.browser.navigation.browser_navigate", navigate):
        await youtube_play(_page(True), "algo")

    url = navigate.await_args.args[1]
    assert "watch?v=abcdefghijk" in url
    assert "list=RDabcdefghijk" in url


@pytest.mark.asyncio
async def test_play_query_vacia_no_navega():
    result = await youtube_play(_page(True), "   ")
    assert "Error" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("salida", [
    b"corto|Titulo\n",                      # menos de 11 caracteres
    b"ERROR: no such video|Titulo\n",       # espacios: no es un id
    b"sin-separador\n",                     # falta el '|'
    b"",                                    # sin resultados
])
async def test_search_rechaza_salida_no_valida(salida):
    """Una salida que no es un id de YouTube no debe llegar a construir una URL."""
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(salida, b""))
    with patch("shutil.which", return_value="/usr/bin/yt-dlp"), \
         patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await youtube_search("algo") is None


@pytest.mark.asyncio
async def test_search_devuelve_id_y_titulo():
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"B_HSa1dEL9s|For Whom The Bell Tolls\n", b""))
    with patch("shutil.which", return_value="/usr/bin/yt-dlp"), \
         patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await youtube_search("metallica") == ("B_HSa1dEL9s", "For Whom The Bell Tolls")


# --- Diálogo de consentimiento -----------------------------------------------

@pytest.mark.asyncio
async def test_consentimiento_se_rechaza_y_desbloquea():
    """Mientras el modal está abierto se come el teclado y el vídeo no suena.

    Regresión: el aria-label del botón es "Rechazar el uso de cookies...", no
    "Rechazar todo", y vive en shadow DOM, así que buscarlo por nombre accesible
    fallaba y la música nunca arrancaba.
    """
    from src.browser.youtube import _dismiss_consent

    page = MagicMock()
    page.frames = []
    page.wait_for_load_state = AsyncMock()
    llamadas = []

    async def evaluate(js, *args):
        llamadas.append(js)
        if "getBoundingClientRect" in js:          # _JS_HAS_CONSENT
            return True
        if "prioridad" in js:                      # _JS_DISMISS_CONSENT
            return "rechazar todo"
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    assert await _dismiss_consent(page) is True
    # Se pulsa RECHAZAR, no aceptar: para poner música no hay que consentir nada.
    assert any("rechazar todo" in js for js in llamadas if isinstance(js, str))


@pytest.mark.asyncio
async def test_sin_modal_no_toca_nada():
    from src.browser.youtube import _dismiss_consent

    page = MagicMock()
    page.frames = []
    page.evaluate = AsyncMock(return_value=False)
    assert await _dismiss_consent(page) is False


def test_prioridad_de_botones_de_consentimiento():
    """El orden importa: rechazar antes que aceptar."""
    from src.browser.youtube import _JS_DISMISS_CONSENT

    orden = _JS_DISMISS_CONSENT.split("prioridad = ")[1].split("]")[0]
    assert orden.index("rechazar todo") < orden.index("aceptar todo")
    assert orden.index("reject all") < orden.index("accept all")


# --- youtube_control ---------------------------------------------------------

@pytest.mark.asyncio
async def test_control_pausa_lo_ya_parado():
    assert "ya estaba parada" in await youtube_control(_page(playing=False), "pause")


@pytest.mark.asyncio
async def test_control_play_lo_ya_sonando():
    assert "Ya está sonando" in await youtube_control(_page(playing=True), "play")


@pytest.mark.asyncio
async def test_control_next_sin_sonido_lo_dice():
    """Tras Shift+N el vídeo nuevo puede quedarse parado: se avisa, no se finge."""
    page = _page(playing=False)
    with patch("src.browser.youtube._PLAYBACK_TIMEOUT", 0.3):
        result = await youtube_control(page, "next")
    assert "no está sonando" in result
    page.keyboard.press.assert_any_await("Shift+N")


@pytest.mark.asyncio
async def test_control_accion_desconocida():
    assert "Error" in await youtube_control(_page(playing=True), "formatea")


# --- music_control -----------------------------------------------------------

@pytest.mark.asyncio
async def test_control_sin_reproductores():
    with patch("src.command_executor._active_player", AsyncMock(return_value=None)):
        assert await music_control("next") == "No hay nada reproduciéndose."


@pytest.mark.asyncio
async def test_control_accion_invalida():
    result = await music_control("borra todo")
    assert "no es una acción" in result


@pytest.mark.asyncio
async def test_control_traduce_el_vocabulario_hablado():
    """'siguiente' y 'pausa' llegan en español desde el modelo."""
    assert MUSIC_CONTROL_MAP["siguiente"] == "next"
    assert MUSIC_CONTROL_MAP["siguiente canción"] == "next"
    assert MUSIC_CONTROL_MAP["anterior"] == "previous"
    assert MUSIC_CONTROL_MAP["para"] == "pause"
    assert MUSIC_CONTROL_MAP["reanuda"] == "play"

    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=[(True, ""), (True, "Playing")])
    with patch("src.command_executor._active_player", AsyncMock(return_value="spotify")), \
         patch("src.command_executor.CommandExecutor", return_value=executor):
        result = await music_control("siguiente")

    assert executor.execute.await_args_list[0].args[0] == "playerctl --player=spotify next"
    assert result == "Siguiente canción."


@pytest.mark.asyncio
async def test_control_pausa_que_no_pausa_no_finge():
    """Si tras mandar pause el reproductor sigue en Playing, se dice."""
    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=[(True, ""), (True, "Playing")])
    with patch("src.command_executor._active_player", AsyncMock(return_value="spotify")), \
         patch("src.command_executor.CommandExecutor", return_value=executor):
        result = await music_control("pause")

    assert "sigue sonando" in result


@pytest.mark.asyncio
async def test_control_pausa_correcta():
    executor = MagicMock()
    executor.execute = AsyncMock(side_effect=[(True, ""), (True, "Paused")])
    with patch("src.command_executor._active_player", AsyncMock(return_value="spotify")), \
         patch("src.command_executor.CommandExecutor", return_value=executor):
        assert await music_control("pause") == "Música pausada."


@pytest.mark.asyncio
async def test_active_player_prefiere_el_que_suena():
    """El reproductor elegido es el que está sonando, no siempre Spotify: con música
    en YouTube (Chromium), hablarle a Spotify no hace nada."""
    from src.command_executor import _active_player

    executor = MagicMock()

    async def execute(cmd):
        if "--list-all" in cmd:
            return True, "spotify\nchromium.instance123\n"
        if "spotify status" in cmd:
            return True, "Paused"
        return True, "Playing"

    executor.execute = AsyncMock(side_effect=execute)
    with patch("src.command_executor.CommandExecutor", return_value=executor):
        assert await _active_player() == "chromium.instance123"


# --- Enrutado a YouTube (se hace en código, no en el prompt) ------------------
#
# El árbol de decisión del system prompt se queda INTACTO: medirlo demostró que este
# modelo no tolera editarlo. Cualquier variante del punto 3 o del 4 rompía llamadas sin
# relación con la música — "¿qué hora es?" pasó de 3/3 a 0/15 escribiendo la llamada
# como texto. Así que "en YouTube" lo detecta play_specific_music, adonde el prompt ya
# enruta, y delega.

@pytest.mark.asyncio
@pytest.mark.parametrize("peticion,esperado", [
    ("iron maiden en youtube", "iron maiden"),
    ("Fear of the Dark en YouTube", "Fear of the Dark"),
    ("el vídeo de la cabra", "la cabra"),
])
async def test_peticion_de_youtube_va_al_navegador(peticion, esperado):
    from src.command_executor import play_specific_music

    with patch("src.command_executor.play_youtube_music",
               AsyncMock(return_value="Reproduciendo.")) as yt:
        await play_specific_music(peticion)

    # Se quita "en youtube" de la búsqueda: si no, contamina el término.
    assert yt.await_args.args[0] == esperado.lower()


@pytest.mark.asyncio
async def test_musica_normal_sigue_yendo_a_spotify():
    """Sin mencionar YouTube, el comportamiento es el de siempre."""
    from src.command_executor import play_specific_music

    with patch("src.command_executor.play_youtube_music", AsyncMock()) as yt, \
         patch("src.command_executor.web_search", AsyncMock(return_value="")), \
         patch("src.command_executor.CommandExecutor") as ejecutor:
        ejecutor.return_value.execute = AsyncMock(return_value=(False, ""))
        ejecutor.return_value.spawn = AsyncMock()
        await play_specific_music("musica de estopa")

    yt.assert_not_awaited()


def test_no_confunde_youtubers_con_youtube():
    from src.command_executor import _YOUTUBE_RE

    assert _YOUTUBE_RE.search("pon a youtubers de humor") is None
    assert _YOUTUBE_RE.search("musica de los 90") is None


# --- Redirección de playerctl (se hace en código, no en el prompt) ------------
#
# El árbol de decisión sigue enseñando execute_system_command("playerctl
# --player=spotify <acción>") porque cambiar esa línea degradaba el tool calling en
# general: "¿qué hora es?" pasó de 3/3 a 0/3. Así que la corrección de reproductor
# vive aquí, donde se puede medir.

@pytest.mark.asyncio
@pytest.mark.parametrize("comando", [
    "playerctl next",
    "playerctl --player=spotify next",
])
async def test_playerctl_se_reencamina_al_reproductor_activo(comando):
    from src.command_executor import execute_system_command

    with patch("src.command_executor._active_player", AsyncMock(return_value="vlc")), \
         patch("src.command_executor.open_terminal_and_run_command",
               AsyncMock(return_value="ok")) as terminal:
        await execute_system_command(comando)

    assert terminal.await_args.args[0] == "playerctl --player=vlc next"


@pytest.mark.asyncio
async def test_playerctl_sobre_chromium_va_por_cdp():
    """Chromium ignora 'play' y no tiene 'next' por MPRIS: hay que ir por el navegador."""
    from src.command_executor import execute_system_command

    with patch("src.command_executor._active_player",
               AsyncMock(return_value="chromium.instance1")), \
         patch("src.command_executor.music_control",
               AsyncMock(return_value="Siguiente canción.")) as control, \
         patch("src.command_executor.open_terminal_and_run_command",
               AsyncMock()) as terminal:
        resultado = await execute_system_command("playerctl --player=spotify next")

    control.assert_awaited_once_with("next")
    terminal.assert_not_awaited()
    assert resultado == "Siguiente canción."


@pytest.mark.asyncio
async def test_playerctl_sin_reproductor_lo_dice():
    from src.command_executor import execute_system_command

    with patch("src.command_executor._active_player", AsyncMock(return_value=None)), \
         patch("src.command_executor.open_terminal_and_run_command",
               AsyncMock()) as terminal:
        resultado = await execute_system_command("playerctl pause")

    assert "No hay ningún reproductor" in resultado
    terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_un_comando_normal_no_se_toca():
    """La redirección solo aplica a playerctl; el resto pasa intacto."""
    from src.command_executor import execute_system_command

    with patch("src.command_executor.open_terminal_and_run_command",
               AsyncMock(return_value="ok")) as terminal:
        await execute_system_command("date")

    assert terminal.await_args.args[0] == "date"


@pytest.mark.asyncio
async def test_active_player_ignora_al_propio_asistente():
    """`playerctl -l` incluye 'asistenteia'; no es música del usuario."""
    from src.command_executor import _active_player

    executor = MagicMock()
    executor.execute = AsyncMock(return_value=(True, "asistenteia\n"))
    with patch("src.command_executor.CommandExecutor", return_value=executor):
        assert await _active_player() is None
