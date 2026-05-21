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
                stderr_msg = stderr.decode().strip()
                explanation = explain_exit_code(process.returncode)
                error_msg = stderr_msg if stderr_msg else explanation
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
    Usa esto para abrir cualquier aplicación instalada o para ejecutar cualquier comando de consola.
    Se ejecutará dentro de la terminal persistente visible para el usuario.
    
    Args:
        command: El binario o comando exacto a ejecutar.
    """
    command = _sanitize_tool_args(command)
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


async def open_terminal_and_run_command(command: str) -> str:
    """
    Abre una terminal gráfica visible (como Alacritty, Kitty o Foot) y ejecuta un comando específico en ella.
    Si ya hay una terminal de AsistenteIA abierta, enviará y ejecutará el comando en la misma ventana.
    Mantiene la ventana de la terminal abierta para que el usuario pueda ver el resultado,
    interactuar con ella o escribir su contraseña de administrador (sudo) si es necesario.
    
    Args:
        command: El comando exacto de Linux que se ejecutará dentro de la terminal.
    """
    import shutil
    import subprocess
    import asyncio
    
    command = _sanitize_tool_args(command)
    session_name = "asistenteia"
    
    # Envolver el comando para capturar el código de salida y mostrar un banner profesional si no es background
    wrapped_command = command
    if not command.strip().endswith("&"):
        # Extraer el nombre base del comando para el banner
        cmd_name = command.strip().split()[0].split("/")[-1] if command.strip() else "comando"
        wrapped_command = (
            f"{{ {command.strip()} ; }} ; EXIT_CODE=$? ; "
            f"if [ $EXIT_CODE -eq 0 ]; then "
            f"echo -e \"\\n\\033[1;30m[AsistenteIA: '{cmd_name}' finalizado correctamente]\\033[0m\"; "
            f"else "
            f"echo -e \"\\n\\033[1;31m[AsistenteIA: '{cmd_name}' falló con código de error $EXIT_CODE]\\033[0m\"; "
            f"fi"
        )
    
    # 1. Comprobar si la sesión de tmux existe y si está activa en pantalla (attached)
    session_exists = False
    session_attached = False
    
    try:
        check_session = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if check_session.returncode == 0:
            session_exists = True
            
            # Ver si hay clientes conectados/adjuntos a esa sesión
            check_attached = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name} #{session_attached}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            for line in check_attached.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == session_name:
                    if parts[1] != "0":
                        session_attached = True
                    break
    except Exception as e:
        logger.error(f"Error comprobando estado de tmux: {e}")

    # 2. Si la terminal ya está abierta y acoplada a la sesión, enviamos el comando directamente
    if session_attached:
        try:
            logger.info(f"Enviando comando a sesión de tmux existente: {command}")
            # Mandamos el comando envuelto y un ENTER (C-m)
            subprocess.run(["tmux", "send-keys", "-t", session_name, wrapped_command, "C-m"], check=True)
            return f"Éxito: Se ha enviado el comando a la terminal abierta: {command}"
        except Exception as e:
            logger.error(f"Error enviando comando a tmux: {e}")
            return f"Error al enviar comando a la terminal activa: {e}"

    # 3. Si no está abierta o acoplada, abrimos la ventana de la terminal
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
        
        # Esperar un momento a que la terminal y tmux se inicien/adjunten gráficamente antes de mandar las teclas
        await asyncio.sleep(0.8)
        
        # Enviar el comando envuelto
        subprocess.run(["tmux", "send-keys", "-t", session_name, wrapped_command, "C-m"], check=True)
        
        return f"Éxito: Se ha abierto una ventana de {chosen_terminal.capitalize()} y ejecutado: {command}"
    except Exception as e:
        logger.error(f"Error abriendo terminal/tmux: {e}")
        return f"Error al abrir la terminal e iniciar la sesión persistente: {e}"


async def read_terminal_screen() -> str:
    """
    Lee el contenido textual visible en la pantalla de la terminal persistente (sesión tmux 'asistenteia').
    Usa esto para verificar el resultado de un comando que acabas de ejecutar, comprobar si
    se produjo un error, o ver si la consola está esperando interacción (ej: pidiendo contraseña de sudo).
    """
    import subprocess
    import asyncio
    import re
    
    # Pequeño retardo para dar tiempo a la terminal a renderizar los cambios del comando recién enviado
    await asyncio.sleep(0.8)
    
    session_name = "asistenteia"
    try:
        # Verificar si la sesión existe
        check_session = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if check_session.returncode != 0:
            return "La terminal persistente no está iniciada (no hay sesión activa de tmux)."
            
        # Capturar el panel activo de la sesión de tmux
        capture = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if capture.returncode == 0:
            output = capture.stdout.strip()
            if not output:
                return "La pantalla de la terminal está vacía."
                
            # Buscar si hay algún indicador de código de salida en el contenido capturado
            # El patrón es: [AsistenteIA: 'comando' finalizado correctamente] o [AsistenteIA: 'comando' falló con código de error X]
            success_match = re.search(r"\[AsistenteIA: '([^']+)'\s+finalizado correctamente\]", output)
            exit_code_match = re.search(r"\[AsistenteIA: '([^']+)'\s+falló con código de error (\d+)\]", output)
            
            # Devolver las últimas 40 líneas para que el contexto no se sature pero tenga suficiente detalle
            lines = output.splitlines()
            last_lines = lines[-40:]
            screen_content = "\n".join(last_lines)
            
            if exit_code_match:
                cmd_name = exit_code_match.group(1)
                exit_code = int(exit_code_match.group(2))
                explanation = explain_exit_code(exit_code)
                return f"❌ ERROR EN TERMINAL: '{cmd_name}' falló (código {exit_code})\n\nQué significa: {explanation}\n\nCONTENIDO VISIBLE EN LA TERMINAL:\n\n{screen_content}"
            elif success_match:
                cmd_name = success_match.group(1)
                explanation = explain_exit_code(0)
                return f"✅ '{cmd_name}' completado exitosamente\n\nQué significa: {explanation}\n\nCONTENIDO VISIBLE EN LA TERMINAL:\n\n{screen_content}"
            
            return "CONTENIDO VISIBLE EN LA TERMINAL:\n\n" + screen_content
        else:
            return f"Error al capturar la pantalla de la terminal: {capture.stderr.strip()}"
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
    import subprocess
    input_text = _sanitize_tool_args(input_text)
    session_name = "asistenteia"
    try:
        # Verificar si la sesión existe
        check_session = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if check_session.returncode != 0:
            return "La terminal persistente no está iniciada (no hay proceso activo al cual enviar entrada)."
            
        # Enviar las teclas y presionar ENTER (C-m)
        subprocess.run(["tmux", "send-keys", "-t", session_name, input_text, "C-m"], check=True)
        return f"Éxito: Se ha enviado la entrada '{input_text}' al proceso de la terminal."
    except Exception as e:
        logger.error(f"Error en send_input_to_terminal: {e}")
        return f"Error al enviar la entrada a la terminal: {e}"


async def interrupt_terminal_command() -> str:
    """
    Envía una señal de interrupción Ctrl+C (SIGINT) al comando en ejecución en la terminal persistente
    para detener un proceso que se ha quedado bloqueado, congelado o en un bucle infinito.
    """
    import subprocess
    session_name = "asistenteia"
    try:
        # Verificar si la sesión existe
        check_session = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if check_session.returncode != 0:
            return "La terminal persistente no está activa (no hay ningún proceso para interrumpir)."
            
        # Enviar Ctrl+C
        subprocess.run(["tmux", "send-keys", "-t", session_name, "C-c"], check=True)
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
    import socket
    import subprocess
    import shutil
    import os
    import asyncio
    from playwright.async_api import async_playwright
    
    action = _sanitize_tool_args(action)
    target = _sanitize_tool_args(target)
    value = _sanitize_tool_args(value)

    def _is_port_open(port: int = 9222) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except Exception:
            return False

    # 1. Comprobar si Chromium está activo con CDP habilitado
    if not _is_port_open(9222):
        # Intentar lanzarlo
        profile_path = "/home/victor/develop/asistenteia/.chrome-profile"
        os.makedirs(profile_path, exist_ok=True)
        
        binary = shutil.which("chromium") or shutil.which("google-chrome-stable")
        if not binary:
            return "Error: No se encontró Chromium o Google Chrome en el sistema."
            
        args = [
            binary,
            "--remote-debugging-port=9222",
            f"--user-data-dir={profile_path}",
            "--no-first-run",
            "--no-default-browser-check"
        ]
        logger.info(f"Lanzando Chromium visible con CDP habilitado en puerto 9222...")
        subprocess.Popen(args, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Esperar a que el puerto responda
        for _ in range(10):
            await asyncio.sleep(0.5)
            if _is_port_open(9222):
                break
        else:
            return "Error: Se intentó lanzar Chromium pero no se detectó respuesta en el puerto 9222."

    if action == "launch":
        return "Éxito: Navegador Chromium visible iniciado y listo con depuración habilitada en el puerto 9222."

    # 2. Conectarse y realizar la acción mediante Playwright
    try:
        async with async_playwright() as p:
            logger.info("Conectando a Chromium visible vía CDP...")
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            
            # Obtener el contexto y asegurar que hay al menos una pestaña
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            
            if action == "navigate":
                if not target:
                    return "Error: Especifica la URL a navegar en el argumento 'target'."
                if not target.startswith("http"):
                    target = "https://" + target
                
                logger.info(f"Navegando a {target}...")
                await page.goto(target, wait_until="networkidle", timeout=15000)
                title = await page.title()
                return f"Éxito: Navegado correctamente a '{target}' (Título: '{title}')."
                
            elif action == "click":
                if not target:
                    return "Error: Especifica el selector CSS para hacer clic en el argumento 'target'."
                
                logger.info(f"Haciendo clic en selector '{target}'...")
                await page.wait_for_selector(target, timeout=5000)
                await page.click(target)
                await asyncio.sleep(1.0) # Esperar a que renderice la respuesta
                return f"Éxito: Se hizo clic en el elemento con selector '{target}'."
                
            elif action == "type":
                if not target:
                    return "Error: Especifica el selector CSS en 'target' para escribir."
                
                logger.info(f"Escribiendo texto en '{target}'...")
                await page.wait_for_selector(target, timeout=5000)
                await page.click(target)
                # Simular escritura real con pequeños retrasos entre teclas
                await page.type(target, value, delay=50)
                await asyncio.sleep(0.5)
                return f"Éxito: Se escribió correctamente '{value}' en el elemento '{target}'."
                
            elif action == "read":
                title = await page.title()
                content = await page.evaluate("document.body.innerText")
                snippet = content[:3000] + "\n\n[Contenido de la página truncado por longitud...]" if len(content) > 3000 else content
                return f"INFORMACIÓN DE PESTAÑA ACTIVA:\n- TÍTULO: {title}\n- URL: {page.url}\n\nCONTENIDO TEXTUAL:\n\n{snippet}"
                
            elif action == "scroll":
                scroll_amount = 500
                if value:
                    try:
                        scroll_amount = int(value)
                    except ValueError:
                        pass
                logger.info(f"Haciendo scroll vertical de {scroll_amount}px...")
                await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                return f"Éxito: Se desplazó la página verticalmente {scroll_amount} píxeles."
                
            elif action == "clip":
                import re as _re
                from datetime import datetime
                
                title = await page.title()
                url = page.url
                logger.info(f"Guardando clip de '{title}' en Obsidian...")
                
                # 1. Extraer texto limpio usando trafilatura (sin anuncios ni menús)
                clean_content = None
                try:
                    import trafilatura
                    def _fetch_clean():
                        downloaded = trafilatura.fetch_url(url)
                        if downloaded:
                            return trafilatura.extract(
                                downloaded,
                                include_comments=False,
                                include_tables=True,
                                favor_recall=True
                            )
                        return None
                    clean_content = await asyncio.to_thread(_fetch_clean)
                except Exception as e:
                    logger.warning(f"trafilatura falló ({e}), usando innerText como fallback.")
                
                # Fallback: texto visible de la página
                if not clean_content:
                    clean_content = await page.evaluate("document.body.innerText")
                
                if not clean_content:
                    return "Error: No se pudo extraer contenido textual de la página actual."
                
                # 2. Construir el nombre del archivo (slug limpio del título)
                now = datetime.now()
                date_str = now.strftime("%Y-%m-%d")
                time_str = now.strftime("%H:%M")
                slug = _re.sub(r'[^\w\s-]', '', title.lower())
                slug = _re.sub(r'[\s_-]+', '-', slug).strip('-')[:60]
                filename = f"{date_str} - {slug}.md"
                
                vault_dir = "/home/victor/Documentos/Obsidian Vault/Clippings"
                filepath = os.path.join(vault_dir, filename)
                
                # 3. Generar el frontmatter YAML con metadatos enriquecidos
                frontmatter = (
                    f"---\n"
                    f"título: \"{title}\"\n"
                    f"url: {url}\n"
                    f"fecha_captura: {date_str} {time_str}\n"
                    f"etiquetas: [clipping, por-revisar]\n"
                    f"---\n\n"
                )
                
                # 4. Construir el contenido final del archivo Markdown
                # Truncar a 15.000 chars para no saturar el vault con artículos enormes
                max_chars = 15000
                body = clean_content
                truncated = False
                if len(body) > max_chars:
                    body = body[:max_chars]
                    truncated = True
                
                md_content = (
                    frontmatter +
                    f"# {title}\n\n"
                    f"> **Fuente:** [{url}]({url})\n\n"
                    f"---\n\n"
                    f"{body}\n"
                )
                if truncated:
                    md_content += "\n\n---\n*[Contenido truncado — artículo completo disponible en la URL fuente]*\n"
                
                # 5. Escribir el archivo en el Vault de Obsidian
                os.makedirs(vault_dir, exist_ok=True)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(md_content)
                
                # 6. Devolver resumen MUY COMPACTO al modelo (el informe completo ya está en Obsidian)
                word_count = len(clean_content.split())
                # Solo los primeros 400 chars para no saturar el contexto del LLM
                summary_preview = clean_content[:400].strip()
                
                return (
                    f"CLIP GUARDADO EN OBSIDIAN:\n"
                    f"- Archivo: {filename}\n"
                    f"- Palabras extraídas: {word_count}\n"
                    f"EXTRACTO INICIAL (para tu resumen oral al usuario):\n{summary_preview}\n"
                )
                
            elif action == "research":
                import re as _re
                from datetime import datetime
                import trafilatura

                if not target:
                    return "Error: Especifica el tema o pregunta a investigar en el argumento 'target'."

                max_steps = 30
                if value:
                    try:
                        max_steps = min(int(value), 30)
                    except ValueError:
                        pass

                query = target
                logger.info(f"Iniciando investigación profunda ({max_steps} pasos máx): {query}")

                # --- Estado de la investigación ---
                visited_urls = set()
                # Lista de (url, titulo, extracto) recopilados
                gathered: list[dict] = []
                # Cola de URLs pendientes por visitar
                pending_urls: list[tuple[str, str]] = []  # (url, titulo)
                step = 0
                search_attempts = 0
                # Variaciones de búsqueda para cuando el primer intento no es suficiente
                search_variants = [
                    query,
                    query + " explicación detallada",
                    query + " site:wikipedia.org",
                    query + " tutorial guía",
                    query + " cómo funciona",
                ]

                def _extract_url(url: str) -> str | None:
                    """Extrae texto limpio de una URL usando trafilatura."""
                    try:
                        downloaded = trafilatura.fetch_url(url)
                        if not downloaded:
                            return None
                        return trafilatura.extract(
                            downloaded,
                            include_comments=False,
                            include_tables=True,
                            favor_recall=True
                        )
                    except Exception:
                        return None

                # --- BUCLE PRINCIPAL DE INVESTIGACIÓN ---
                while step < max_steps:
                    step += 1
                    logger.info(f"[Research] Paso {step}/{max_steps} | Fuentes: {len(gathered)} | Pendientes: {len(pending_urls)}")

                    # FASE 1: Si no hay URLs pendientes, lanzar una nueva búsqueda
                    if not pending_urls:
                        if search_attempts >= len(search_variants):
                            logger.info("[Research] Agotadas todas las variantes de búsqueda.")
                            break

                        current_query = search_variants[search_attempts]
                        search_attempts += 1
                        logger.info(f"[Research] Búsqueda #{search_attempts}: '{current_query}'")

                        # Navegar a Google con la query (más fiable para extracción de links)
                        encoded_q = current_query.replace(' ', '+')
                        search_url = f"https://www.google.com/search?q={encoded_q}&hl=es"
                        try:
                            await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                            await asyncio.sleep(2.0)  # Dar tiempo a Google a renderizar
                        except Exception as e:
                            logger.warning(f"[Research] Error navegando a Google: {e}")
                            # Fallback a DuckDuckGo si Google falla
                            try:
                                ddg_url = f"https://duckduckgo.com/?q={encoded_q}"
                                await page.goto(ddg_url, wait_until="domcontentloaded", timeout=12000)
                                await asyncio.sleep(2.0)
                            except Exception as e2:
                                logger.warning(f"[Research] Fallback DDG también falló: {e2}")
                                continue

                        # Extraer enlaces de resultados orgánicos de Google
                        links = await page.evaluate("""
                            () => {
                                const results = [];
                                const seen = new Set();
                                // Selectores de resultados orgánicos de Google
                                const selectors = [
                                    '#search a[href^="https"]',
                                    '#rso a[href^="https"]',
                                    '.g a[href^="https"]',
                                    'a[href^="https"]'
                                ];
                                const skipDomains = ['google.com', 'google.es', 'youtube.com',
                                                    'accounts.google', 'support.google',
                                                    'maps.google', 'translate.google',
                                                    'webcache', 'policies.google'];
                                for (const sel of selectors) {
                                    document.querySelectorAll(sel).forEach(a => {
                                        const href = a.href || '';
                                        const text = (a.innerText || a.textContent || '').trim();
                                        const skip = skipDomains.some(d => href.includes(d));
                                        if (href && !skip && !seen.has(href) && text.length > 5) {
                                            seen.add(href);
                                            results.push({url: href, title: text.substring(0, 120)});
                                        }
                                    });
                                    if (results.length >= 10) break;
                                }
                                return results.slice(0, 10);
                            }
                        """)

                        logger.info(f"[Research] Links extraídos de búsqueda: {len(links)}")

                        for link in links:
                            url_item = link.get('url', '')
                            title_item = link.get('title', url_item)
                            if url_item and url_item not in visited_urls:
                                pending_urls.append((url_item, title_item))

                        logger.info(f"[Research] {len(pending_urls)} URLs encoladas.")
                        continue

                    # FASE 2: Visitar la siguiente URL pendiente
                    next_url, next_title = pending_urls.pop(0)

                    if next_url in visited_urls:
                        continue
                    visited_urls.add(next_url)

                    logger.info(f"[Research] Visitando: {next_url}")

                    # Navegar en el navegador visible (el usuario ve el progreso)
                    try:
                        await page.goto(next_url, wait_until="domcontentloaded", timeout=12000)
                        await asyncio.sleep(1.0)
                    except Exception as e:
                        logger.warning(f"[Research] No se pudo navegar a {next_url}: {e}")
                        continue

                    # Extraer contenido limpio con trafilatura (más preciso que innerText)
                    content = await asyncio.to_thread(_extract_url, next_url)

                    # Fallback a innerText si trafilatura falla
                    if not content:
                        try:
                            content = await page.evaluate("document.body.innerText")
                        except Exception:
                            content = None

                    if content and len(content.strip()) > 200:
                        # Guardar hasta 2500 chars por fuente para no saturar
                        snippet = content.strip()[:2500]
                        page_title = await page.title()
                        gathered.append({
                            "url": next_url,
                            "titulo": page_title or next_title,
                            "contenido": snippet,
                            "paso": step
                        })
                        logger.info(f"[Research] Fuente #{len(gathered)} añadida: '{page_title}' ({len(snippet)} chars)")

                        # Si ya tenemos 8+ fuentes ricas, podemos parar antes
                        if len(gathered) >= 8:
                            logger.info("[Research] Suficientes fuentes recopiladas. Finalizando bucle.")
                            break

                # --- COMPILAR EL INFORME FINAL ---
                if not gathered:
                    return f"Investigación completada en {step} pasos pero no se encontró contenido útil sobre: {query}"

                now = datetime.now()
                report_lines = [
                    f"INFORME DE INVESTIGACIÓN PROFUNDA",
                    f"Tema: {query}",
                    f"Pasos ejecutados: {step}/{max_steps}",
                    f"Fuentes recopiladas: {len(gathered)}",
                    f"Fecha: {now.strftime('%Y-%m-%d %H:%M')}",
                    "=" * 60,
                    ""
                ]

                for i, src in enumerate(gathered, 1):
                    report_lines.append(f"--- FUENTE {i}: {src['titulo']} ---")
                    report_lines.append(f"URL: {src['url']}")
                    report_lines.append(f"Paso de captura: {src['paso']}")
                    report_lines.append("")
                    report_lines.append(src['contenido'])
                    report_lines.append("")

                full_report = "\n".join(report_lines)

                # Guardar también en Obsidian como nota de investigación
                try:
                    vault_dir = "/home/victor/Documentos/Obsidian Vault/Clippings"
                    os.makedirs(vault_dir, exist_ok=True)
                    date_str = now.strftime("%Y-%m-%d")
                    slug = _re.sub(r'[^\w\s-]', '', query.lower())
                    slug = _re.sub(r'[\s_-]+', '-', slug).strip('-')[:50]
                    filename = f"{date_str} - investigacion - {slug}.md"
                    filepath = os.path.join(vault_dir, filename)

                    md_lines = [
                        f"---",
                        f"título: \"Investigación: {query}\"",
                        f"tipo: investigacion",
                        f"fecha_captura: {date_str} {now.strftime('%H:%M')}",
                        f"fuentes: {len(gathered)}",
                        f"pasos: {step}",
                        f"etiquetas: [investigacion, por-revisar]",
                        f"---",
                        f"",
                        f"# Investigación: {query}",
                        f"",
                        f"**Pasos ejecutados:** {step} | **Fuentes:** {len(gathered)} | **Fecha:** {now.strftime('%Y-%m-%d %H:%M')}",
                        f"",
                        f"---",
                        f"",
                    ]
                    for i, src in enumerate(gathered, 1):
                        md_lines.append(f"## Fuente {i}: {src['titulo']}")
                        md_lines.append(f"> [{src['url']}]({src['url']})")
                        md_lines.append(f"")
                        md_lines.append(src['contenido'])
                        md_lines.append(f"")
                        md_lines.append(f"---")
                        md_lines.append(f"")

                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write("\n".join(md_lines))
                    logger.info(f"[Research] Informe guardado en Obsidian: {filename}")
                    obsidian_note = filename
                except Exception as e:
                    logger.warning(f"[Research] No se pudo guardar en Obsidian: {e}")
                    obsidian_note = "(no guardado)"

                # Devolver RESUMEN COMPACTO al modelo (máx ~1200 chars)
                # El informe completo ya está en Obsidian. El modelo solo necesita
                # los titulares y un extracto de cada fuente para hablar con el usuario.
                compact_lines = [
                    f"INVESTIGACIÓN COMPLETADA: '{query}'",
                    f"Pasos: {step} | Fuentes: {len(gathered)} | Nota Obsidian: {obsidian_note}",
                    ""
                ]
                chars_budget = 900  # dejar margen para el resto del contexto
                for i, src in enumerate(gathered, 1):
                    entry = f"[{i}] {src['titulo']} ({src['url']})\n{src['contenido'][:120]}..."
                    if sum(len(l) for l in compact_lines) + len(entry) > chars_budget:
                        compact_lines.append(f"... y {len(gathered)-i+1} fuentes más en Obsidian.")
                        break
                    compact_lines.append(entry)
                    compact_lines.append("")

                return "\n".join(compact_lines)

            elif action == "translate":
                if not target:
                    return "Error: Especifica la URL de la página a traducir en el argumento 'target'."

                url = target if target.startswith("http") else "https://" + target
                lang = value if value else "es"
                logger.info(f"Traduciendo página a '{lang}': {url}")

                # 1. Navegar a la página
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(1.5)
                except Exception as e:
                    return f"Error navegando a {url}: {e}"

                page_title = await page.title()
                logger.info(f"Página cargada: {page_title}")

                # 2. Extraer texto estructurado de la página
                _JS_EXTRACT = """() => {
                    const skipTags = new Set(['SCRIPT','STYLE','NOSCRIPT','NAV','FOOTER','HEADER','ASIDE','IFRAME']);
                    const sel = 'h1,h2,h3,h4,h5,h6,p,li,td,th,figcaption,blockquote,dd,dt,article,section';
                    const blocks = [];
                    document.querySelectorAll(sel).forEach(el => {
                        if (skipTags.has(el.tagName)) return;
                        const t = el.innerText.trim();
                        if (t.length > 2) {
                            const tag = el.tagName.toLowerCase();
                            if (tag.length === 2 && tag.charAt(0) === 'h' && '123456'.indexOf(tag.charAt(1)) !== -1) {
                                blocks.push('\\n## ' + t + '\\n');
                            } else if (tag === 'li') {
                                blocks.push('- ' + t);
                            } else {
                                blocks.push(t);
                            }
                        }
                    });
                    return blocks.join('\\n\\n');
                }"""
                raw_text = await page.evaluate(_JS_EXTRACT)

                if not raw_text or len(raw_text.strip()) < 10:
                    raw_text = await page.evaluate("document.body.innerText")
                    if not raw_text or len(raw_text.strip()) < 10:
                        return f"No se pudo extraer texto traducible de {url}."

                MAX_CHARS = 12000
                truncated = False
                if len(raw_text) > MAX_CHARS:
                    raw_text = raw_text[:MAX_CHARS]
                    truncated = True
                    logger.info(f"Texto truncado a {MAX_CHARS} caracteres para traducción")

                # 3. Traducir con Google Translate (deep-translator)
                try:
                    from deep_translator import GoogleTranslator
                except ImportError:
                    try:
                        import subprocess as _sp
                        _sp.run(
                            ["pip3", "install", "deep-translator", "--break-system-packages", "-q"],
                            check=True, timeout=60
                        )
                        from deep_translator import GoogleTranslator
                    except Exception:
                        return "Error: Instala deep-translator con: pip install deep-translator"

                def _translate_chunks(text: str, target_lang: str) -> str:
                    translator = GoogleTranslator(source='auto', target=target_lang)
                    CHUNK_SIZE = 4500
                    parts = []
                    start = 0
                    while start < len(text):
                        end = min(start + CHUNK_SIZE, len(text))
                        chunk = text[start:end]
                        if end < len(text):
                            nl = chunk.rfind('\n')
                            if nl > CHUNK_SIZE // 2:
                                chunk = chunk[:nl]
                                start += len(chunk) + 1
                            else:
                                start = end
                        else:
                            start = end
                        try:
                            t = translator.translate(chunk)
                            parts.append(t if t else chunk)
                        except Exception:
                            parts.append(chunk)
                    return '\n\n'.join(parts)

                translated = await asyncio.to_thread(_translate_chunks, raw_text, lang)

                if not translated or not translated.strip():
                    return "Error: La traducción devolvió un resultado vacío."

                # 4. Inyectar panel de traducción en la página (overlay visual)
                _JS_OVERLAY = """(data) => {
            const overlay = document.createElement('div');
            overlay.id = '_asistenteia_tr';
            overlay.style.cssText = 'position:fixed;top:0;right:0;width:50%;height:100vh;background:rgba(255,255,255,0.97);overflow-y:auto;z-index:2147483647;font-family:system-ui,sans-serif;font-size:15px;line-height:1.75;color:#1a1a1a;padding:28px 32px;border-left:4px solid #1a73e8;box-shadow:-6px 0 30px rgba(0,0,0,0.18)';
            const existing = document.getElementById('_asistenteia_tr');
            if (existing) existing.remove();
            if (!document.getElementById('_trStyle')) {
                const style = document.createElement('style');
                style.id = '_trStyle';
                style.textContent = '@keyframes _trSlideIn{from{transform:translateX(100%)}to{transform:translateX(0)}}';
                document.head.appendChild(style);
                overlay.style.animation = '_trSlideIn 0.35s ease-out';
            } else {
                overlay.style.animation = '_trSlideIn 0.35s ease-out';
            }
            const closeBtn = document.createElement('button');
            closeBtn.textContent = '\\u2715';
            closeBtn.style.cssText = 'position:sticky;top:0;float:right;background:#1a73e8;color:white;border:none;border-radius:50%;width:36px;height:36px;font-size:20px;cursor:pointer;box-shadow:0 2px 10px rgba(26,115,232,0.4);z-index:2;display:flex;align-items:center;justify-content:center';
            closeBtn.onmouseover = function() { closeBtn.style.background = '#1557b0'; };
            closeBtn.onmouseout = function() { closeBtn.style.background = '#1a73e8'; };
            closeBtn.onclick = function() { overlay.remove(); };
            overlay.appendChild(closeBtn);
            const header = document.createElement('div');
            header.style.cssText = 'clear:both;margin-bottom:20px';
            header.innerHTML = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px"><span style="font-size:28px">\\uD83C\\uDF10</span><h2 style="color:#1a73e8;margin:0;font-size:22px">Traduccion al ' + data.lang.toUpperCase() + '</h2></div><p style="color:#666;margin:0;font-size:13px"><strong>' + data.title + '</strong><br><a href="' + data.url + '" style="color:#1a73e8">' + data.url + '</a></p><hr style="border:none;border-top:1px solid #e0e0e0;margin:14px 0">';
            overlay.appendChild(header);
            const contentEl = document.createElement('div');
            contentEl.style.cssText = 'max-width:100%;word-wrap:break-word';
            const safe = data.translated.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const lines = safe.split('\\n');
            let html = '';
            for (let i = 0; i < lines.length; i++) {
                const line = lines[i];
                if (line.startsWith('## ')) {
                    html += '<h3 style="color:#1a73e8;margin:18px 0 8px;font-size:18px">' + line.substring(3) + '</h3>';
                } else if (line.startsWith('- ')) {
                    html += '<li style="margin-left:20px;margin-bottom:4px">' + line.substring(2) + '</li>';
                } else if (line.trim() === '') {
                    html += '<br>';
                } else {
                    html += line;
                }
            }
            contentEl.innerHTML = '<p style="margin:0 0 12px">' + html + '</p>';
            if (data.truncated) {
                contentEl.innerHTML += '<p style="color:#999;font-style:italic;margin-top:16px">[Contenido truncado]</p>';
            }
            overlay.appendChild(contentEl);
            document.body.appendChild(overlay);
            return 'ok';
        }"""
                overlay_result = await page.evaluate(
                    _JS_OVERLAY,
                    {"translated": translated, "title": page_title, "url": url, "lang": lang, "truncated": truncated}
                )

                word_count = len(translated.split())
                logger.info(f"Traducción completada: {word_count} palabras, idioma: {lang}")

                return (
                    f"TRADUCCIÓN COMPLETADA\n"
                    f"Página: {page_title}\n"
                    f"URL: {url}\n"
                    f"Palabras traducidas: {word_count}\n"
                    f"Idioma destino: {lang}\n\n"
                    f"Se ha inyectado un panel de traducción en el lado derecho del navegador.\n"
                    f"El usuario puede ver la traducción directamente en la página.\n"
                    f"Pulsa el botón ✕ del panel para cerrarlo."
                )

            else:
                return f"Error: Acción '{action}' no es una acción soportada en control_local_browser."
    except Exception as e:
        logger.error(f"Excepción en control_local_browser: {e}")
        return f"Error controlando el navegador gráfico: {e}"


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
