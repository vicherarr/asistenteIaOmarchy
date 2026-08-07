"""Reproducción de música/vídeo de YouTube en el navegador visible.

El resultado que se devuelve al modelo SIEMPRE refleja lo que de verdad ha pasado:
si no hay resultados, si la página no carga o si el vídeo no arranca, se dice. Nunca
se devuelve un éxito sin haber comprobado que el <video> está sonando; una tool que
miente hace que el asistente mienta por voz.
"""

import asyncio
import logging
import re
import shutil

logger = logging.getLogger(__name__)

# yt-dlp resuelve la búsqueda en ~3s. Margen amplio pero acotado: el usuario está
# esperando de viva voz.
_YTDLP_TIMEOUT = 25.0
# Cuánto se espera a que el reproductor arranque de verdad tras el gesto de play.
_PLAYBACK_TIMEOUT = 12.0

# Un id de vídeo de YouTube: 11 caracteres del alfabeto base64-url.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Diálogo "Antes de ir a YouTube". Mientras está abierto se come las pulsaciones de
# teclado y monta un <tp-yt-iron-overlay-backdrop> que intercepta los clics reales, así
# que el vídeo se queda pausado y en silencio: cerrarlo es lo que desbloquea todo.
#
# Localizarlo tiene tres trampas, todas comprobadas en la página real:
#   1. El <button> vive dentro de shadow DOM (ytd-consent-bump-v2-lightbox →
#      ytd-button-renderer → yt-button-shape → button).
#   2. Su aria-label NO es "Rechazar todo" sino "Rechazar el uso de cookies y otros
#      datos para las finalidades descritas", así que buscar por nombre accesible falla.
#   3. El backdrop se traga los clics de verdad, así que hay que invocar .click() del
#      DOM. Para el consentimiento da igual que no sea un gesto de usuario: eso solo
#      lo exige el audio, que se resuelve después con la tecla 'k'.
#
# Se pulsa "Rechazar todo": para poner música no hace falta consentir nada.
_JS_DISMISS_CONSENT = """() => {
  const prioridad = ['rechazar todo', 'reject all', 'aceptar todo', 'accept all'];
  const botones = [];
  const walk = (root, depth) => {
    if (!root || depth > 12) return;
    const els = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of els) {
      if (el.tagName === 'BUTTON' || el.getAttribute?.('role') === 'button') {
        const t = (el.textContent || '').trim().toLowerCase();
        if (t && t.length < 30) botones.push([t, el]);
      }
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    }
  };
  walk(document, 0);
  for (const etiqueta of prioridad) {
    for (const [texto, el] of botones) {
      if (texto === etiqueta) { el.click(); return etiqueta; }
    }
  }
  return null;
}"""

# ¿Hay un modal de consentimiento VISIBLE? Los elementos siguen en el DOM tras cerrarlo,
# así que no vale con que existan: hay que medirlos.
_JS_HAS_CONSENT = """() => {
    const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
    };
    return visible(document.querySelector('ytd-consent-bump-v2-lightbox'))
        || visible(document.querySelector('tp-yt-iron-overlay-backdrop[opened]'));
}"""

# Estado real del reproductor. `document.querySelector('video').paused` no basta:
# YouTube expone su propia API en #movie_player y es la autoridad (getPlayerState:
# 1 = reproduciendo, 2 = pausado, 3 = buffering). Además se comprueba el silencio:
# un vídeo "reproduciéndose" pero muteado no es música para el usuario, es nada.
_JS_STATE = """() => {
    const mp = document.getElementById('movie_player');
    const v = document.querySelector('#movie_player video') || document.querySelector('video');
    let state = null, muted = null;
    try { if (mp && mp.getPlayerState) state = mp.getPlayerState(); } catch (e) {}
    try { if (mp && mp.isMuted) muted = mp.isMuted(); } catch (e) {}
    if (muted === null && v) muted = v.muted || v.volume === 0;
    return {
        state: state,
        paused: v ? v.paused : true,
        time: v ? v.currentTime : 0,
        muted: !!muted,
        hasVideo: !!v,
    };
}"""

