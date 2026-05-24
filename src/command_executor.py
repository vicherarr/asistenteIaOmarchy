"""Módulo de ejecución de comandos del sistema."""

import asyncio
import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

EXIT_CODE_EXPLANATIONS = {
    0: "El comando se ejecutó correctamente sin errores.",
    1: "Error genérico. El comando falló por una razón no específica.",
    2: "Uso incorrecto del comando. Se pasaron argumentos inválidos o falta algún parámetro requerido.",
    126: "El archivo existe pero no tiene permisos de ejecución o no es un ejecutable válido.",
    127: "Comando no encontrado. El programa que intentas ejecutar no está instalado o no está en el PATH del sistema.",
    128: "El proceso recibió una señal fatal no manejada.",
    130: "El proceso fue interrumpido manualmente con Ctrl+C (SIGINT).",
    137: "El proceso fue terminado abruptamente por falta de memoria (SIGKILL / OOM killer).",
    139: "El proceso falló con un error de segmentación (SIGSEGV). Accedió a memoria que no le pertenecía.",
    143: "El proceso recibió una señal de terminación (SIGTERM).",
}

def explain_exit_code(code: int) -> str:
    """Devuelve una explicación en lenguaje natural para un código de salida."""
    if code == 0:
        return EXIT_CODE_EXPLANATIONS[0]
    explanation = EXIT_CODE_EXPLANATIONS.get(code)
    if explanation:
        return explanation
    if code > 128:
        signal_num = code - 128
        return f"El proceso fue terminado por la señal {signal_num} del sistema."
    return f"El comando falló con un código de error no estándar ({code})."





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
        "thunar",
        "nautilus",
        "xdg-open",
        "studio.sh",
        "notify-send",
        "grim",
        "wl-copy",
        "wl-paste",
        "journalctl",
        "dmesg",
        "pgrep",
        "pkill",
        "killall",
        "kill",
        "dbus-send",
        "ls",
        "cd",
        "pwd",
        "echo",
        "cat",
        "grep",
        "find",
        "head",
        "tail",
        "mkdir",
        "touch",
        "clear",
    ]

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def _is_safe_command(self, command: str) -> bool:
        """Verifica si un comando está en la lista blanca de confianza.
        
        Comandos que NO están en la lista blanca NO se bloquean; se ejecutan igualmente
        pero con una advertencia visible en terminal para que el usuario lo supervise.
        """
        cmd_clean = command.strip()
        if not cmd_clean:
            return False

        import re
        sub_commands = re.split(r'&&|\|\||;|\|', cmd_clean)

        for sub in sub_commands:
            sub = sub.strip()
            if not sub:
                continue

            for prefix in self.ALLOWED_PREFIXES:
                if sub == prefix or sub.startswith(prefix + " ") or sub.startswith(prefix + "\t") or sub.startswith(prefix + "-"):
                    return True
            
            logger.info(f"Comando no verificado (requiere supervisión): '{sub}' en: '{command}'")
            return False

        return True

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
                stderr_msg = stderr.decode().strip()
                explanation = explain_exit_code(process.returncode)
                error_msg = stderr_msg if stderr_msg else explanation
                logger.error(f"Error ejecutando '{command}': {error_msg}")
                return False, error_msg

        except Exception as e:
            return False, f"Excepción: {e}"



# --- Funciones para LiteRT Tool Calling ---

def _sanitize_tool_args(arg: str) -> str:
    """Elimina tokens internos de LiteRT (<|"|>) que pueden aparecer en los argumentos."""
    if not isinstance(arg, str):
        return arg
    # Eliminar tokens completos <|"texto"|>
    sanitized = re.sub(r'<\|".*?\|>', '', arg)
    # Eliminar tokens parciales al inicio o final
    sanitized = sanitized.replace('<|"', '').replace('"|>', '')
    # Eliminar cualquier token residual con patrón <|...|>
    sanitized = re.sub(r'<\|.*?\|>', '', sanitized)
    return sanitized.strip()


async def execute_system_command(command: str) -> str:
    """
    Ejecuta un comando del sistema en Linux (CachyOS/Hyprland).
    Usa esto para abrir cualquier aplicación instalada o para ejecutar cualquier comando de consola.
    Se ejecutará dentro de la terminal persistente visible para el usuario.
    
    Args:
        command: El binario o comando exacto a ejecutar.
    """
    command = _sanitize_tool_args(command)

    # Normalizar comandos playerctl sin --player para que siempre apunten a Spotify
    import re as _re
    _playerctl_actions = {"next", "previous", "pause", "play", "stop", "play-pause"}
    _pc_match = _re.match(r'^playerctl\s+(' + '|'.join(_playerctl_actions) + r')$', command.strip())
    if _pc_match:
        action = _pc_match.group(1)
        command = f"playerctl --player=spotify {action}"
        logger.info(f"playerctl normalizado a: {command}")

    logger.info(f"Redirigiendo execute_system_command a terminal visible: {command}")
    return await open_terminal_and_run_command(command)


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
        
        # Límite inteligente: 3500 chars (~1200 tokens) para búsquedas web
        max_chars = 3500
        if len(summary) > max_chars:
            summary = summary[:max_chars] + "\n...[resultados truncados]"
        
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
    query = _sanitize_tool_args(query).strip().lower()

    # Guard: interceptar palabras de control de reproducción mal enrutadas
    _CONTROL_MAP = {
        "siguiente": "next",
        "siguiente canción": "next",
        "next": "next",
        "anterior": "previous",
        "canción anterior": "previous",
        "atrás": "previous",
        "atras": "previous",
        "previous": "previous",
        "pausa": "pause",
        "pausar": "pause",
        "para": "pause",
        "pause": "pause",
        "play": "play",
        "reanuda": "play",
        "continúa": "play",
        "continua": "play",
    }
    if query in _CONTROL_MAP:
        action = _CONTROL_MAP[query]
        logger.warning(
            f"play_specific_music recibió palabra de control '{query}' → redirigiendo a playerctl {action}"
        )
        return await execute_system_command(f"playerctl --player=spotify {action}")

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



