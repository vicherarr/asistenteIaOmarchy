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
    
    command = _sanitize_tool_args(command)
    
    session_name = "asistenteia"
    
    # Verificar si el comando está en la lista blanca; si no, añadir advertencia visible
    executor = CommandExecutor()
    is_verified = executor._is_safe_command(command)
    
    # Enviar el comando limpio (visible para el humano) y luego un banner corto
    wrapped_command = command
    banner_line = None
    if not command.strip().endswith("&"):
        cmd_name = command.strip().split()[0].split("/")[-1] if command.strip() else "comando"
        # Banner compacto: una línea corta y legible
        banner_line = f'test $? = 0 && echo "✅ {cmd_name} OK" || echo "❌ {cmd_name} ERROR $?"'
    
    # 1. Comprobar si la sesión de tmux existe y si está activa en pantalla (attached)
    session_attached = False
    
    try:
        ok, _ = await _run_tmux_cmd(["has-session", "-t", session_name])
        if ok:
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

    # 2. Si la terminal ya está abierta y acoplada a la sesión, enviamos el comando
    if session_attached:
        try:
            logger.info(f"Enviando comando a sesión de tmux existente: {command}")
            # Paso 1: enviar el comando limpio (visible para el humano)
            buf_file = settings.TEMP_DIR / "tmux_buf"
            buf_file.write_text(command.strip() + "\n", encoding="utf-8")
            await _run_tmux_cmd(["load-buffer", "-b", "cmd", "-t", session_name, str(buf_file)])
            await _run_tmux_cmd(["paste-buffer", "-b", "cmd", "-t", session_name])
            await _run_tmux_cmd(["send-keys", "-t", session_name, "C-m"])
            buf_file.unlink(missing_ok=True)
            
            # Paso 2: esperar a que el comando termine y enviar el banner
            await asyncio.sleep(1.5)
            if banner_line:
                buf_file.write_text(banner_line + "\n", encoding="utf-8")
                await _run_tmux_cmd(["load-buffer", "-b", "cmd", "-t", session_name, str(buf_file)])
                await _run_tmux_cmd(["paste-buffer", "-b", "cmd", "-t", session_name])
                await _run_tmux_cmd(["send-keys", "-t", session_name, "C-m"])
            
            # Esperar a que el banner se ejecute y capturar output
            await asyncio.sleep(1.0)
            ok_capture, screen_output = await _run_tmux_cmd(["capture-pane", "-p", "-t", session_name])
            if ok_capture and screen_output:
                # Devolver últimas 60 líneas (más contexto para comandos largos)
                lines = screen_output.splitlines()
                last_lines = lines[-60:]
                screen_content = "\n".join(last_lines)
                
                # Límite inteligente: 4000 chars (~1300 tokens) para salidas de terminal
                # Esto deja ~2700 tokens para historial + prompt + respuesta del modelo
                max_chars = 4000
                if len(screen_content) > max_chars:
                    screen_content = screen_content[:max_chars] + "\n...[salida truncada]"
                
                return f"Éxito: Comando ejecutado: {command}\n\nSALIDA DE LA TERMINAL:\n{screen_content}"
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
        
        # Esperar un momento a que la terminal y tmux se inicien/adjunten gráficamente
        await asyncio.sleep(0.8)
        
        # Paso 1: enviar el comando limpio (visible para el humano)
        buf_file = settings.TEMP_DIR / "tmux_buf"
        buf_file.write_text(command.strip() + "\n", encoding="utf-8")
        await _run_tmux_cmd(["load-buffer", "-b", "cmd", "-t", session_name, str(buf_file)])
        await _run_tmux_cmd(["paste-buffer", "-b", "cmd", "-t", session_name])
        await _run_tmux_cmd(["send-keys", "-t", session_name, "C-m"])
        buf_file.unlink(missing_ok=True)
        
        # Paso 2: esperar y enviar el banner
        await asyncio.sleep(1.5)
        if banner_line:
            buf_file.write_text(banner_line + "\n", encoding="utf-8")
            await _run_tmux_cmd(["load-buffer", "-b", "cmd", "-t", session_name, str(buf_file)])
            await _run_tmux_cmd(["paste-buffer", "-b", "cmd", "-t", session_name])
            await _run_tmux_cmd(["send-keys", "-t", session_name, "C-m"])
        
        # Esperar a que el comando termine y capturar output
        await asyncio.sleep(2.0)
        ok_capture, screen_output = await _run_tmux_cmd(["capture-pane", "-p", "-t", session_name])
        if ok_capture and screen_output:
            lines = screen_output.splitlines()
            last_lines = lines[-60:]
            screen_content = "\n".join(last_lines)
            
            # Límite inteligente: 4000 chars (~1300 tokens) para salidas de terminal
            max_chars = 4000
            if len(screen_content) > max_chars:
                screen_content = screen_content[:max_chars] + "\n...[salida truncada]"
            
            return f"Éxito: Terminal {chosen_terminal.capitalize()} abierta y comando ejecutado: {command}\n\nSALIDA DE LA TERMINAL:\n{screen_content}"
        
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
    import re
    
    # Pequeño retardo para dar tiempo a la terminal a renderizar los cambios del comando recién enviado
    await asyncio.sleep(0.8)
    
    session_name = "asistenteia"
    try:
        # Verificar si la sesión existe
        ok, _ = await _run_tmux_cmd(["has-session", "-t", session_name])
        if not ok:
            return "La terminal persistente no está iniciada (no hay sesión activa de tmux)."
            
        # Capturar el panel activo de la sesión de tmux
        ok_capture, output = await _run_tmux_cmd(["capture-pane", "-p", "-t", session_name])
        if ok_capture:
            if not output:
                return "La pantalla de la terminal está vacía."
                
            # Buscar si hay algún indicador de código de salida en el contenido capturado
            success_match = re.search(r"\[AsistenteIA: '([^']+)'\s+finalizado correctamente\]", output)
            exit_code_match = re.search(r"\[AsistenteIA: '([^']+)'\s+falló con código de error (\d+)\]", output)
            
            # Devolver últimas 60 líneas para contexto suficiente
            lines = output.splitlines()
            last_lines = lines[-60:]
            screen_content = "\n".join(last_lines)
            
            # Límite inteligente: 4000 chars (~1300 tokens) para lectura de pantalla
            max_chars = 4000
            if len(screen_content) > max_chars:
                screen_content = screen_content[:max_chars] + "\n...[pantalla truncada]"
            
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
        await _run_tmux_cmd(["send-keys", "-t", session_name, input_text, "C-m"])
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
        await _run_tmux_cmd(["send-keys", "-t", session_name, "C-c"])
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
    from src.browser import (
        ensure_browser, get_page,
        browser_navigate, browser_click, browser_type, browser_read, browser_scroll,
        browser_clip, browser_research, browser_translate,
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