_JS_TITLE = """() => {
    const h = document.querySelector('h1.ytd-watch-metadata, h1.title, #title h1');
    return h ? h.innerText.trim() : '';
}"""


async def youtube_search(query: str) -> tuple[str, str] | None:
    """Resuelve una búsqueda a (id_de_vídeo, título) con yt-dlp. None si no hay nada."""
    if not shutil.which("yt-dlp"):
        logger.error("yt-dlp no está instalado; no se puede resolver la búsqueda.")
        return None

    args = [
        "yt-dlp", "--no-warnings", "--flat-playlist",
        "--print", "%(id)s|%(title)s",
        f"ytsearch1:{query}",
    ]
    logger.info(f"Resolviendo en YouTube con yt-dlp: {query}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_YTDLP_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(f"yt-dlp superó los {_YTDLP_TIMEOUT}s buscando '{query}'.")
        return None
    except Exception as e:
        logger.error(f"Error ejecutando yt-dlp: {e}")
        return None

    if proc.returncode != 0:
        logger.error(f"yt-dlp falló (código {proc.returncode}): {stderr.decode(errors='replace')[:300]}")
        return None

    first = stdout.decode(errors="replace").strip().splitlines()
    if not first or "|" not in first[0]:
        logger.warning(f"yt-dlp no devolvió resultados para '{query}'.")
        return None

    video_id, _, title = first[0].partition("|")
    video_id = video_id.strip()
    if not _VIDEO_ID_RE.match(video_id):
        logger.warning(f"yt-dlp devolvió un id inesperado: {video_id!r}")
        return None

    return video_id, title.strip() or query


async def get_youtube_page(playwright):
    """Devuelve una pestaña dedicada a YouTube, sin pisar lo que el usuario esté leyendo.

    `get_page` coge `context.pages[0]`, que es la PRIMERA pestaña, no la activa: usarla
    para poner música secuestraría la pestaña en la que estuvieras.
    """
    from src.browser.launcher import CDP_PORT

    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    context = browser.contexts[0]
    for page in context.pages:
        if "youtube.com" in (page.url or ""):
            logger.info("Reutilizando la pestaña de YouTube ya abierta.")
            return page
    logger.info("Abriendo una pestaña nueva para YouTube.")
    return await context.new_page()


async def _dismiss_consent(page) -> bool:
    """Cierra el diálogo "Antes de ir a YouTube" pulsando "Rechazar todo".

    Busca en el documento principal y en TODOS los iframes: el diálogo de Google suele
    estar en uno, así que mirar solo la página principal no lo encuentra. Devuelve True
    si cerró algo.
    """
    try:
        if not await page.evaluate(_JS_HAS_CONSENT):
            return False
    except Exception:
        return False

    for frame in [page, *page.frames]:
        try:
            pulsado = await frame.evaluate(_JS_DISMISS_CONSENT)
        except Exception:
            continue
        if not pulsado:
            continue
        logger.info(f"Diálogo de consentimiento cerrado con '{pulsado}'.")
        # Al rechazar/aceptar, YouTube recarga la página.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        return True

    logger.warning("Hay un modal de consentimiento pero no encontré el botón para cerrarlo.")
    return False


async def _player_state(page) -> dict:
    try:
        return await page.evaluate(_JS_STATE) or {}
    except Exception:
        return {}


async def _is_playing(page) -> bool:
    """¿Está sonando de verdad? Ni pausado ni muteado."""
    st = await _player_state(page)
    if not st.get("hasVideo"):
        return False
    if st.get("muted"):
        return False
    state = st.get("state")
    if state is not None:
        return state == 1
    return not st.get("paused", True) and (st.get("time") or 0) > 0


async def _unmute(page) -> None:
    """Quita el silencio. YouTube arranca el autoplay muteado muy a menudo.

    Se usa la tecla 'm' (gesto real vía CDP) y, si no basta, la API del reproductor.
    Un vídeo que suena en silencio no es música: sin esto el usuario no oye nada
    mientras la tool asegura que está reproduciendo.
    """
    st = await _player_state(page)
    if not st.get("muted"):
        return
    try:
        await page.keyboard.press("m")
        await asyncio.sleep(0.8)
        if not (await _player_state(page)).get("muted"):
            logger.info("Silencio quitado con la tecla 'm'.")
            return
    except Exception as e:
        logger.warning(f"No se pudo pulsar 'm' para quitar el silencio: {e}")

    try:
        await page.evaluate("""() => {
            const mp = document.getElementById('movie_player');
            if (mp && mp.unMute) { mp.unMute(); mp.setVolume(80); }
            const v = document.querySelector('#movie_player video') || document.querySelector('video');
            if (v) { v.muted = false; if (v.volume === 0) v.volume = 0.8; }
        }""")
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning(f"No se pudo quitar el silencio por la API del reproductor: {e}")


async def _api_call(page, metodo: str) -> bool:
    """Llama a un método del reproductor de YouTube (playVideo, pauseVideo...).

    Es la vía más fiable: no depende de que el documento tenga el foco, al contrario
    que los atajos de teclado. Funciona porque Chromium se lanza con
    --autoplay-policy=no-user-gesture-required; si la instancia viniera de fuera sin
    esa bandera, el navegador puede rechazar el play y entran los fallbacks.
    """
    try:
        return bool(await page.evaluate(
            f"""() => {{
                const mp = document.getElementById('movie_player');
                if (!mp || !mp.{metodo}) return false;
                mp.{metodo}(); return true;
            }}"""
        ))
    except Exception as e:
        logger.warning(f"La API del reproductor falló en {metodo}: {e}")
        return False


async def _press(page, tecla: str) -> None:
    """Pulsa un atajo de YouTube. Los atajos exigen que el documento tenga el foco."""
    try:
        await page.bring_to_front()
    except Exception:
        pass
    await page.keyboard.press(tecla)


async def _nudge_play(page) -> None:
    """Arranca la reproducción, probando por orden de fiabilidad.

    1. La API del reproductor (`playVideo`): determinista y sin depender del foco.
    2. La tecla 'k': un evento sintetizado por CDP cuenta como gesto de usuario, útil
       si el navegador se lanzó sin la bandera de autoplay.
    3. Un clic sobre el reproductor.
    """
    await _unmute(page)
    if await _is_playing(page):
        return

    if await _api_call(page, "playVideo"):
        await asyncio.sleep(1.2)
        await _unmute(page)
        if await _is_playing(page):
            return

    try:
        await _press(page, "k")
        await asyncio.sleep(1.5)
        await _unmute(page)
        if await _is_playing(page):
            return
    except Exception as e:
        logger.warning(f"No se pudo enviar la tecla de play: {e}")

    try:
        player = page.locator("#movie_player").first
        if await player.count():
            # Clic arriba a la izquierda del reproductor: cuenta como gesto sin caer
            # en los controles ni en el enlace del título.
            await player.click(timeout=3000, position={"x": 30, "y": 30})
            await asyncio.sleep(1.5)
    except Exception as e:
        logger.warning(f"No se pudo hacer clic en el reproductor: {e}")

    await _unmute(page)


async def _wait_until_playing(page, timeout: float | None = None) -> bool:
    """Espera a que suene de verdad, insistiendo con el gesto y el consentimiento.

    Un cambio de vídeo implica navegación dentro de la SPA de YouTube: el reproductor
    tarda en montarse y arranca muteado a menudo, así que no basta con un sleep fijo.

    El timeout se resuelve en la llamada, no como valor por defecto del parámetro: así
    se puede ajustar el módulo (p.ej. en tests) sin quedarse con el valor de importación.
    """
    if timeout is None:
        timeout = _PLAYBACK_TIMEOUT
    deadline = asyncio.get_running_loop().time() + timeout
    nudges = 0
    while asyncio.get_running_loop().time() < deadline:
        if await _is_playing(page):
            return True
        if nudges < 3:
            await _dismiss_consent(page)
            await _nudge_play(page)
            nudges += 1
        else:
            await asyncio.sleep(1.0)
    return await _is_playing(page)


async def youtube_play(page, query: str) -> str:
    """Busca en YouTube y reproduce el resultado en el navegador visible.

    Navega a la Mix del vídeo (no al vídeo suelto) para que haya una cola real: así
    'siguiente canción' tiene a dónde ir y la música sigue sola al acabar el tema.
    """
    query = (query or "").strip()
    if not query:
        return "Error: dime qué quieres escuchar en YouTube."

    found = await youtube_search(query)
    if not found:
        return (f"No he encontrado nada en YouTube para '{query}'. "
                "No he reproducido nada; prueba con otro nombre.")

    video_id, title = found
    # &list=RD<id>&start_radio=1 es la Mix (radio) de YouTube: una cola infinita
    # de temas parecidos en vez de un solo vídeo.
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}&start_radio=1"

    from src.browser.navigation import browser_navigate

    try:
        await browser_navigate(page, url)
    except Exception as e:
        logger.error(f"Error navegando a YouTube: {e}")
        return f"No he podido abrir YouTube: {e}"

    # El modal de consentimiento tarda un momento en montarse y bloquea el teclado
    # mientras está abierto, así que se intenta cerrar más de una vez antes de dar
    # por buena cualquier comprobación de reproducción.
    for _ in range(3):
        if await _dismiss_consent(page):
            break
        await asyncio.sleep(1.0)

    # YouTube suele arrancar solo, pero muy a menudo MUTEADO; el gesto de _nudge_play
    # se encarga de sonido y play. El veredicto lo da siempre el estado del reproductor.
    if await _wait_until_playing(page):
        real_title = ""
        try:
            real_title = (await page.evaluate(_JS_TITLE) or "").strip()
        except Exception:
            pass
        shown = real_title or title
        logger.info(f"Reproducción confirmada en YouTube: {shown}")
        return f"Reproduciendo '{shown}' en YouTube."

    st = await _player_state(page)
    logger.warning(f"'{title}' abierto en YouTube pero no suena. Estado: {st}")
    if st.get("muted"):
        return (f"He abierto '{title}' en YouTube pero se ha quedado en silencio. "
                "Quítale el mute en la ventana del navegador.")
    return (f"He abierto '{title}' en YouTube pero la reproducción no ha arrancado sola. "
            "Dale al play en la ventana del navegador.")


