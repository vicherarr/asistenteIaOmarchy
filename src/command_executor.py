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
        "google-chrome-stable",
        "spotify",
        "alacritty",
        "code",
        "android-studio",
        "studio.sh",
        "notify-send",
        "grim",
        "wl-copy",
        "wl-paste",
        "journalctl",
        "dmesg",
        "pgrep",
    ]

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def _is_safe_command(self, command: str) -> bool:
        """Verifica si un comando es seguro según la lista blanca."""
        # Protección básica contra encadenamiento (excepto journalctl que lo necesita para --user)
        if ";" in command or "&&" in command or "||" in command:
            logger.warning(f"Comando bloqueado por caracteres sospechosos: {command}")
            return False

        for prefix in self.ALLOWED_PREFIXES:
            if command.startswith(prefix):
                return True

        logger.warning(f"Comando bloqueado (no permitido): {command}")
        return False

    async def spawn(self, command: str) -> bool:
        """Lanza un comando sin esperar a que termine (fire-and-forget)."""
        if not self._is_safe_command(command):
            return False
            
        try:
            args = shlex.split(command)
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True # Desacoplar del proceso padre
            )
            logger.info(f"Comando lanzado (spawn): {command}")
            return True
        except Exception as e:
            logger.error(f"Error en spawn '{command}': {e}")
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

def _sanitize_tool_args(arg: str) -> str:
    """Elimina tokens internos de LiteRT (<|"|>) que pueden aparecer en los argumentos."""
    if not isinstance(arg, str):
        return arg
    sanitized = re.sub(r'<\|".*?\|>', '', arg)
    sanitized = sanitized.replace('<|"', '').replace('"|>', '')
    return sanitized.strip()


def execute_system_command(command: str) -> str:
    """
    Ejecuta un comando del sistema en Linux (CachyOS/Hyprland).
    Usa esto para abrir cualquier aplicación instalada.
    
    COMANDOS COMUNES CONFIRMADOS:
    - Entorno: 'hyprctl', 'omarchy'
    - Desarrollo: 'android-studio', 'code', 'alacritty'
    - Web: 'chromium', 'google-chrome-stable'
    - Media/Audio: 'spotify', 'wpctl', 'playerctl'
    - Utilidades: 'nmcli', 'bluetoothctl', 'notify-send'
    
    Si el usuario dice 'abre <programa>', usa simplemente el nombre del binario.
    No inventes prefijos.
    
    Args:
        command: El binario o comando exacto a ejecutar.
    """
    command = _sanitize_tool_args(command)
    executor = CommandExecutor()

    # Lógica especial para Spotify: si se pide solo 'spotify', abrir y reproducir.
    if command.strip().lower() == "spotify":
        async def _spotify_flow():
            # 1. Comprobar si ya está abierto (pgrep devuelve 0 si existe, 1 si no)
            success, _ = await executor.execute("pgrep -x spotify")
            if not success:
                logger.info("Spotify no detectado. Lanzando proceso independiente...")
                # Usamos spawn con start_new_session=True para que la app gráfica persista
                await executor.spawn("spotify")
            else:
                logger.info("Spotify ya está en ejecución.")

            # 2. Esperar activamente a que aparezca en playerctl (máximo 15s)
            for i in range(15):
                # Importante: playerctl -l devuelve 0 si hay reproductores, pero necesitamos filtrar el texto
                _, players = await executor.execute("playerctl -l")
                if "spotify" in players.lower():
                    logger.info(f"Spotify detectado en el bus de medios (intento {i}). Enviando PLAY.")
                    await asyncio.sleep(2) # Margen para que cargue el DBus y la UI
                    return await executor.execute("playerctl --player=spotify play")
                await asyncio.sleep(1)
            
            return False, "Timeout: Spotify no apareció en el bus de medios."
        
        try:
            try:
                loop = asyncio.get_running_loop()
                success, output = loop.run_until_complete(_spotify_flow())
            except RuntimeError:
                success, output = asyncio.run(_spotify_flow())
            
            if success:
                return "Éxito: Spotify abierto y reproducción iniciada."
            else:
                return f"Spotify se abrió pero falló el play: {output}"
        except Exception as e:
            return f"Error en flujo de Spotify: {e}"
    
    try:
        try:
            loop = asyncio.get_running_loop()
            success, output = loop.run_until_complete(executor.execute(command))
        except RuntimeError:
            success, output = asyncio.run(executor.execute(command))
        
        if success:
            return f"Éxito: {output if output else 'Comando ejecutado correctamente'}"
        else:
            return f"Error: {output}"
    except Exception as e:
        logger.error(f"Error en tool execute_system_command: {e}")
        return f"Excepción ejecutando comando: {e}"


