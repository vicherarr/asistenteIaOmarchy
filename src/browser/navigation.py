"""Operaciones básicas de navegación browser."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def browser_navigate(page, target: str) -> str:
    """Navega a una URL."""
    if not target:
        return "Error: Especifica la URL a navegar en el argumento 'target'."
    if not target.startswith("http"):
        target = "https://" + target

    logger.info(f"Navegando a {target}...")
    await page.goto(target, wait_until="networkidle", timeout=15000)
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


async def browser_read(page) -> str:
    """Lee título y contenido de la pestaña activa."""
    title = await page.title()
    content = await page.evaluate("document.body.innerText")
    snippet = content[:3000] + "\n\n[Contenido de la página truncado por longitud...]" if len(content) > 3000 else content
    return f"INFORMACIÓN DE PESTAÑA ACTIVA:\n- TÍTULO: {title}\n- URL: {page.url}\n\nCONTENIDO TEXTUAL:\n\n{snippet}"


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