async def open_file_manager(folder: str = "") -> str:
    """
    Abre el explorador de archivos (Thunar por defecto, con fallback a Nautilus).
    
    Args:
        folder: Ruta de la carpeta a abrir (opcional). Si no se especifica, abre el home del usuario.
    """
    import os
    import shutil
    folder = _sanitize_tool_args(folder)
    executor = CommandExecutor()
    
    if folder and not os.path.isdir(folder):
        return f"Error: La carpeta '{folder}' no existe."
    
    file_manager_bin = None
    for fm in ["thunar", "nautilus", "xdg-open"]:
        if shutil.which(fm):
            file_manager_bin = fm
            break
    
    if not file_manager_bin:
        return "Error: No se encontró ningún explorador de archivos (Thunar, Nautilus o xdg-open)."
    
    cmd = f"{file_manager_bin} {folder}" if folder else file_manager_bin
    
    try:
        success = await executor.spawn(cmd)
        return f"Éxito: Se ha abierto el explorador de archivos ({file_manager_bin}) en {folder or 'tu carpeta personal'}." if success else "Error al abrir el explorador de archivos."
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


async def read_log_file(service: str = "asistenteia") -> str:
    """Lee logs de systemd de forma segura sin shell injection."""
    try:
        process = await asyncio.create_subprocess_exec(
            "journalctl",
            "--user",
            "-u", service,
            "-n", "10",
            "--no-pager",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            return f"Logs de {service}:\n{stdout.decode()}"
        else:
            explanation = explain_exit_code(process.returncode)
            stderr_msg = stderr.decode().strip()
            return f"Error leyendo logs: {stderr_msg or explanation}"
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


async def _run_tmux_cmd(args: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    """Ejecuta un comando tmux de forma asíncrona. Devuelve (éxito, salida)."""
    try:
        process = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        if process.returncode == 0:
            return True, stdout.decode().strip()
        return False, stderr.decode().strip()
    except asyncio.TimeoutError:
        return False, "Timeout ejecutando tmux"
    except Exception as e:
        return False, str(e)


def _is_waiting_for_password(screen_output: str) -> bool:
    """
    Inspecciona el contenido de la pantalla capturada de tmux para detectar
    si el terminal está solicitando la contraseña de administrador (sudo).
    """
    if not screen_output:
        return False
    
    # Nos centramos en las últimas líneas que es donde suelen aparecer los prompts
    lines = [l.strip().lower() for l in screen_output.splitlines() if l.strip()]
    if not lines:
        return False
    
    # Consideramos las últimas 3 líneas no vacías para evitar falsos positivos
    recent_lines = lines[-3:]
    
    password_indicators = [
        "password",
        "contraseña",
        "contrasenya",
        "passphrase",
        "clave",
        "[sudo]"
    ]
    
    for line in recent_lines:
        if any(ind in line for ind in password_indicators):
            # Generalmente los prompts terminan en dos puntos (:), contienen [sudo], o piden el input
            if line.endswith(":") or ":" in line or "[sudo]" in line:
                return True
    return False


async def open_terminal_and_run_command(command: str) -> str:
    """
    Abre una terminal gráfica visible o crea una nueva pestaña/shell (bash) en la ventana existente.
    Llama a esta herramienta cuando el usuario te pida abrir una terminal, otra terminal, una segunda o tercera
    terminal, una pestaña nueva o un intérprete de comandos bash/zsh.
    Si ya hay una terminal abierta gráficamente en pantalla, esta herramienta creará y enfocará automáticamente
    una nueva pestaña limpia de comandos en la misma ventana de terminal.
    Mantiene la ventana de la terminal abierta para que el usuario interactúe con ella.
    
    Args:
        command: El comando de Linux a ejecutar. Si solo deseas abrir una terminal/pestaña limpia, pasa "" (cadena vacía) o "bash".
    """
    import shutil
    import subprocess
    
    command = _sanitize_tool_args(command)
    session_name = "asistenteia"
    
    # 1. Comprobar si la sesión de tmux existe y está acoplada
    session_exists = False
    session_attached = False
    
    try:
        ok, _ = await _run_tmux_cmd(["has-session", "-t", session_name])
        if ok:
            session_exists = True
            ok_sessions, output = await _run_tmux_cmd(["list-sessions", "-F", "#{session_name} #{session_attached}"])
            if ok_sessions:
                for line in output.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == session_name:
                        if parts[1] != "0":
                            session_attached = True
                        break
    except Exception as e:
        logger.error(f"Error comprobando estado de tmux: {e}")

    # 2. Interceptar si el comando es abrir una nueva terminal/shell
    cmd_strip = command.strip().lower()
    is_terminal_launcher = cmd_strip in ("alacritty", "kitty", "foot", "bash", "zsh", "sh", "") or any(cmd_strip.startswith(term) for term in ("alacritty ", "kitty ", "foot ", "bash ", "zsh ", "sh "))
    
    if is_terminal_launcher:
        logger.info(f"Interceptado comando de terminal redundante o vacío: '{command}'")
        if session_attached:
            # Si el terminal ya está abierto gráficamente en pantalla, creamos un nuevo tab (pestaña) en tmux
            logger.info("Terminal ya abierta. Creando nueva pestaña de shell (bash).")
            ok_nw, err_nw = await _run_tmux_cmd(["new-window", "-t", f"{session_name}:", "-n", "bash"])
            if not ok_nw:
                logger.error(f"Error al crear nueva pestaña en tmux: {err_nw}")
                return f"Error al abrir una nueva pestaña de terminal: {err_nw}"
            return "Éxito: Se ha creado y enfocado una nueva pestaña de terminal en la ventana existente."
        else:
            # Si está cerrada, procedemos al paso de abrir la ventana de terminal (que se adjuntará a la sesión)
            command = ""  # Limpiamos el comando para que no escriba nada adicional
            banner_line = None
    else:
        wrapped_command = command
        banner_line = None
        if not command.strip().endswith("&"):
            cmd_name = command.strip().split()[0].split("/")[-1] if command.strip() else "comando"
            banner_line = f'test $? = 0 && echo "✅ {cmd_name} OK" || echo "❌ {cmd_name} ERROR $?"'

    # 3. Si la sesión existe y hay un comando a ejecutar, verificar si el pane activo está ocupado
    if session_exists and command:
        try:
            ok_proc, current_proc = await _run_tmux_cmd(["display-message", "-p", "-t", f"{session_name}:", "#{pane_current_command}"])
            if ok_proc and current_proc.strip().lower() not in ("bash", "zsh", "sh", "tmux"):
                cmd_name = command.strip().split()[0].split("/")[-1] if command.strip() else "comando"
                logger.info(f"Terminal ocupada con el proceso '{current_proc.strip()}'. Creando nueva pestaña de tmux: {cmd_name}")
                ok_nw, err_nw = await _run_tmux_cmd(["new-window", "-t", f"{session_name}:", "-n", cmd_name, "bash"])
                if not ok_nw:
                    logger.error(f"Error al crear nueva pestaña ocupada en tmux: {err_nw}")
                    return f"Error al crear nueva pestaña de terminal para {cmd_name}: {err_nw}"
                # Dar un momento para que el nuevo shell se inicialice
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error comprobando ocupación de terminal: {e}")

    # 4. Si no está acoplada a una terminal gráfica, abrir la ventana del emulador
    if not session_attached:
        terminals = ["alacritty", "kitty", "foot"]
        chosen_terminal = None
        for term in terminals:
            if shutil.which(term):
                chosen_terminal = term
                break
                
        if not chosen_terminal:
            return "Error: No se encontró ningún emulador de terminal compatible (Alacritty, Kitty, Foot) instalado."
            
        try:
            # tmux new-session -A -s <name> se acopla a la sesión si existe, o la crea si no.
            tmux_cmd = f"tmux new-session -A -s {session_name} bash"
            args = [chosen_terminal, "-e", "bash", "-c", tmux_cmd]
                
            logger.info(f"Abriendo terminal {chosen_terminal} con sesión tmux '{session_name}'")
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            # Esperar a que la terminal gráfica se inicialice y adjunte
            await asyncio.sleep(0.8)
            session_attached = True
        except Exception as e:
            logger.error(f"Error abriendo terminal gráfica: {e}")
            return f"Error al abrir la terminal gráfica: {e}"

    # 5. Enviar el comando principal
    if not command:
        return "Éxito: Terminal persistente de AsistenteIA abierta correctamente."

    try:
        cmd_name = command.strip().split()[0].split("/")[-1] if command.strip() else "comando"
        # Renombrar la pestaña activa actual en tmux para asociarla al comando
        await _run_tmux_cmd(["rename-window", "-t", f"{session_name}:", cmd_name])

        logger.info(f"Enviando comando a la sesión '{session_name}': {command}")
        buf_file = settings.TEMP_DIR / "tmux_buf"
        buf_file.write_text(command.strip() + "\n", encoding="utf-8")
        await _run_tmux_cmd(["load-buffer", "-b", "cmd", "-t", f"{session_name}:", str(buf_file)])
        await _run_tmux_cmd(["paste-buffer", "-b", "cmd", "-t", f"{session_name}:"])
        await _run_tmux_cmd(["send-keys", "-t", f"{session_name}:", "C-m"])
        buf_file.unlink(missing_ok=True)
        
        # Esperar el renderizado inicial y ver si solicita password
        await asyncio.sleep(1.5)
        
        # 6. Detección dinámica de prompt de contraseña (sudo)
        ok_capture, screen_output = await _run_tmux_cmd(["capture-pane", "-p", "-t", f"{session_name}:"])
        if ok_capture and _is_waiting_for_password(screen_output):
            logger.info("Detectado prompt de contraseña (sudo). Cancelando envío automático del banner_line.")
            banner_line = None
            
        # 7. Enviar banner si no se canceló
        if banner_line:
            buf_file.write_text(banner_line + "\n", encoding="utf-8")
            await _run_tmux_cmd(["load-buffer", "-b", "cmd", "-t", f"{session_name}:", str(buf_file)])
            await _run_tmux_cmd(["paste-buffer", "-b", "cmd", "-t", f"{session_name}:"])
            await _run_tmux_cmd(["send-keys", "-t", f"{session_name}:", "C-m"])
            buf_file.unlink(missing_ok=True)
            # Esperar a que el banner se ejecute
            await asyncio.sleep(1.0)
            
        # 8. Capturar salida final
        ok_capture, screen_output = await _run_tmux_cmd(["capture-pane", "-p", "-t", f"{session_name}:"])
        if ok_capture and screen_output:
            lines = screen_output.splitlines()
            last_lines = lines[-60:]
            screen_content = "\n".join(last_lines)
            
            max_chars = 4000
            if len(screen_content) > max_chars:
                screen_content = screen_content[:max_chars] + "\n...[salida truncada]"
                
            return f"Éxito: Comando enviado a la terminal: {command}\n\nSALIDA DE LA TERMINAL:\n{screen_content}"
            
        return f"Éxito: Se ha enviado el comando a la terminal: {command}"
    except Exception as e:
        logger.error(f"Error ejecutando comando en terminal/tmux: {e}")
        return f"Error al ejecutar comando en la terminal: {e}"


def _process_screen_output(output: str) -> str:
    import re
    if not output:
        return "La pantalla de la terminal está vacía."
        
    # Buscar si hay algún indicador de código de salida en el contenido capturado
    success_match = re.search(r"\[AsistenteIA: '([^']+)'\s+finalizado correctamente\]", output)
    success_match_new = re.search(r"✅\s+(\S+)\s+OK", output)
    
    exit_code_match = re.search(r"\[AsistenteIA: '([^']+)'\s+falló con código de error (\d+)\]", output)
    exit_code_match_new = re.search(r"❌\s+(\S+)\s+ERROR\s+(\d+)", output)
    
    # Devolver últimas 60 líneas para contexto suficiente
    lines = output.splitlines()
    last_lines = lines[-60:]
    screen_content = "\n".join(last_lines)
    
    # Límite inteligente: 4000 chars (~1300 tokens) para lectura de pantalla
    max_chars = 4000
    if len(screen_content) > max_chars:
        screen_content = screen_content[:max_chars] + "\n...[pantalla truncada]"
    
    if exit_code_match or exit_code_match_new:
        cmd_name = exit_code_match.group(1) if exit_code_match else exit_code_match_new.group(1)
        exit_code = int(exit_code_match.group(2)) if exit_code_match else int(exit_code_match_new.group(2))
        explanation = explain_exit_code(exit_code)
        return f"❌ ERROR EN TERMINAL: '{cmd_name}' falló (código {exit_code})\n\nQué significa: {explanation}\n\nCONTENIDO VISIBLE EN LA TERMINAL:\n\n{screen_content}"
    elif success_match or success_match_new:
        cmd_name = success_match.group(1) if success_match else success_match_new.group(1)
        explanation = explain_exit_code(0)
        return f"✅ '{cmd_name}' completado exitosamente\n\nQué significa: {explanation}\n\nCONTENIDO VISIBLE EN LA TERMINAL:\n\n{screen_content}"
    
    return "CONTENIDO VISIBLE EN LA TERMINAL:\n\n" + screen_content


async def read_terminal_screen() -> str:
    """
    Lee el contenido textual visible en la pantalla de la terminal persistente (sesión tmux 'asistenteia').
    Si hay múltiples pestañas abiertas, leerá el contenido de todas ellas para darte una visión completa.
    Usa esto para verificar el resultado de un comando que acabas de ejecutar, comprobar si
    se produjo un error, o ver si la consola está esperando interacción (ej: pidiendo contraseña de sudo).
    """
    import re
    
    # Pequeño retardo para dar tiempo a la terminal a renderizar los cambios del comando recién enviado
    await asyncio.sleep(0.8)
    
    session_name = "asistenteia"
    try:
        # Verificar si la sesión existe
        ok, _ = await _run_tmux_cmd(["has-session", "-t", session_name])
        if not ok:
            return "La terminal persistente no está iniciada (no hay sesión activa de tmux)."
            
        # 1. Obtener la lista de ventanas (pestañas) en la sesión
        ok_list, list_output = await _run_tmux_cmd(["list-windows", "-t", f"{session_name}:", "-F", "#I:#W:#{window_active}"])
        
        if ok_list and list_output:
            windows = list_output.strip().splitlines()
            # Si solo hay una pestaña, realizamos el flujo estándar directo (retrocompatible)
            if len(windows) <= 1:
                ok_capture, output = await _run_tmux_cmd(["capture-pane", "-p", "-t", f"{session_name}:"])
                if not ok_capture:
                    return f"Error al capturar la pantalla de la terminal: {output}"
                return _process_screen_output(output)
            
            # Si hay múltiples pestañas, capturamos cada una y las unimos
            combined_content = []
            for win in windows:
                parts = win.split(":")
                if len(parts) >= 3:
                    idx, name, active = parts[0], parts[1], parts[2]
                    is_active = active == "1"
                    active_label = " - ACTIVA ACTUALMENTE" if is_active else ""
                    
                    ok_cap, win_output = await _run_tmux_cmd(["capture-pane", "-p", "-t", f"{session_name}:{idx}"])
                    if ok_cap and win_output:
                        processed = _process_screen_output(win_output)
                        combined_content.append(f"=== PESTAÑA '{name}' (Índice {idx}{active_label}) ===\n{processed}\n")
            
            if combined_content:
                return "\n".join(combined_content)
            
        # Fallback si falla list-windows
        ok_capture, output = await _run_tmux_cmd(["capture-pane", "-p", "-t", f"{session_name}:"])
        if ok_capture:
            return _process_screen_output(output)
        else:
            return f"Error al capturar la pantalla de la terminal: {output}"
            
    except Exception as e:
        logger.error(f"Error en read_terminal_screen: {e}")
        return f"Excepción leyendo pantalla de la terminal: {e}"


async def send_input_to_terminal(input_text: str) -> str:
    """
    Envía una entrada de texto directa (como responder a preguntas de confirmación [Y/n] o ingresar datos)
    al flujo de entrada estándar (stdin) del proceso activo en la terminal persistente visible.
    
    Args:
        input_text: El texto exacto a enviar (ej. 'y', 'n', 'mi_contraseña').
    """
    input_text = _sanitize_tool_args(input_text)
    session_name = "asistenteia"
    try:
        # Verificar si la sesión existe
        ok, _ = await _run_tmux_cmd(["has-session", "-t", session_name])
        if not ok:
            return "La terminal persistente no está iniciada (no hay proceso activo al cual enviar entrada)."
            
        # Enviar las teclas y presionar ENTER (C-m)
        await _run_tmux_cmd(["send-keys", "-t", f"{session_name}:", input_text, "C-m"])
        return f"Éxito: Se ha enviado la entrada '{input_text}' al proceso de la terminal."
    except Exception as e:
        logger.error(f"Error en send_input_to_terminal: {e}")
        return f"Error al enviar la entrada a la terminal: {e}"


async def interrupt_terminal_command() -> str:
    """
    Envía una señal de interrupción Ctrl+C (SIGINT) al comando en ejecución en la terminal persistente
    para detener un proceso que se ha quedado bloqueado, congelado o en un bucle infinito.
    """
    session_name = "asistenteia"
    try:
        # Verificar si la sesión existe
        ok, _ = await _run_tmux_cmd(["has-session", "-t", session_name])
        if not ok:
            return "La terminal persistente no está activa (no hay ningún proceso para interrumpir)."
            
        # Enviar Ctrl+C
        await _run_tmux_cmd(["send-keys", "-t", f"{session_name}:", "C-c"])
        return "Éxito: Señal de interrupción Ctrl+C enviada a la terminal para detener el comando activo."
    except Exception as e:
        logger.error(f"Error en interrupt_terminal_command: {e}")
        return f"Error al interrumpir el comando en la terminal: {e}"


async def control_local_browser(action: str, target: str = "", value: str = "") -> str:
    """
    Controla el navegador Chromium visible en tu pantalla a través del protocolo de depuración (CDP).
    Si el navegador no está abierto con la depuración habilitada, lo lanzará automáticamente.
    La ventana permanecerá visible para que puedas ver y continuar la interacción manualmente.
    
    Args:
        action: La acción a realizar:
            'launch': Solo inicia el navegador si no está abierto.
            'navigate': Va a una URL específica (ej. target='https://google.com').
            'click': Hace clic en un elemento web usando un selector CSS (ej. target='button.submit').
            'type': Escribe texto en un campo usando un selector CSS (ej. target='input#search', value='recetas de cocina').
            'read': Lee el título y el texto visible de la pestaña activa actual.
            'look': Captura VISUALMENTE la página web actual (solo la web, no el escritorio) para
                    que el asistente la 'vea' y la analice. Úsala cuando necesites ver el aspecto o
                    el resultado visual de una página (imágenes, gráficos, mapas, layout) y no baste
                    el texto de 'read'. value='full' para capturar la página entera con scroll.
            'scroll': Hace scroll vertical (ej. value='500' para bajar, value='-500' para subir).
            'clip': Guarda la pestaña activa como nota Markdown en Obsidian (~/Documentos/Obsidian Vault/Clippings/).
                    Devuelve el resumen del contenido y la ruta del archivo guardado.
            'research': Investigación profunda y persistente sobre un tema. Navega, busca y recopila
                        información de múltiples fuentes web de forma autónoma, sin rendirse, hasta un
                        máximo de 30 pasos. Devuelve un informe completo con todo lo encontrado.
                        Usa target para el tema o pregunta a investigar.
                        Usa value para el número máximo de pasos (por defecto 30).
            'translate': Traduce una página web completa al español y la muestra en un panel overlay
                        dentro del navegador. Navega a la URL, extrae el texto, lo traduce con Google
                        Translate, e inyecta un panel visual elegante en el lado derecho de la página.
                        Usa target para la URL de la página a traducir.
                        Usa value para el idioma destino (por defecto 'es' para español).
        target: URL o selector CSS según la acción.
        value: Texto a escribir o valor de scroll.
    """
    from src.browser import (
        ensure_browser, get_page,
        browser_navigate, browser_click, browser_type, browser_read, browser_scroll,
        browser_look, browser_clip, browser_research, browser_translate,
    )
    from playwright.async_api import async_playwright

    action = _sanitize_tool_args(action)
    target = _sanitize_tool_args(target)
    value = _sanitize_tool_args(value)

    # 1. Asegurar que Chromium está corriendo con CDP
    error = await ensure_browser()
    if error:
        return error

    if action == "launch":
        return "Éxito: Navegador Chromium visible iniciado y listo con depuración habilitada en el puerto 9222."

    # 2. Conectar y despachar acción (playwright context manager mantiene la conexión viva)
    try:
        async with async_playwright() as p:
            page = await get_page(p)

            if action == "navigate":
                return await browser_navigate(page, target)
            elif action == "click":
                return await browser_click(page, target)
            elif action == "type":
                return await browser_type(page, target, value)
            elif action == "read":
                return await browser_read(page)
            elif action == "look":
                return await browser_look(page, value)
            elif action == "scroll":
                return await browser_scroll(page, value)
            elif action == "clip":
                return await browser_clip(page)
            elif action == "research":
                return await browser_research(page, target, value)
            elif action == "translate":
                return await browser_translate(page, target, value)
            else:
                return f"Error: Acción '{action}' no es una acción soportada en control_local_browser."
    except Exception as e:
        logger.error(f"Excepción en control_local_browser: {e}")
        return f"Error controlando el navegador gráfico: {e}"


async def launch_application(app_name: str) -> str:
    """
    Busca una aplicación instalada en el sistema por su nombre y la inicia de forma gráfica.
    Úsala cuando el usuario te pida abrir o iniciar una aplicación (ej: Steam, Heroic Games Launcher, Chromium, etc.).
    
    Args:
        app_name: Nombre o término de búsqueda de la aplicación a abrir.
    """
    import os
    import re
    import shutil
    from pathlib import Path
    
    app_name = _sanitize_tool_args(app_name)
    query = app_name.lower().strip()
    
    if not query:
        return "Error: Debes proporcionar el nombre de una aplicación."
        
    search_dirs = [
        Path(os.path.expanduser("~/.local/share/applications")),
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path(os.path.expanduser("~/.local/share/flatpak/exports/share/applications"))
    ]
    
    desktop_files = []
    for d in search_dirs:
        if d.exists() and d.is_dir():
            try:
                # Usar to_thread para evitar bloquear el event loop con I/O de disco
                files = await asyncio.to_thread(lambda dir_path: list(dir_path.glob("*.desktop")), d)
                desktop_files.extend(files)
            except Exception as e:
                logger.warning(f"Error listando archivos desktop en {d}: {e}")
                
    # Función síncrona para leer y analizar cada archivo .desktop
    def parse_desktop_file(filepath: Path) -> Optional[dict]:
        try:
            content = filepath.read_text(errors="ignore")
            entry = {}
            in_desktop_entry = False
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line == "[Desktop Entry]":
                    in_desktop_entry = True
                    continue
                elif line.startswith("[") and line.endswith("]"):
                    in_desktop_entry = False
                    continue
                if in_desktop_entry and "=" in line:
                    key, val = line.split("=", 1)
                    entry[key.strip()] = val.strip()
            return entry
        except Exception:
            return None
            
    matches = []
    for filepath in desktop_files:
        entry = await asyncio.to_thread(parse_desktop_file, filepath)
        if not entry:
            continue
            
        name = entry.get("Name", "")
        exec_cmd = entry.get("Exec", "")
        no_display = entry.get("NoDisplay", "false").lower() == "true"
        hidden = entry.get("Hidden", "false").lower() == "true"
        
        if no_display or hidden or not name or not exec_cmd:
            continue
            
        name_lower = name.lower()
        file_lower = filepath.name.lower()
        
        # Guardar puntuación para coincidencia inteligente
        score = 0
        if query == name_lower:
            score = 100  # Coincidencia exacta
        elif name_lower.startswith(query):
            score = 80   # Prefijo
        elif query in name_lower:
            score = 50   # Subcadena en el nombre
        elif query in file_lower:
            score = 30   # Subcadena en el nombre del archivo
            
        if score > 0:
            matches.append((score, name, exec_cmd, filepath))
            
    if not matches:
        return f"No se encontró ninguna aplicación instalada que coincida con '{app_name}'."
        
    # Ordenar por mejor coincidencia
    matches.sort(key=lambda x: x[0], reverse=True)
    best_match = matches[0]
    matched_name = best_match[1]
    exec_cmd = best_match[2]
    
    # Sanitizar el comando Exec (remover parámetros tipo %U, %f, etc.)
    cleaned_exec = re.sub(r'%[fFuUkicd]', '', exec_cmd).strip()
    
    if not cleaned_exec:
        return f"Error: Se encontró '{matched_name}', pero su comando de ejecución no es válido."
        
    # Intentar ejecutar con Hyprland dispatch primero
    if shutil.which("hyprctl"):
        try:
            # Escapar comillas dobles para que el comando Lua en Hyprland sea válido
            escaped_exec = cleaned_exec.replace('"', '\\"')
            hypr_cmd = f"hyprctl dispatch 'hl.dsp.exec_cmd(\"{escaped_exec}\")'"
            
            logger.info(f"Lanzando {matched_name} vía Hyprland Lua dispatcher: {hypr_cmd}")
            
            process = await asyncio.create_subprocess_shell(
                hypr_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0 and b"ok" in stdout.lower():
                return f"He lanzado exitosamente '{matched_name}' de forma gráfica."
            else:
                logger.warning(f"Fallo al lanzar vía hyprctl: {stdout.decode()} {stderr.decode()}. Intentando fallback...")
        except Exception as e:
            logger.error(f"Error ejecutando hyprctl dispatch para '{matched_name}': {e}")
            
    # Fallback: Lanzar usando subprocess.Popen tradicional (desacoplado)
    try:
        import shlex
        args = shlex.split(cleaned_exec)
        # Asegurarse de que el binario existe antes de lanzarlo
        binary = args[0]
        if not shutil.which(binary) and not os.path.exists(binary):
            return f"Error: El ejecutable '{binary}' para '{matched_name}' no existe en el sistema."
            
        logger.info(f"Lanzando {matched_name} vía fallback Popen: {cleaned_exec}")
        
        await asyncio.to_thread(
            subprocess.Popen,
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return f"He lanzado '{matched_name}' en segundo plano."
    except Exception as e:
        logger.error(f"Error en fallback de lanzamiento para '{matched_name}': {e}")
        return f"No se pudo iniciar la aplicación '{matched_name}'. Error: {e}"


async def close_application(app_name: str) -> str:
    """
    Busca una aplicacion activa o proceso por su nombre y la cierra de forma segura.
    Usa esta herramienta cuando el usuario pida cerrar, detener o salir de una aplicacion como Steam o Chromium.
    
    Args:
        app_name: Nombre de la aplicacion a cerrar.
    """
    import os
    import shutil
    import json
    import signal
    import re
    import shlex
    from pathlib import Path
    
    app_name = _sanitize_tool_args(app_name)
    query = app_name.lower().strip()
    
    if not query:
        return "Error: Debes proporcionar el nombre de una aplicación para cerrar."
        
    query_norm = re.sub(r'[^a-z0-9]', '', query)
    query_words = [w for w in re.split(r'[^a-z0-9]', query) if w]
    
    # 1. Buscar la aplicación en los archivos .desktop para extraer metadatos de lanzamiento (WMClass, Binario Exec)
    search_dirs = [
        Path(os.path.expanduser("~/.local/share/applications")),
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path(os.path.expanduser("~/.local/share/flatpak/exports/share/applications"))
    ]
    
    desktop_files = []
    for d in search_dirs:
        if d.exists() and d.is_dir():
            try:
                files = await asyncio.to_thread(lambda dir_path: list(dir_path.glob("*.desktop")), d)
                desktop_files.extend(files)
            except Exception as e:
                logger.warning(f"Error listando archivos desktop en {d}: {e}")
                
    def parse_desktop_file(filepath: Path) -> Optional[dict]:
        try:
            content = filepath.read_text(errors="ignore")
            entry = {}
            in_desktop_entry = False
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line == "[Desktop Entry]":
                    in_desktop_entry = True
                    continue
                elif line.startswith("[") and line.endswith("]"):
                    in_desktop_entry = False
                    continue
                if in_desktop_entry and "=" in line:
                    key, val = line.split("=", 1)
                    entry[key.strip()] = val.strip()
            return entry
        except Exception:
            return None
            
    matches = []
    for filepath in desktop_files:
        entry = await asyncio.to_thread(parse_desktop_file, filepath)
        if not entry:
            continue
            
        name = entry.get("Name", "")
        exec_cmd = entry.get("Exec", "")
        no_display = entry.get("NoDisplay", "false").lower() == "true"
        hidden = entry.get("Hidden", "false").lower() == "true"
        
        if no_display or hidden or not name or not exec_cmd:
            continue
            
        name_lower = name.lower()
        file_lower = filepath.name.lower()
        
        score = 0
        if query == name_lower:
            score = 100
        elif name_lower.startswith(query):
            score = 80
        elif query in name_lower:
            score = 50
        elif query in file_lower:
            score = 30
            
        if score > 0:
            matches.append((score, name, exec_cmd, filepath, entry))
            
    # Extraer tokens del mejor archivo .desktop coincidente (si existe)
    wm_class = ""
    exec_binary = ""
    desktop_id = ""
    app_name_parsed = ""
    
    if matches:
        matches.sort(key=lambda x: x[0], reverse=True)
        best_match = matches[0]
        matched_name = best_match[1]
        exec_cmd = best_match[2]
        filepath = best_match[3]
        entry = best_match[4]
        
        wm_class = entry.get("StartupWMClass", "").lower()
        desktop_id = filepath.stem.lower()
        app_name_parsed = matched_name.lower()
        
        cleaned_exec = re.sub(r'%[fFuUkicd]', '', exec_cmd).strip()
        if cleaned_exec:
            try:
                args = shlex.split(cleaned_exec)
                if args:
                    exec_binary = Path(args[0]).name.lower()
            except Exception:
                pass
                
    # Compilar un set de tokens de búsqueda
    search_tokens = {query, query_norm}
    if wm_class:
        search_tokens.add(wm_class.lower())
        search_tokens.add(re.sub(r'[^a-z0-9]', '', wm_class.lower()))
    if exec_binary:
        search_tokens.add(exec_binary.lower())
        search_tokens.add(re.sub(r'[^a-z0-9]', '', exec_binary.lower()))
        if "." in exec_binary:
            search_tokens.add(exec_binary.split(".")[0])
    if desktop_id:
        search_tokens.add(desktop_id.lower())
        search_tokens.add(re.sub(r'[^a-z0-9]', '', desktop_id.lower()))
    if app_name_parsed:
        search_tokens.add(app_name_parsed.lower())
        search_tokens.add(re.sub(r'[^a-z0-9]', '', app_name_parsed.lower()))
        
    search_tokens = {t for t in search_tokens if t and len(t) > 2}
    
    closed_graphical = False
    closed_pids = []
    matched_app_name = ""
    
    # 2. Intentar cerrarla como ventana gráfica en Hyprland
    if shutil.which("hyprctl"):
        try:
            process = await asyncio.create_subprocess_exec(
                "hyprctl", "clients", "-j",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                clients = json.loads(stdout.decode(errors="ignore"))
                graphical_matches = []
                
                for client in clients:
                    c_class = client.get("class", "").lower()
                    c_title = client.get("title", "").lower()
                    c_init_class = client.get("initialClass", "").lower()
                    c_init_title = client.get("initialTitle", "").lower()
                    address = client.get("address", "")
                    pid = client.get("pid", 0)
                    
                    if not address:
                        continue
                        
                    c_class_norm = re.sub(r'[^a-z0-9]', '', c_class)
                    c_init_class_norm = re.sub(r'[^a-z0-9]', '', c_init_class)
                    
                    score = 0
                    if any(t == c_class or t == c_init_class or t == c_class_norm or t == c_init_class_norm for t in search_tokens):
                        score = 100
                    elif any(t in c_class or t in c_init_class or c_class in t or c_init_class in t for t in search_tokens):
                        score = 80
                    elif any(t in c_title or t in c_init_title for t in search_tokens):
                        score = 60
                    elif query_words and all(w in c_title or w in c_init_title for w in query_words):
                        score = 40
                        
                    if score > 0:
                        graphical_matches.append((score, client.get("class", "Aplicación"), address, pid))
                        
                if graphical_matches:
                    graphical_matches.sort(key=lambda x: x[0], reverse=True)
                    best_match = graphical_matches[0]
                    matched_app_name = best_match[1]
                    
                    target_class = best_match[1].lower()
                    windows_to_close = [m for m in graphical_matches if m[1].lower() == target_class]
                    
                    logger.info(f"Cerrando {len(windows_to_close)} ventanas de clase '{best_match[1]}' vía Hyprland")
                    for _, _, address, pid in windows_to_close:
                        close_cmd = f"hyprctl dispatch closewindow address:{address}"
                        proc = await asyncio.create_subprocess_shell(
                            close_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        await proc.communicate()
                        closed_graphical = True
                        if pid > 0:
                            closed_pids.append(pid)
        except Exception as e:
            logger.error(f"Error cerrando ventanas vía Hyprland para '{app_name}': {e}")

    # 3. Fallback/Complemento: Buscar y terminar todos los procesos coincidentes en /proc
    own_pid = os.getpid()
    
    def scan_proc_for_tokens() -> list[tuple[int, str]]:
        found = []
        for path in Path("/proc").glob("[0-9]*"):
            try:
                pid = int(path.name)
                if pid == own_pid:
                    continue
                    
                comm_path = path / "comm"
                comm = ""
                if comm_path.exists():
                    comm = comm_path.read_text(errors="ignore").strip().lower()
                
                cmdline_path = path / "cmdline"
                cmdline = ""
                if cmdline_path.exists():
                    cmdline = cmdline_path.read_text(errors="ignore").replace("\x00", " ").lower()
                
                if "asistenteia" in cmdline or "antigravity" in cmdline:
                    continue
                
                comm_norm = re.sub(r'[^a-z0-9]', '', comm)
                cmdline_norm = re.sub(r'[^a-z0-9]', '', cmdline)
                
                matched = False
                # Comprobar tokens del Exec o StartupWMClass en comm/cmdline
                if any(t == comm or t == comm_norm or t in cmdline for t in search_tokens if len(t) > 2):
                    matched = True
                # Comprobar palabras de consulta en comm/cmdline
                elif query_words and all(w in comm or w in cmdline for w in query_words):
                    matched = True
                
                if matched:
                    name = comm if comm else (cmdline.split()[0] if cmdline.strip() else "proceso")
                    found.append((pid, name))
            except (ValueError, OSError, PermissionError):
                continue
        return found

    try:
        proc_matches = await asyncio.to_thread(scan_proc_for_tokens)
        if proc_matches:
            logger.info(f"Procesos a cerrar para '{app_name}': {proc_matches}")
            killed_names = set()
            for pid, name in proc_matches:
                try:
                    os.kill(pid, signal.SIGTERM)
                    closed_pids.append(pid)
                    killed_names.add(name)
                except OSError:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        closed_pids.append(pid)
                        killed_names.add(name)
                    except OSError:
                        pass
            if closed_pids:
                apps_list = ", ".join(killed_names)
                return f"He cerrado los procesos de '{apps_list}' (PIDs: {closed_pids})."
    except Exception as e:
        logger.error(f"Error terminando procesos para '{app_name}': {e}")
        
    if closed_graphical:
        return f"He cerrado las ventanas de '{matched_app_name}' de forma gráfica."
        
    return f"No encontré ninguna aplicación activa o proceso en ejecución para '{app_name}'."




