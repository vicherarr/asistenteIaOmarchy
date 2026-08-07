"""Lanzador y conector de Chromium vía CDP."""

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path

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


def _mark_profile_clean(profile_path: str) -> None:
    """Marca el perfil como cerrado limpiamente.

    Si el proceso anterior murió sin cerrar (lo normal cuando el servicio se reinicia o
    se mata Chromium), al arrancar sale el globo "¿Quieres restaurar las páginas?", que
    se planta encima de la web y estorba tanto al usuario como a la automatización.
    Chromium decide eso leyendo `exit_type`/`exited_cleanly` de Preferences.
    """
    prefs_file = Path(profile_path) / "Default" / "Preferences"
    if not prefs_file.exists():
        return
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        profile = prefs.setdefault("profile", {})
        if profile.get("exit_type") == "Normal" and profile.get("exited_cleanly") is True:
            return
        profile["exit_type"] = "Normal"
        profile["exited_cleanly"] = True
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
        logger.info("Perfil de Chromium marcado como cerrado limpiamente.")
    except (OSError, ValueError) as e:
        logger.warning(f"No se pudo limpiar el estado de salida del perfil: {e}")


async def ensure_browser() -> str | None:
    """
    Asegura que Chromium está corriendo con CDP habilitado.
    Devuelve None si éxito, o un mensaje de error si falla.
    """
    if _is_port_open(CDP_PORT):
        return None

    profile_path = str(settings.PROJECT_ROOT / ".chrome-profile")
    os.makedirs(profile_path, exist_ok=True)
    _mark_profile_clean(profile_path)

    binary = shutil.which("chromium") or shutil.which("google-chrome-stable")
    if not binary:
        return "Error: No se encontró Chromium o Google Chrome en el sistema."

    args = [
        binary,
        "--remote-debugging-port=9222",
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
        # Sin esto, Chromium bloquea el autoplay con sonido en una pestaña sin
        # interacción previa (poner música en YouTube). Solo aplica a las instancias
        # que lanzamos nosotros, así que la reproducción no depende de la bandera:
        # youtube_play arranca además con un gesto real vía CDP.
        "--autoplay-policy=no-user-gesture-required",
        # El globo "¿Quieres restaurar las páginas?" tras un cierre sucio se pone
        # delante y estorba. Se pasan los dos nombres de la bandera porque cambió
        # entre versiones de Chromium; la que no exista se ignora sin más.
        "--hide-crash-restore-bubble",
        "--disable-session-crashed-bubble",
        # Ni globos de "Chromium no es tu navegador por defecto" ni burbujas de
        # infobar que tapen el reproductor.
        "--disable-infobars",
        "--no-service-autorun",
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
