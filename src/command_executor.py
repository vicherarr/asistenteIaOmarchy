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

# --- Funciones para LiteRT Tool Calling ---

def execute_system_command(command: str) -> str:
    """
    Ejecuta un comando del sistema en Linux de forma segura. 
    Usa esto para abrir aplicaciones (ej. 'chromium', 'spotify'), controlar volumen ('wpctl'), 
    controlar música ('playerctl') o gestionar ventanas de Hyprland ('hyprctl').
    
    Args:
        command: El comando exacto a ejecutar.
    """
    executor = CommandExecutor()
    
    # Dado que LiteRT llama a esto desde un hilo separado (asyncio.to_thread),
    # podemos usar asyncio.run() si no hay un loop corriendo en este hilo.
    try:
        try:
            loop = asyncio.get_running_loop()
            # Si ya hay un loop (poco probable en el thread de LiteRT), usamos run_until_complete
            success, output = loop.run_until_complete(executor.execute(command))
        except RuntimeError:
            # Caso normal: no hay loop en el thread de LiteRT
            success, output = asyncio.run(executor.execute(command))
        
        if success:
            return f"Éxito: {output if output else 'Comando ejecutado correctamente'}"
        else:
            return f"Error: {output}"
    except Exception as e:
        logger.error(f"Error en tool execute_system_command: {e}")
        return f"Excepción ejecutando comando: {e}"

def get_system_status() -> str:
    """
    Obtiene un resumen detallado del estado actual del sistema: CPU, RAM, Audio, Red y Ventanas.
    Usa esto cuando el usuario pregunte por el rendimiento, carga o estado general del PC.
    """
    from src.context_injector import get_system_context
    try:
        # get_system_context es síncrona
        context = get_system_context()
        return f"Contexto del sistema obtenido:\n{context}"
    except Exception as e:
        return f"Error obteniendo estado: {e}"

def read_log_file(service: str = "asistenteia") -> str:
    """
    Lee las últimas 20 líneas del log de un servicio systemd. 
    Útil para diagnosticar errores si algo no funciona bien.
    
    Args:
        service: El nombre del servicio (ej. 'asistenteia', 'bluetooth', 'pipewire').
    """
    import subprocess
    try:
        cmd = ["journalctl", "--user", "-u", service, "-n", "20", "--no-pager"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return f"Logs de {service}:\n{result.stdout}"
        else:
            return f"Error leyendo logs: {result.stderr}"
    except Exception as e:
        return f"Error ejecutando journalctl: {e}"


def parse_gemma_response(raw_text: str) -> ParsedResponse:
    """
    Parsea la respuesta del LLM para extraer texto y comandos.
    Maneja etiquetas de pensamiento (R1), bloques markdown y JSON plano con estructuras anidadas.
    """
    # 1. Limpiar etiquetas de pensamiento de DeepSeek R1 (<thought>...</thought>)
    clean_text = re.sub(r"<thought>[\s\S]*?</thought>", "", raw_text).strip()
    
    # 2. Intentar extraer JSON de bloques de código markdown
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", clean_text)
    
    json_str = None
    if json_match:
        json_str = json_match.group(1)
    else:
        # Intentar buscar un objeto JSON plano { ... } manejando anidamiento básico
        # Buscamos desde el primer '{' hasta el último '}'
        start_idx = clean_text.find('{')
        end_idx = clean_text.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = clean_text[start_idx:end_idx+1]

    if json_str:
        try:
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
                text_without_json = clean_text.replace(json_str, "").strip()
                # Limpiar también los bloques markdown si quedaron
                text_without_json = re.sub(r"```json\s*```", "", text_without_json).strip()
                response_text = text_without_json

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