def clipboard_manager(action: str, content: str = "") -> str:
    """
    Lee o escribe contenido en el portapapeles del sistema (Wayland).
    
    Args:
        action: 'copy' para escribir en el portapapeles, 'paste' para leerlo.
        content: El texto a copiar (solo si action='copy').
    """
    action = _sanitize_tool_args(action)
    content = _sanitize_tool_args(content)
    
    try:
        if action == "copy":
            if not content:
                return "Error: No hay contenido para copiar."
            process = subprocess.Popen(['wl-copy'], stdin=subprocess.PIPE)
            process.communicate(input=content.encode())
            return f"Éxito: Texto copiado al portapapeles ({len(content)} caracteres)."
        
        elif action == "paste":
            result = subprocess.run(['wl-paste', '--no-newline'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                text = result.stdout.strip()
                return f"Contenido del portapapeles:\n{text}" if text else "El portapapeles está vacío."
            else:
                return f"Error leyendo portapapeles: {result.stderr}"
        else:
            return f"Acción desconocida: {action}."
    except Exception as e:
        return f"Error en clipboard_manager: {e}"


def web_search(query: str) -> str:
    """
    Busca información en internet (DuckDuckGo).
    Usa esto para responder preguntas sobre temas actuales, noticias o documentación técnica.
    """
    query = _sanitize_tool_args(query)
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            # Usamos el método moderno text() de DDGS
            search_results = list(ddgs.text(query, max_results=5))
            if not search_results: 
                return f"No hay resultados para: {query}"
            
            summary = f"Resultados para '{query}' (Para abrir un sitio, usa: execute_system_command con 'chromium <URL>'):\n\n"
            for i, r in enumerate(search_results, 1):
                url = r.get('href') or r.get('link')
                summary += f"[{i}] TÍTULO: {r.get('title')}\n"
                summary += f"    URL: {url}\n"
                summary += f"    RESUMEN: {r.get('body')}\n\n"
            return summary
    except Exception as e:
        logger.error(f"Error en web_search: {e}")
        return f"Error en búsqueda web: {e}"


def manage_windows(action: str, target: str = "") -> str:
    """
    Gestiona ventanas y escritorios de Hyprland.
    
    Args:
        action: 
            'focus': Enfoca una aplicación (ej. target='chromium').
            'close': Cierra una ventana.
            'fullscreen': Alterna pantalla completa.
            'workspace': Cambia al escritorio número <target> (ej. target='3').
            'movetoworkspace': Mueve la ventana activa al escritorio <target> (ej. target='2').
        target: Nombre de la app o número de escritorio según la acción.
    """
    action = _sanitize_tool_args(action)
    target = _sanitize_tool_args(target)
    cmd = ""
    
    if action == "focus": 
        cmd = f"hyprctl dispatch focuswindow {target}"
    elif action == "close": 
        cmd = "hyprctl dispatch killactive" if not target else f"hyprctl dispatch closewindow {target}"
    elif action == "fullscreen": 
        cmd = "hyprctl dispatch fullscreen 0"
    elif action == "workspace":
        if not target: return "Error: Especifica el número de escritorio."
        cmd = f"hyprctl dispatch workspace {target}"
    elif action == "movetoworkspace":
        if not target: return "Error: Especifica el escritorio destino."
        cmd = f"hyprctl dispatch movetoworkspace {target}"
    else: 
        return f"Acción desconocida: {action}"

    executor = CommandExecutor()
    try:
        try:
            loop = asyncio.get_running_loop()
            success, output = loop.run_until_complete(executor.execute(cmd))
        except RuntimeError:
            success, output = asyncio.run(executor.execute(cmd))
        return f"Éxito: {action} {target} ejecutado." if success else f"Error: {output}"
    except Exception as e:
        return f"Error: {e}"


def system_diagnostics(component: str = "all") -> str:
    """
    Busca errores recientes en audio, bluetooth o kernel.
    """
    component = _sanitize_tool_args(component)
    res = "## DIAGNÓSTICO RECIENTE\n\n"
    executor = CommandExecutor()
    
    async def _q(c):
        _, o = await executor.execute(c)
        return o

    try:
        loop = asyncio.get_event_loop()
        if component in ("all", "audio"):
            res += "### Audio:\n" + loop.run_until_complete(_q("journalctl --user -u pipewire -n 5 --no-pager")) + "\n"
        if component in ("all", "bluetooth"):
            res += "### BT:\n" + loop.run_until_complete(_q("journalctl -u bluetooth -n 5 --no-pager")) + "\n"
        return res
    except Exception as e:
        return f"Error: {e}"


def get_system_status() -> str:
    """Resumen de hardware: CPU, RAM, Audio."""
    from src.context_injector import get_system_context
    return f"Contexto:\n{get_system_context()}"


def read_log_file(service: str = "asistenteia") -> str:
    """Lee logs de systemd."""
    try:
        cmd = f"journalctl --user -u {service} -n 10 --no-pager"
        result = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=5)
        return f"Logs de {service}:\n{result.stdout}" if result.returncode == 0 else f"Error: {result.stderr}"
    except Exception as e:
        return f"Error: {e}"


def read_web_page(url: str) -> str:
    """
    Descarga y extrae el texto principal de una página web (limpio de anuncios y menús).
    Úsala después de web_search para leer el contenido profundo de un artículo o documentación.
    
    Args:
        url: La dirección web completa a leer.
    """
    url = _sanitize_tool_args(url)
    if not url.startswith("http"):
        return "Error: La URL debe empezar con http:// o https://"
        
    try:
        import trafilatura
        
        logger.info(f"Leyendo contenido de: {url}")
        downloaded = trafilatura.fetch_url(url)
        
        if downloaded is None:
            return f"Error: No se pudo descargar el contenido de {url}. Puede que el sitio bloquee bots o requiera JavaScript."
            
        result = trafilatura.extract(downloaded, include_comments=False, include_tables=True)
        
        if result is None:
            return "Error: No se pudo extraer texto limpio de esta página. Intenta abrirla manualmente."
            
        # Truncamiento de seguridad a ~3500 caracteres
        max_chars = 3500
        if len(result) > max_chars:
            result = result[:max_chars] + "\n\n[Contenido truncado por longitud...]"
            
        return f"CONTENIDO EXTRAÍDO DE {url}:\n\n{result}"
        
    except Exception as e:
        logger.error(f"Error en read_web_page: {e}")
        return f"Error leyendo la página web: {e}"


async def _playwright_task(action: str, target: str, value: str = "") -> str:
    """Tarea interna asíncrona para Playwright."""
    from playwright.async_api import async_playwright
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # User agent moderno para evitar bloqueos básicos
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        try:
            if action == "navigate":
                await page.goto(target, wait_until="networkidle", timeout=20000)
                # Extraer texto simplificado via JS
                content = await page.evaluate("document.body.innerText")
                title = await page.title()
                return f"CONTENIDO DE {title} ({target}):\n\n{content[:4000]}"
            
            elif action == "click":
                url, selector = target, value
                await page.goto(url, wait_until="networkidle")
                await page.click(selector)
                await page.wait_for_timeout(1000)
                content = await page.evaluate("document.body.innerText")
                return f"Acción realizada. Nuevo contenido:\n\n{content[:2000]}"
            
            else:
                return f"Acción '{action}' no soportada en interact_web."
                
        except Exception as e:
            return f"Error en interacción web: {e}"
        finally:
            await browser.close()


def interact_web(action: str, target: str, value: str = "") -> str:
    """
    Interactúa con sitios web dinámicos (JavaScript, SPAs, Clics). 
    Úsala si 'read_web_page' falla o el sitio requiere interacción.
    
    Args:
        action: 'navigate' (leer sitio con JS), 'click' (hacer clic en selector).
        target: URL para navegar, o URL para la acción de clic.
        value: Selector CSS (solo para acción 'click').
    """
    action = _sanitize_tool_args(action)
    target = _sanitize_tool_args(target)
    value = _sanitize_tool_args(value)
    
    try:
        try:
            loop = asyncio.get_running_loop()
            return loop.run_until_complete(_playwright_task(action, target, value))
        except RuntimeError:
            return asyncio.run(_playwright_task(action, target, value))
    except Exception as e:
        logger.error(f"Error en interact_web: {e}")
        return f"Error crítico en automatización web: {e}"


def parse_gemma_response(raw_text: str) -> ParsedResponse:
    """Parsea JSON y limpia etiquetas thought."""
    clean_text = re.sub(r"<thought>[\s\S]*?</thought>", "", raw_text).strip()
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", clean_text)
    json_str = json_match.group(1) if json_match else None
    if not json_str:
        start = clean_text.find('{')
        end = clean_text.rfind('}')
        if start != -1 and end != -1: json_str = clean_text[start:end+1]

    if json_str:
        try:
            data = json.loads(json_str)
            commands = [SystemCommand(command=c.get("command", ""), description=c.get("description", "")) for c in data.get("commands", [])]
            return ParsedResponse(response_text=data.get("response_text", ""), commands=commands, action_type=data.get("action_type", "speak"))
        except: pass
    return ParsedResponse(response_text=clean_text, commands=[], action_type="speak")
