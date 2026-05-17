"""Módulo de ejecución de comandos del sistema."""

import asyncio
import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SystemCommand:
    command: str
    description: str


@dataclass
class ParsedResponse:
    response_text: str
    commands: list[SystemCommand]
    action_type: str


class CommandExecutorError(Exception):
    """Errores del ejecutor de comandos."""
    pass


class CommandExecutor:
    """Ejecuta comandos del sistema de forma segura."""

    ALLOWED_PREFIXES = (
        "omarchy",
        "hyprctl",
        "playerctl",
        "wpctl",
        "chromium",
        "notify-send",
        "pactl",
        "pavucontrol",
        "firefox",
        "thunderbird",
        "spotify",
        "vlc",
        "rhythmbox",
        "gedit",
        "kate",
        "alacritty",
        "kitty",
        "foot",
        "nautilus",
        "dolphin",
        "thunar",
        "xreader",
        "evince",
        "gnome-calculator",
        "kcalc",
        "screenshot",
        "grim",
        "slurp",
        "swappy",
        "omarchy launch",
        "omarchy search",
    )

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def _is_safe_command(self, command: str) -> bool:
        """Verifica si un comando es seguro para ejecutar."""
        cmd_base = command.split()[0] if command.split() else ""

        for prefix in self.ALLOWED_PREFIXES:
            if command.startswith(prefix):
                return True

        logger.warning(f"Comando bloqueado (no permitido): {command}")
        return False

    async def execute(self, command: str, description: str = "") -> tuple[bool, str]:
        """
        Ejecuta un comando del sistema de forma segura y asíncrona.
        Devuelve (éxito, salida_o_error).
        """
        if not self._is_safe_command(command):
            return False, f"Comando no permitido: {command}"

        if self.dry_run:
            logger.info(f"[DRY RUN] {command}")
            return True, f"[DRY RUN] {command}"

        try:
            # Parse command safely to avoid shell injection
            args = shlex.split(command)

            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return False, "Timeout ejecutando comando"

            if process.returncode == 0:
                logger.info(f"Comando ejecutado: {command}")
                return True, stdout.decode().strip()
            else:
                error_msg = stderr.decode().strip() or f"Código de salida: {process.returncode}"
                logger.error(f"Error ejecutando '{command}': {error_msg}")
                return False, error_msg

        except Exception as e:
            return False, f"Excepción: {e}"

    async def execute_multiple(self, commands: list[SystemCommand]) -> list[tuple[bool, str]]:
        """Ejecuta múltiples comandos secuencialmente de forma asíncrona."""
        results = []
        for cmd in commands:
            success, output = await self.execute(cmd.command, cmd.description)
            results.append((success, output))
        return results


def parse_gemma_response(raw_text: str) -> ParsedResponse:
    """
    Parsea la respuesta de Gemma para extraer texto, comandos y tipo de acción.
    Maneja tanto JSON estructurado como texto plano.
    """
    json_pattern = r"```json\s*([\s\S]*?)\s*```"
    match = re.search(json_pattern, raw_text)

    if match:
        json_str = match.group(1)
        try:
            data = json.loads(json_str)
            response_text = data.get("response_text", raw_text)
            action_type = data.get("action_type", "speak")

            commands = []
            for cmd_data in data.get("commands", []):
                commands.append(SystemCommand(
                    command=cmd_data.get("command", ""),
                    description=cmd_data.get("description", ""),
                ))

            return ParsedResponse(
                response_text=response_text,
                commands=commands,
                action_type=action_type,
            )

        except json.JSONDecodeError:
            logger.warning("JSON inválido en respuesta de Gemma, usando texto plano")

    inline_json_pattern = r"\{[\s\S]*\}"
    match = re.search(inline_json_pattern, raw_text)

    if match:
        try:
            data = json.loads(match.group(0))
            response_text = data.get("response_text", raw_text)
            action_type = data.get("action_type", "speak")

            commands = []
            for cmd_data in data.get("commands", []):
                commands.append(SystemCommand(
                    command=cmd_data.get("command", ""),
                    description=cmd_data.get("description", ""),
                ))

            return ParsedResponse(
                response_text=response_text,
                commands=commands,
                action_type=action_type,
            )

        except json.JSONDecodeError:
            pass

    return ParsedResponse(
        response_text=raw_text,
        commands=[],
        action_type="speak",
    )
