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
#
# Estas cuatro no HACEN el trabajo: dejan algo en cola (stage_vision_capture /
# stage_document) y devuelven una frase de relleno — "Captura realizada. Analizando el
# contenido visual." — porque quien remata es AssistantService, con una segunda pasada al
# modelo adjuntando la imagen. Eso lo hace así porque desde dentro de una tool no se puede
# reentrar en el motor.
#
# Con Hermes llevando el bucle esa segunda pasada NO ocurre. Si se expusieran, Hermes
# recibiría la frase de relleno, no vería ninguna imagen, y describiría la pantalla de
# memoria: exactamente la invención que el guardarraíl anti-invención existe para
# impedir. Mejor que no tenga la herramienta a que la tenga y mienta.
#
# Para habilitarlas hay que resolver antes la visión con este motor: el sidecar del perfil
# hermes corre con vision:false, y meter la torre de visión junto a 64k de contexto no
# entra en 8 GiB. Ver el plan (Fase 2).
_EXCLUDED: set[str] = {
    "analyze_screen",
    "analyze_clipboard_image",
    "analyze_camera",
    "create_document",
}


def build_mcp_server(tools: Iterable[Callable], name: str = "luka") -> MCPServer:
    """Construye el servidor MCP con las herramientas de Luka registradas."""
    server = MCPServer(
        name=name,
        instructions=(
            "Herramientas del asistente Luka sobre este escritorio Linux "
            "(CachyOS/Hyprland): pantalla, música, aplicaciones, portapapeles, "
            "correo y la cámara del satélite. Úsalas en lugar de las genéricas "
            "cuando la petición sea sobre ESTE equipo."
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
