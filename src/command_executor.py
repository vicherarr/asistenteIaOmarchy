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
    description: str = ""


@dataclass
class ParsedResponse:
    response_text: str
    commands: list[SystemCommand]
    action_type: str  # speak, execute, vision, both


class CommandExecutorError(Exception):
    """Errores en la ejecución de comandos."""
    pass


class CommandExecutor:
    """Ejecutor de comandos del sistema con validación de seguridad."""

    # Lista blanca de comandos permitidos (prefijos)
    ALLOWED_PREFIXES = [
        "omarchy",
        "hyprctl",
        "wpctl",
        "playerctl",
        "nmcli",
        "bluetoothctl",
        "systemctl --user",
        "chromium",
        "alacritty",
        "notify-send",
        "grim",
    ]

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def _is_safe_command(self, command: str) -> bool:
        """Verifica si un comando es seguro según la lista blanca."""
        # Protección básica contra encadenamiento
        if ";" in command or "&&" in command or "||" in command or "|" in command:
            logger.warning(f"Comando bloqueado por caracteres sospechosos: {command}")
            return False

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
    Parsea la respuesta del LLM para extraer texto y comandos.
    Maneja etiquetas de pensamiento (R1), bloques markdown y JSON plano.
    """
    # 1. Limpiar etiquetas de pensamiento de DeepSeek R1 (<thought>...</thought>)
    clean_text = re.sub(r"<thought>[\s\S]*?</thought>", "", raw_text).strip()
    
    # 2. Intentar extraer JSON de bloques de código markdown
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", clean_text)
    if not json_match:
        # Intentar buscar un objeto JSON plano { ... } si no hay bloques markdown
        json_match = re.search(r"(\{[\s\S]*?\})", clean_text)

    if json_match:
        try:
            json_str = json_match.group(1)
            data = json.loads(json_str)
            
            # Extraer campos con fallbacks
            response_text = data.get("response_text", "")
            action_type = data.get("action_type", "speak")

            commands = []
            for cmd_data in data.get("commands", []):
                commands.append(SystemCommand(
                    command=cmd_data.get("command", ""),
                    description=cmd_data.get("description", ""),
                ))

            # Si el JSON no tiene texto de respuesta, usamos el texto limpio fuera del JSON
            if not response_text:
                response_text = re.sub(r"```json[\s\S]*?```", "", clean_text).strip()
                response_text = re.sub(r"\{[\s\S]*?\}", "", response_text).strip()

            return ParsedResponse(
                response_text=response_text,
                commands=commands,
                action_type=action_type,
            )

        except (json.JSONDecodeError, AttributeError):
            logger.warning("Fallo al decodificar JSON sugerido por el modelo")
            pass

    # Si no hay JSON válido, devolver el texto limpio como respuesta verbal
    return ParsedResponse(
        response_text=clean_text,
        commands=[],
        action_type="speak",
    )
