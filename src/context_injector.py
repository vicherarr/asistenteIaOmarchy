"""Inyección de contexto del sistema (Hardware + Omarchy/Hyprland)."""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

OMARCHY_COMMANDS_PATH = Path(__file__).parent.parent / "config" / "omarchy_commands.md"
SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "config" / "system_prompt.txt"


class ContextInjectorError(Exception):
    """Errores del inyector de contexto."""
    pass


def _run_cmd(cmd: str, shell: bool = True, timeout: int = 5) -> str:
    """Ejecuta un comando del sistema y devuelve su salida (Síncrono)."""
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else f"[Error: {result.stderr.strip()}]"
    except subprocess.TimeoutExpired:
        return "[Timeout]"
    except Exception as e:
        return f"[Exception: {e}]"


async def _run_cmd_async(cmd: str, timeout: int = 5) -> str:
    """Ejecuta un comando del sistema de forma asíncrona (Preferido)."""
    import shlex
    try:
        args = shlex.split(cmd) if not "|" in cmd else ["/bin/bash", "-c", cmd]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        if process.returncode == 0:
            return stdout.decode().strip()
        else:
            return f"[Error: {stderr.decode().strip()}]"
    except Exception as e:
        return f"[Exception: {e}]"


def get_cpu_info() -> str:
    """Obtiene información del CPU."""
    return _run_cmd("lscpu | grep -E 'Model name|Architecture|CPU\\(s\\)' | head -3")


def get_gpu_info() -> str:
    """Obtiene información de la GPU."""
    return _run_cmd("lspci 2>/dev/null | grep -i vga | head -1")


def get_memory_info() -> str:
    """Obtiene información de memoria RAM."""
    return _run_cmd("free -h | grep Mem")


def get_disk_info() -> str:
    """Obtiene información de disco."""
    return _run_cmd("df -h / | tail -1")


def get_display_info() -> str:
    """Obtiene información de monitores desde Hyprland."""
    return _run_cmd("hyprctl monitors -j 2>/dev/null || echo 'Hyprland no disponible'")


def get_window_info() -> str:
    """Obtiene ventanas activas en Hyprland."""
    return _run_cmd("hyprctl activewindow -j 2>/dev/null || echo 'Hyprland no disponible'")


def get_audio_status() -> str:
    """Obtiene estado de dispositivos de audio."""
    return _run_cmd("wpctl status 2>/dev/null | head -30 || echo 'PipeWire no disponible'")


def get_network_info() -> str:
    """Obtiene información de red."""
    return _run_cmd("ip -4 addr show | grep inet | grep -v 127.0.0.1")


def get_hardware_context() -> str:
    """
    Recopila toda la información de hardware del sistema.
    Devuelve un string formateado para ser usado por herramientas LiteRT.
    """
    sections = [
        ("CPU", get_cpu_info()),
        ("GPU", get_gpu_info()),
        ("Memoria RAM", get_memory_info()),
        ("Disco", get_disk_info()),
        ("Monitores", get_display_info()),
        ("Ventana Activa", get_window_info()),
        ("Audio", get_audio_status()),
        ("Red", get_network_info()),
    ]

    context = "## CONTEXTO DE HARDWARE DEL SISTEMA\n\n"
    for name, info in sections:
        context += f"### {name}\n{info}\n\n"

    return context


def get_system_context() -> str:
    """Alias para herramientas LiteRT."""
    return get_hardware_context()


def get_context_summary() -> str:
    """Devuelve un resumen corto del contexto para logging."""
    return f"CPU: {get_cpu_info().splitlines()[0] if get_cpu_info() else 'N/A'}"