async def youtube_control(page, action: str) -> str:
    """Controla la reproducción de la pestaña de YouTube por CDP.

    Chromium publica MPRIS, pero solo de forma parcial: `status` y `pause` funcionan,
    mientras que `play` no reanuda (exige un gesto de usuario) y `next` ni existe en su
    sesión de medios (comprobado en este equipo). Los atajos de la propia web enviados
    por CDP sí cuentan como gesto, así que son la vía fiable para todo lo demás.
    """
    try:
        if action in ("pause", "stop"):
            if not await _is_playing(page):
                return "La música ya estaba parada."
            if not await _api_call(page, "pauseVideo"):
                await _press(page, "k")
            await asyncio.sleep(1.0)
            if await _is_playing(page):
                return "He mandado pausa pero el vídeo sigue sonando."
            return "Música pausada." if action == "pause" else "Música detenida."

        if action == "play":
            if await _is_playing(page):
                return "Ya está sonando."
            await _nudge_play(page)
            if not await _is_playing(page):
                return "He mandado play pero el vídeo no ha arrancado."
            return "Reproducción reanudada."

        if action in ("next", "previous"):
            await _press(page, "Shift+N" if action == "next" else "Shift+P")
            # El cambio de vídeo navega dentro de la SPA: el reproductor nuevo tarda en
            # montarse, así que se espera activamente en vez de con un sleep fijo.
            await asyncio.sleep(2.0)
            etiqueta = "Siguiente canción." if action == "next" else "Canción anterior."
            if not await _wait_until_playing(page):
                return f"{etiqueta[:-1]}, pero no está sonando; dale al play."
            return etiqueta

        return f"Error: '{action}' no es una acción de reproducción."
    except Exception as e:
        logger.error(f"Error controlando YouTube ({action}): {e}")
        return f"No he podido {action} en YouTube: {e}"
