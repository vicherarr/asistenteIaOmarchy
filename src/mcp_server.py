"""Las herramientas de Luka, expuestas por MCP para que las use Hermes.

Con el motor `hermes` el bucle agéntico lo lleva Hermes, que trae sus propias
herramientas (terminal, ficheros, web, navegador...). Pero las de Luka no las tiene, y
sin ellas se queda sin lo que hace de Luka un asistente de ESTE escritorio: ver la
pantalla, la música, Gmail, la cámara del satélite. Medido antes de escribir esto:
preguntarle "¿qué hay en mi pantalla?" daba 40 segundos de nada y una respuesta vacía.

**Por qué HTTP y no un subproceso stdio.** Las tools de Luka no son funciones puras: usan
`audio_manager`, `device_gateway`, el estado del navegador y la sesión de tmux, todo ello
vivo en el proceso del asistente. Si Hermes lanzara el servidor MCP como subproceso sería
un proceso nuevo y vacío, y no serviría de nada. Por eso el servidor se monta DENTRO del
FastAPI que ya corre (`src/main.py`) y Hermes se conecta por URL. Las tools se ejecutan
donde tienen su estado.

No hay que reescribir ninguna herramienta: ya se autodescriben con type hints y docstring,
que es exactamente lo que MCP necesita para su esquema. `add_tool` las introspecciona.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)

# Ruta donde queda montado dentro de la app (Hermes apunta a http://host:puerto + esto).
MCP_PATH = "/mcp"

# Herramientas que NO se exponen a Hermes, aunque Luka las tenga registradas.
# Tres motivos distintos, agrupados abajo. La regla que los une: Hermes solo debe recibir
# lo que SOLO Luka sabe hacer. Todo lo demás sobra, y sobrar no es gratis (ver _DUPLICADAS).

# (1) No HACEN el trabajo: dejan algo en cola (stage_vision_capture / stage_document) y
# devuelven una frase de relleno — "Captura realizada. Analizando el contenido visual." —
# porque quien remata es AssistantService, con una segunda pasada al modelo adjuntando la
# imagen. Eso lo hace así porque desde dentro de una tool no se puede reentrar en el motor.
#
# Con Hermes llevando el bucle esa segunda pasada NO ocurre. Si se expusieran, Hermes
# recibiría la frase de relleno, no vería ninguna imagen, y describiría la pantalla de
# memoria: exactamente la invención que el guardarraíl anti-invención existe para
# impedir. Mejor que no tenga la herramienta a que la tenga y mienta.
#
# Para habilitarlas hay que resolver antes la visión con este motor: el sidecar del perfil
# hermes corre con vision:false, y meter la torre de visión junto a 64k de contexto no
# entra en 8 GiB. Ver el plan (Fase 2).
_SIN_SEGUNDA_PASADA: set[str] = {
    "analyze_screen",
    "analyze_clipboard_image",
    "analyze_camera",
    "create_document",
}

# (2) Hermes YA las trae de serie, y mejores: `terminal`/`process` para comandos,
# `read_file`/`search_files` para logs y ficheros, `web_search`/`web_extract` para la web.
# Las suyas son tools NÚCLEO (_HERMES_CORE_TOOLS en hermes/toolsets.py), así que nunca se
# difieren detrás del puente tool_search; las de Luka llegaban por MCP y sí se diferían.
#
# Prestárselas no era neutro: duplicaba cada capacidad con dos nombres distintos para lo
# mismo (execute_system_command vs terminal), y el system prompt de Luka nombraba la
# variante que el modelo NO tenía delante. Medido el 23/08/2026: dos turnos seguidos
# quemaron las 8 iteraciones enteras en tool_search/tool_describe/tool_call sin ejecutar
# una sola herramienta real, y el marcado de tool call se coló crudo hasta el TTS.
#
# Lo que Luka conserva aquí es lo que Hermes no puede replicar: la terminal tmux VISIBLE
# en el escritorio (abrir/leer/responder/interrumpir), que es otra intención distinta de
# "ejecuta esto y dame la salida", y el navegador Chromium del usuario con su sesión viva.
_DUPLICADAS: set[str] = {
    "execute_system_command",   # -> terminal
    "read_log_file",            # -> read_file / terminal
    "system_diagnostics",       # -> terminal
    "web_search",               # -> web_search
    "read_web_page",            # -> web_extract
}

# (3) Privacidad: con el motor Hermes el bucle lo lleva un LLM que puede ser de NUBE
# (OpenRouter por defecto). Exponer el correo y la agenda significa que el contenido de
# los mensajes y los eventos acaba en un tercero. Luka sí las conserva con sus propios
# motores —el MCP solo lo consume Hermes—, así que no se pierde ninguna función: se pierde
# solo con este motor, que es donde el dato saldría del equipo.
#
# La exclusión es INCONDICIONAL a propósito, no condicionada a que el modelo sea de nube.
# El servidor MCP se monta una vez al arrancar (src/main.py), pero el modelo de Hermes se
# cambia en caliente desde la CLI ('asistenteia engine hermes model'). Atarlo al modelo
# activo dejaría la superficie MCP obsoleta al pasar de local a nube, y el fallo sería
# hacia el lado malo: el correo saliendo sin que nada avise. Esto falla cerrado.
_PRIVADAS: set[str] = {
    "gmail_manager",
    "calendar_manager",
}

_EXCLUDED: set[str] = _SIN_SEGUNDA_PASADA | _DUPLICADAS | _PRIVADAS


def build_mcp_server(tools: Iterable[Callable], name: str = "luka") -> MCPServer:
    """Construye el servidor MCP con las herramientas de Luka registradas."""
    server = MCPServer(
        name=name,
        instructions=(
            "Herramientas del asistente Luka sobre este escritorio Linux "
            "(CachyOS/Hyprland): música, aplicaciones gráficas, portapapeles, "
            "capturas, la terminal VISIBLE del escritorio, el navegador Chromium "
            "del usuario y la cámara del satélite. Son cosas de ESTE equipo que no "
            "puedes hacer con tus herramientas genéricas. Para ejecutar comandos sin "
            "que el usuario los vea, leer ficheros o buscar en la web, usa las tuyas "
            "(terminal, read_file, web_search): son mejores y no están aquí."
        ),
    )
    registered = []
    for fn in tools:
        fname = getattr(fn, "__name__", None)
        if not fname or fname in _EXCLUDED:
            continue
        try:
            server.add_tool(fn)
            registered.append(fname)
        except Exception as e:  # noqa: BLE001 — una tool mal anotada no debe tumbar el resto
            logger.warning(f"No se pudo exponer por MCP la tool {fname}: {e}")
    logger.info(f"Servidor MCP '{name}' con {len(registered)} herramientas: {sorted(registered)}")
    return server


def mount_mcp(app, tools: Iterable[Callable]) -> Optional[object]:
    """Monta el servidor MCP en la app FastAPI. Devuelve el session_manager (o None).

    El session_manager hay que arrancarlo en el lifespan de la app; se devuelve para que
    `main.py` lo haga sin que este módulo tenga que conocer su ciclo de vida.
    """
    try:
        server = build_mcp_server(tools)
        # stateless_http: cada petición se resuelve sola, sin sesión que mantener. Es lo
        # que queremos aquí: el cliente es un proceso local y no necesitamos reanudar
        # streams ni recordar nada entre llamadas.
        sub = server.streamable_http_app(streamable_http_path="/", stateless_http=True)
        app.mount(MCP_PATH, sub)
        return server.session_manager
    except Exception as e:  # noqa: BLE001 — sin MCP el asistente debe arrancar igual
        logger.error(f"No se pudo montar el servidor MCP: {e}", exc_info=True)
        return None
