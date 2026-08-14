"""Tests del servidor MCP que expone las tools de Luka a Hermes."""

import asyncio

import pytest

from src.mcp_server import MCP_PATH, build_mcp_server, mount_mcp


async def tool_simple(nombre: str, veces: int = 1) -> str:
    """Saluda a alguien varias veces.

    Args:
        nombre: A quién saludar.
        veces: Cuántas veces.
    """
    return " ".join([f"hola {nombre}"] * veces)


def tool_sincrona(texto: str) -> str:
    """Devuelve el texto en mayúsculas."""
    return texto.upper()


def test_registra_las_tools_con_su_esquema():
    """El esquema sale de los type hints + docstring, sin reescribir la tool."""
    srv = build_mcp_server([tool_simple, tool_sincrona])
    tools = asyncio.run(srv.list_tools())
    por_nombre = {t.name: t for t in tools}

    assert set(por_nombre) == {"tool_simple", "tool_sincrona"}
    esquema = por_nombre["tool_simple"].input_schema
    assert esquema["required"] == ["nombre"]          # 'veces' tiene defecto
    assert set(esquema["properties"]) == {"nombre", "veces"}
    assert "Saluda" in (por_nombre["tool_simple"].description or "")


def test_ejecuta_tools_async_y_sincronas():
    srv = build_mcp_server([tool_simple, tool_sincrona])

    async def run():
        a = await srv.call_tool("tool_simple", {"nombre": "Luka", "veces": 2})
        b = await srv.call_tool("tool_sincrona", {"texto": "eco"})
        return a, b

    a, b = asyncio.run(run())
    assert "hola Luka hola Luka" in str(a)
    assert "ECO" in str(b)


def test_una_tool_invalida_no_tumba_al_resto():
    """Una mal anotada se salta con un warning; las demás siguen expuestas."""
    def sin_anotaciones(a, b):   # noqa: ANN001 — a propósito
        """Tool problemática."""
        return a

    srv = build_mcp_server([tool_sincrona, sin_anotaciones])
    nombres = {t.name for t in asyncio.run(srv.list_tools())}
    assert "tool_sincrona" in nombres


def test_las_tools_reales_de_luka_producen_esquemas_validos():
    """Contra las tools de verdad: es lo que consumirá Hermes."""
    from src.command_executor import execute_system_command, web_search
    from src.vision_tool import analyze_screen

    srv = build_mcp_server([execute_system_command, web_search, analyze_screen])
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}

    assert tools["execute_system_command"].input_schema["required"] == ["command"]
    assert tools["web_search"].input_schema["required"] == ["query"]
    for t in tools.values():
        assert (t.description or "").strip(), f"{t.name} sin descripción"


def test_mount_mcp_no_revienta_la_app_si_falla(monkeypatch):
    """Sin MCP el asistente tiene que arrancar igual: devuelve None, no lanza."""
    import src.mcp_server as ms

    def explota(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ms, "build_mcp_server", explota)

    class FakeApp:
        def mount(self, *a, **k): ...

    assert mount_mcp(FakeApp(), [tool_sincrona]) is None


def test_se_monta_en_la_ruta_esperada():
    """Hermes apunta a esta ruta en su config; si cambia, deja de encontrarlo."""
    montado = {}

    class FakeApp:
        def mount(self, path, sub):
            montado["path"] = path

    manager = mount_mcp(FakeApp(), [tool_sincrona])
    assert montado["path"] == MCP_PATH == "/mcp"
    assert manager is not None


def test_no_expone_las_tools_que_dependen_de_la_segunda_pasada():
    """Las de staging devuelven una frase de relleno y no hacen el trabajo.

    Con Hermes al mando no hay segunda pasada, así que exponerlas haría que
    describiera la pantalla de memoria: justo lo que el guardarraíl impide.
    """
    from src.camera_tool import analyze_camera
    from src.command_executor import web_search
    from src.document_tool import create_document
    from src.vision_tool import analyze_clipboard_image, analyze_screen

    srv = build_mcp_server([
        web_search, analyze_screen, analyze_clipboard_image, analyze_camera, create_document,
    ])
    nombres = {t.name for t in asyncio.run(srv.list_tools())}

    assert nombres == {"web_search"}
    for prohibida in ("analyze_screen", "analyze_clipboard_image",
                      "analyze_camera", "create_document"):
        assert prohibida not in nombres


def test_take_screenshot_si_se_expone():
    """No hace staging: guarda/copia la captura y termina. Es segura."""
    from src.vision_tool import take_screenshot

    srv = build_mcp_server([take_screenshot])
    assert {t.name for t in asyncio.run(srv.list_tools())} == {"take_screenshot"}
