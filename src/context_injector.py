"""Inyección de contexto del sistema (Hardware + Omarchy/Hyprland)."""

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
    """Ejecuta un comando del sistema y devuelve su salida."""
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
    Devuelve un string formateado para inyectar en el system prompt.
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


def get_omarchy_commands() -> str:
    """Carga el manual de comandos de Omarchy."""
    if OMARCHY_COMMANDS_PATH.exists():
        return OMARCHY_COMMANDS_PATH.read_text(encoding="utf-8")

    logger.warning("Archivo de comandos Omarchy no encontrado. Usando defaults.")
    return """## COMANDOS DE OMARCHI/HYPRLAND DISPONIBLES

El asistente puede ejecutar estos comandos del sistema:

- `omarchy launch <app>` - Lanzar cualquier aplicación
- `omarchy search <query>` - Buscar en el sistema
- `hyprctl dispatch exec <command>` - Ejecutar comando en Hyprland
- `hyprctl dispatch focuswindow <class>` - Enfocar ventana
- `playerctl play-pause` - Pausar/reproducir música
- `playerctl next` - Siguiente pista
- `playerctl previous` - Pista anterior
- `playerctl stop` - Detener reproducción
- `wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle` - Silenciar audio
- `wpctl set-volume @DEFAULT_AUDIO_SINK@ 50%` - Ajustar volumen
- `chromium <url>` - Abrir URL en navegador
- `notify-send "titulo" "mensaje"` - Enviar notificación
"""


def get_system_prompt() -> str:
    """Carga el system prompt base o usa uno por defecto."""
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    return """Eres un asistente de voz experto para Linux CachyOS con Hyprland/Omarchy.
Responde siempre en español de forma concisa y útil.
Cuando necesites ejecutar acciones del sistema, usa el formato JSON especificado."""


def build_full_system_prompt() -> str:
    """
    Construye el system prompt completo con todo el contexto inyectado.
    Este es el prompt que se envía como primer mensaje de sistema a Gemma.
    """
    hardware_context = get_hardware_context()
    omarchy_commands = get_omarchy_commands()
    base_prompt = get_system_prompt()

    full_prompt = f"""{base_prompt}

{hardware_context}

{omarchy_commands}

## FORMATO DE RESPUESTA REQUERIDO

Debes responder SIEMPRE en este formato JSON:

```json
{{
    "response_text": "Tu respuesta verbal al usuario en español",
    "commands": [
        {{"command": "comando_a_ejecutar", "description": "qué hace"}}
    ],
    "action_type": "speak|execute|both|vision"
}}
```

Reglas:
- response_text: Lo que el asistente dirá por voz
- commands: Lista de comandos del sistema a ejecutar (vacía si no hay acción)
- action_type: "speak" (solo hablar), "execute" (solo ejecutar), "both" (ambos), "vision" (capturar pantalla y describir lo que se ve)
- Si el usuario pide ver, describir o analizar su pantalla, usar action_type "vision"
- Si el usuario pide abrir algo, usar `omarchy launch <app>`
- Si el usuario pide controlar música, usar `playerctl`
- Si el usuario pide buscar algo, usar `chromium <url>`
- Nunca inventes comandos que no estén en la lista
- Si no necesitas ejecutar nada, commands es [] y action_type es "speak"
"""

    return full_prompt


def get_context_summary() -> str:
    """Devuelve un resumen corto del contexto para logging."""
    return f"CPU: {get_cpu_info().splitlines()[0] if get_cpu_info() else 'N/A'}"
