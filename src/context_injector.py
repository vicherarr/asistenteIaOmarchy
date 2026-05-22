"""Inyección de contexto del sistema (Hardware + Omarchy/Hyprland) - 100% async."""

import asyncio
import logging
import shlex
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

OMARCHY_COMMANDS_PATH = Path(__file__).parent.parent / "config" / "omarchy_commands.md"
SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "config" / "system_prompt.txt"


class ContextInjectorError(Exception):
    """Errores del inyector de contexto."""
    pass


async def _run_cmd(cmd: str, timeout: int = 5) -> str:
    """Ejecuta un comando del sistema de forma asíncrona."""
    try:
        # Si el comando contiene pipes, necesitamos shell
        if "|" in cmd or "&&" in cmd or "||" in cmd:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            args = shlex.split(cmd)
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
    except asyncio.TimeoutError:
        return "[Timeout]"
    except Exception as e:
        return f"[Exception: {e}]"


async def get_cpu_info() -> str:
    """Obtiene información del CPU de forma asíncrona."""
    return await _run_cmd("lscpu | grep -E 'Model name|Architecture|CPU\\(s\\)' | head -3")


async def get_gpu_info() -> str:
    """Obtiene información de la GPU de forma asíncrona."""
    return await _run_cmd("lspci 2>/dev/null | grep -i vga | head -1")


async def get_memory_info() -> str:
    """Obtiene información de memoria RAM de forma asíncrona."""
    return await _run_cmd("free -h | grep Mem")


async def get_disk_info() -> str:
    """Obtiene información de disco de forma asíncrona."""
    return await _run_cmd("df -h / | tail -1")


async def get_display_info() -> str:
    """Obtiene información de monitores desde Hyprland de forma asíncrona."""
    return await _run_cmd("hyprctl monitors -j 2>/dev/null || echo 'Hyprland no disponible'")


async def get_window_info() -> str:
    """Obtiene ventanas activas en Hyprland de forma asíncrona."""
    return await _run_cmd("hyprctl activewindow -j 2>/dev/null || echo 'Hyprland no disponible'")


async def get_audio_status() -> str:
    """Obtiene estado de dispositivos de audio de forma asíncrona."""
    return await _run_cmd("wpctl status 2>/dev/null | head -30 || echo 'PipeWire no disponible'")


async def get_network_info() -> str:
    """Obtiene información de red de forma asíncrona."""
    return await _run_cmd("ip -4 addr show | grep inet | grep -v 127.0.0.1")


async def get_hardware_context() -> str:
    """
    Recopila toda la información de hardware del sistema de forma asíncrona y paralela.
    Devuelve un string formateado para ser usado por herramientas LiteRT.
    """
    # Ejecutar todas las consultas en paralelo para minimizar latencia
    results = await asyncio.gather(
        get_cpu_info(),
        get_gpu_info(),
        get_memory_info(),
        get_disk_info(),
        get_display_info(),
        get_window_info(),
        get_audio_status(),
        get_network_info(),
    )

    sections = [
        ("CPU", results[0]),
        ("GPU", results[1]),
        ("Memoria RAM", results[2]),
        ("Disco", results[3]),
        ("Monitores", results[4]),
        ("Ventana Activa", results[5]),
        ("Audio", results[6]),
        ("Red", results[7]),
    ]

    context = "## CONTEXTO DE HARDWARE DEL SISTEMA\n\n"
    for name, info in sections:
        context += f"### {name}\n{info}\n\n"

    return context


async def get_system_context() -> str:
    """Alias para herramientas LiteRT."""
    return await get_hardware_context()


async def get_context_summary() -> str:
    """Devuelve un resumen corto del contexto para logging."""
    cpu = await get_cpu_info()
    first_line = cpu.splitlines()[0] if cpu else "N/A"
    return f"CPU: {first_line}"
