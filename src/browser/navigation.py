"""Operaciones básicas de navegación browser."""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Presupuesto único de texto para lecturas en vivo. Aunque el contexto del modelo sea
# amplio, mantenemos las lecturas compactas por latencia y para que el TTS resuma bien.
READ_CHAR_BUDGET = 2500
# Por debajo de este umbral de texto limpio, la página es casi seguro visual
# (SPA/canvas/imágenes/muro) y caemos automáticamente a análisis con visión.
MIN_TEXT_FOR_READ = 200


def _extract_clean(html: str) -> Optional[str]:
    """Extrae el texto principal limpio del HTML ya renderizado (sin red adicional)."""
    if not isinstance(html, str) or not html.strip():
        return None
    try:
        import trafilatura
        return trafilatura.extract(
            html, include_comments=False, include_tables=True, favor_recall=True
        )
    except Exception:
        return None


async def _capture_and_stage(page, full_page: bool, prompt_hint: Optional[str]) -> None:
    """Captura la página visible (vía DOM, no escritorio) y la encola para la segunda
    pasada de visión. prompt_hint enfoca esa pasada en una pregunta concreta."""
    from src.vision_tool import VisionTool, stage_vision_capture

    tmp_path = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    await page.screenshot(path=tmp_path, full_page=full_page)
    resized = VisionTool._resize_image(tmp_path)
    if resized != tmp_path:
        Path(tmp_path).unlink(missing_ok=True)
    stage_vision_capture(resized, prompt_hint or None)


async def browser_navigate(page, target: str) -> str:
    """Navega a una URL."""
    if not target:
        return "Error: Especifica la URL a navegar en el argumento 'target'."
    if not target.startswith("http"):
        target = "https://" + target

    logger.info(f"Navegando a {target}...")
    # domcontentloaded es fiable; networkidle nunca dispara en webs con analytics/ads y
    # provoca timeouts falsos. Tras el DOM, esperamos 'load' pero sin bloquearnos si tarda.
    await page.goto(target, wait_until="domcontentloaded", timeout=15000)
    try:
        await page.wait_for_load_state("load", timeout=4000)
    except Exception:
        pass
    title = await page.title()
    return f"Éxito: Navegado correctamente a '{target}' (Título: '{title}')."


async def browser_click(page, target: str) -> str:
    """Hace clic en un selector CSS."""
    if not target:
        return "Error: Especifica el selector CSS para hacer clic en el argumento 'target'."

    logger.info(f"Haciendo clic en selector '{target}'...")
    await page.wait_for_selector(target, timeout=5000)
    await page.click(target)
    await asyncio.sleep(1.0)
    return f"Éxito: Se hizo clic en el elemento con selector '{target}'."


async def browser_type(page, target: str, value: str) -> str:
    """Escribe texto en un campo."""
    if not target:
        return "Error: Especifica el selector CSS en 'target' para escribir."

    logger.info(f"Escribiendo texto en '{target}'...")
    await page.wait_for_selector(target, timeout=5000)
    await page.click(target)
    await page.type(target, value, delay=50)
    await asyncio.sleep(0.5)
    return f"Éxito: Se escribió correctamente '{value}' en el elemento '{target}'."


async def browser_read(page, value: str = "") -> str:
    """Lee la pestaña activa de forma eficiente en tokens.

    1. Extrae texto LIMPIO con trafilatura sobre el DOM ya renderizado (respeta JS y la
       sesión/cookies del navegador visible), con fallback a innerText.
    2. Si el texto resultante es pobre, la página es visual: cae AUTOMÁTICAMENTE a una
       captura para la segunda pasada de visión, así no se pierde el contenido.

    Args:
        value: pregunta opcional en la que enfocar la lectura; guía la pasada de visión.
    """
    title = await page.title()
    url = page.url
    focus = (value or "").strip()

    clean = None
    try:
        html = await page.content()
        clean = await asyncio.to_thread(_extract_clean, html)
    except Exception as e:
        logger.warning(f"Extracción limpia falló ({e}); uso innerText.")

    if not clean or len(clean.strip()) < MIN_TEXT_FOR_READ:
        try:
            clean = await page.evaluate("document.body.innerText")
        except Exception:
            clean = clean or ""

    clean = (clean or "").strip()

    if len(clean) < MIN_TEXT_FOR_READ:
        logger.info(f"Texto insuficiente ({len(clean)} chars) en '{title}'. Fallback a visión.")
        await _capture_and_stage(page, full_page=False, prompt_hint=focus or None)
        return (f"La página '{title}' tiene poco texto legible; "
                f"capturo su contenido visual para analizarlo.")

    snippet = clean[:READ_CHAR_BUDGET]
    if len(clean) > READ_CHAR_BUDGET:
        snippet += "\n\n[Contenido truncado por longitud...]"

    header = f"INFORMACIÓN DE PESTAÑA ACTIVA:\n- TÍTULO: {title}\n- URL: {url}"
    if focus:
        header += f"\n- ENFOQUE: {focus}"
    return f"{header}\n\nCONTENIDO TEXTUAL:\n\n{snippet}"


async def browser_scroll(page, value: str) -> str:
    """Hace scroll vertical."""
    scroll_amount = 500
    if value:
        try:
            scroll_amount = int(value)
        except ValueError:
            pass
    logger.info(f"Haciendo scroll vertical de {scroll_amount}px...")
    await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
    return f"Éxito: Se desplazó la página verticalmente {scroll_amount} píxeles."


async def browser_look(page, value: str = "") -> str:
    """Captura visualmente la página web (solo la web, vía DOM) y la deja lista para análisis.

    No grim/escritorio: usa Playwright, así que captura únicamente el contenido del
    navegador, independientemente de la ventana enfocada. value='full' captura la página
    completa con scroll; por defecto solo el viewport visible.
    """
    full_page = str(value).lower().strip() in ("full", "all", "completa", "entera", "true", "1")
    logger.info(f"Capturando página web (full_page={full_page})...")

    # Sin prompt_hint: la segunda pasada usará la petición original del usuario.
    await _capture_and_stage(page, full_page=full_page, prompt_hint=None)

    title = await page.title()
    return f"Éxito: Captura visual de la página '{title}' realizada. Analizando su contenido."
