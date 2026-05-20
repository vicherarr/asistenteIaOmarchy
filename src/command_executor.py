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
        "dbus-send",
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


async def execute_system_command(command: str) -> str:
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
                    logger.info(f"Spotify detectado en el bus de medios (intento {i}). Enviando PLAY vía D-Bus.")
                    await asyncio.sleep(2) # Margen para que cargue el DBus
                    dbus_cmd = "dbus-send --print-reply --dest=org.mpris.MediaPlayer2.spotify /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player.Play"
                    return await executor.execute(dbus_cmd)
                await asyncio.sleep(1)
            
            return False, "Timeout: Spotify no apareció en el bus de medios."
        
        try:
            success, output = await _spotify_flow()
            if success:
                return "Éxito: Spotify abierto y reproducción iniciada."
            else:
                return f"Spotify se abrió pero falló el play: {output}"
        except Exception as e:
            return f"Error en flujo de Spotify: {e}"
    
    try:
        success, output = await executor.execute(command)
        if success:
            return f"Éxito: {output if output else 'Comando ejecutado correctamente'}"
        else:
            return f"Error: {output}"
    except Exception as e:
        logger.error(f"Error en tool execute_system_command: {e}")
        return f"Excepción ejecutando comando: {e}"


async def clipboard_manager(action: str, content: str = "") -> str:
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
            process = await asyncio.create_subprocess_exec(
                'wl-copy',
                stdin=subprocess.PIPE
            )
            await process.communicate(input=content.encode())
            return f"Éxito: Texto copiado al portapapeles ({len(content)} caracteres)."
        
        elif action == "paste":
            process = await asyncio.create_subprocess_exec(
                'wl-paste', '--no-newline',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                text = stdout.decode().strip()
                return f"Contenido del portapapeles:\n{text}" if text else "El portapapeles está vacío."
            else:
                return f"Error leyendo portapapeles: {stderr.decode()}"
        else:
            return f"Acción desconocida: {action}."
    except Exception as e:
        return f"Error en clipboard_manager: {e}"


async def web_search(query: str) -> str:
    """
    Busca información en internet (DuckDuckGo).
    Usa esto para responder preguntas sobre temas actuales, noticias o documentación técnica.
    """
    query = _sanitize_tool_args(query)
    try:
        def _search():
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=5))
        
        search_results = await asyncio.to_thread(_search)
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


async def play_specific_music(query: str) -> str:
    """
    Busca y reproduce un artista, canción o álbum específico en Spotify.
    Usa esto cuando el usuario pida algo concreto (ej: "pon música de Estopa").
    
    Args:
        query: Nombre del artista, canción o álbum a buscar.
    """
    query = _sanitize_tool_args(query)
    # Buscamos de forma muy abierta
    search_query = f"Spotify artist track album {query}"
    
    logger.info(f"Buscando música específica: {query}")
    results_text = await web_search(search_query)
    
    # Extraer enlaces de Spotify
    import re
    pattern = r'https?://open\.spotify\.com/(?:[a-z]{2}/)?(?:artist|track|album|playlist)/[a-zA-Z0-9]+'
    spotify_links = re.findall(pattern, results_text)
    
    executor = CommandExecutor()
    target_uri = None
    
    if spotify_links:
        target_link = spotify_links[0]
        logger.info(f"Enlace de Spotify encontrado: {target_link}")
        # Convertir a URI nativo
        uri_match = re.search(r'spotify\.com/(artist|track|album|playlist)/([a-zA-Z0-9]+)', target_link)
        if uri_match:
            target_uri = f"spotify:{uri_match.group(1)}:{uri_match.group(2)}"
        else:
            target_uri = target_link
    else:
        logger.warning(f"No se encontró link directo para '{query}'. Usando búsqueda nativa.")
        target_uri = f"spotify:search:{query}"

    async def _spotify_aggressive_flow(uri: str):
        # 1. Asegurar que Spotify está abierto
        success_pgrep, _ = await executor.execute("pgrep -x spotify")
        if not success_pgrep:
            logger.info("Spotify no detectado. Lanzando...")
            await executor.spawn("spotify")
        
        # 2. Esperar a que el reproductor aparezca en el bus de medios (hasta 20s)
        player_ready = False
        for i in range(20):
            _, players = await executor.execute("playerctl -l")
            if "spotify" in players.lower():
                player_ready = True
                logger.info(f"Spotify detectado en el bus tras {i}s.")
                break
            await asyncio.sleep(1)
        
        if not player_ready:
            return False, "Spotify no apareció en el bus de medios."

        # 3. Secuencia de reproducción nativa vía D-Bus (OpenUri)
        # Este es el método oficial MPRIS para abrir Y REPRODUCIR un URI inmediatamente
        logger.info(f"Enviando OpenUri para: {uri}")
        open_uri_cmd = (
            f"dbus-send --print-reply --dest=org.mpris.MediaPlayer2.spotify "
            f"/org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player.OpenUri string:\"{uri}\""
        )
        
        await asyncio.sleep(2) # Margen de carga
        await executor.execute(open_uri_cmd)
        
        # 4. Verificación y reintento de Play si se queda pausado
        await asyncio.sleep(3)
        for _ in range(2):
            _, status = await executor.execute("playerctl --player=spotify status")
            if "Playing" in status:
                logger.info("Reproducción confirmada.")
                return True, "OK"
            
            logger.info("El reproductor no arrancó solo. Forzando Play vía D-Bus...")
            dbus_play = "dbus-send --print-reply --dest=org.mpris.MediaPlayer2.spotify /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player.Play"
            await executor.execute(dbus_play)
            await asyncio.sleep(2)
            
        return False, "No se pudo iniciar la reproducción tras abrir el URI."

    try:
        success, msg = await _spotify_aggressive_flow(target_uri)
        if success:
            return f"Reproduciendo '{query}' en Spotify."
        else:
            return f"He abierto Spotify con '{query}', pero es posible que tengas que darle al play manualmente ({msg})."
    except Exception as e:
        return f"Error en el flujo de música: {e}"


