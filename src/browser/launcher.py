"""Lanzador y conector de Chromium vía CDP."""

import asyncio
import logging
import os
import shutil
import socket
import subprocess

from src.config import settings

logger = logging.getLogger(__name__)

CDP_PORT = 9222


def _is_port_open(port: int = CDP_PORT) -> bool:
    """Verifica si el puerto CDP está abierto."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except Exception:
        return False


async def ensure_browser() -> str | None:
    """
    Asegura que Chromium está corriendo con CDP habilitado.
    Devuelve None si éxito, o un mensaje de error si falla.
    """
    if _is_port_open(CDP_PORT):
        return None

    profile_path = str(settings.PROJECT_ROOT / ".chrome-profile")
    os.makedirs(profile_path, exist_ok=True)

    binary = shutil.which("chromium") or shutil.which("google-chrome-stable")
    if not binary:
        return "Error: No se encontró Chromium o Google Chrome en el sistema."

    args = [
        binary,
        "--remote-debugging-port=9222",
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    logger.info(f"Lanzando Chromium visible con CDP habilitado en puerto {CDP_PORT}...")
    subprocess.Popen(args, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(10):
        await asyncio.sleep(0.5)
        if _is_port_open(CDP_PORT):
            return None

    return f"Error: Se intentó lanzar Chromium pero no se detectó respuesta en el puerto {CDP_PORT}."


async def get_page(playwright):
    """
    Conecta a Chromium vía CDP y devuelve una página activa.
    Debe llamarse DENTRO del bloque 'async with async_playwright()' del caller.
    """
    logger.info("Conectando a Chromium visible vía CDP...")
    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else await context.new_page()
    return page