async def manage_windows(action: str, target: str = "") -> str:
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
        success, output = await executor.execute(cmd)
        return f"Éxito: {action} {target} ejecutado." if success else f"Error: {output}"
    except Exception as e:
        return f"Error: {e}"


async def system_diagnostics(component: str = "all") -> str:
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
        if component in ("all", "audio"):
            res += "### Audio:\n" + await _q("journalctl --user -u pipewire -n 5 --no-pager") + "\n"
        if component in ("all", "bluetooth"):
            res += "### BT:\n" + await _q("journalctl -u bluetooth -n 5 --no-pager") + "\n"
        return res
    except Exception as e:
        return f"Error: {e}"


async def get_system_status() -> str:
    """Resumen de hardware: CPU, RAM, Audio."""
    from src.context_injector import get_system_context
    return f"Contexto:\n{get_system_context()}"


async def read_log_file(service: str = "asistenteia") -> str:
    """Lee logs de systemd."""
    try:
        cmd = f"journalctl --user -u {service} -n 10 --no-pager"
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return f"Logs de {service}:\n{stdout.decode()}" if process.returncode == 0 else f"Error: {stderr.decode()}"
    except Exception as e:
        return f"Error: {e}"


async def read_web_page(url: str) -> str:
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
        def _fetch():
            import trafilatura
            logger.info(f"Leyendo contenido de: {url}")
            downloaded = trafilatura.fetch_url(url)
            if downloaded is None: return None
            return trafilatura.extract(downloaded, include_comments=False, include_tables=True)
            
        result = await asyncio.to_thread(_fetch)
        
        if result is None:
            return f"Error: No se pudo extraer texto limpio de esta página. Intenta abrirla manualmente."
            
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


async def interact_web(action: str, target: str, value: str = "") -> str:
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
        return await _playwright_task(action, target, value)
    except Exception as e:
        logger.error(f"Error en interact_web: {e}")
        return f"Error crítico en automatización web: {e}"


async def open_terminal_and_run_command(command: str) -> str:
    """
    Abre una terminal gráfica visible (como Alacritty, Kitty o Foot) y ejecuta un comando específico en ella.
    Mantiene la ventana de la terminal abierta después de ejecutar el comando para que el usuario pueda
    ver la salida, interactuar con ella o escribir su contraseña de administrador (sudo) si es necesario.
    
    Usa esta herramienta cuando el usuario pida "abre una terminal y ejecuta..." o cuando
    un comando requiera interacción directa del usuario o privilegios de administrador (como sudo).
    
    Args:
        command: El comando exacto de Linux que se ejecutará dentro de la terminal.
    """
    import shutil
    import subprocess
    
    command = _sanitize_tool_args(command)
    
    # Lista de emuladores de terminal soportados en orden de preferencia
    terminals = ["alacritty", "kitty", "foot"]
    chosen_terminal = None
    for term in terminals:
        if shutil.which(term):
            chosen_terminal = term
            break
            
    if not chosen_terminal:
        return "Error: No se encontró ningún emulador de terminal compatible (Alacritty, Kitty, Foot) instalado."
        
    try:
        # Usamos bash -c para ejecutar el comando y luego iniciar un shell interactivo bash para mantener la ventana abierta
        shell_cmd = f"{command}; exec bash"
        args = [chosen_terminal, "-e", "bash", "-c", shell_cmd]
        
        logger.info(f"Abriendo terminal {chosen_terminal} con comando: {command}")
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return f"Éxito: Se ha abierto una ventana de {chosen_terminal.capitalize()} ejecutando el comando: {command}"
    except Exception as e:
        logger.error(f"Error abriendo terminal {chosen_terminal}: {e}")
        return f"Error al abrir la terminal: {e}"


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
