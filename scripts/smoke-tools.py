#!/usr/bin/env python3
"""Comprueba contra el modelo REAL que el tool calling sigue entero.

Por qué existe: enganchar un `tool_event_handler` en todos los turnos rompió el tool
calling de una forma que ningún test unitario podía ver — las respuestas de las tools
llegaban destrozadas al modelo, que soltaba una palabra ("Albert" a un "¿quién fue
Albert Einstein?") y paraba; el asistente acababa diciendo "Acción ejecutada
correctamente" a todo. Los mocks no detectan eso: hace falta el motor de verdad.

Qué verifica, con las tools reales del asistente:
  - que la respuesta es una frase completa y no un muñón de una palabra
  - que las tools que se ejecutan se registran con su NOMBRE (no con '?')
  - que una pregunta informativa no acaba en el fallback genérico

Uso (el servicio tiene el modelo en la GPU, hay que pararlo):
    systemctl --user stop asistenteia.service; \
      venv/bin/python scripts/smoke-tools.py; \
      systemctl --user start asistenteia.service
"""

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402
from src.litert_client import LiteRTClient  # noqa: E402

# Preguntas que en producción tiran de tool y que reventaron con el bug. Se escriben
# como las dice el usuario de verdad, con el "Luka," delante: el prompt influye en si el
# modelo encadena otra llamada o contesta.
CASOS = [
    ("Luka, ¿quién es Albert Einstein?", "informativa, encadena web_search + read_web_page"),
    ("Luka, ¿qué hora es?", "usa la terminal"),
    ("Luka, ¿cuánta memoria RAM tengo?", "usa la terminal"),
]

# El fallo que se escapó era INTERMITENTE: pasó el smoke test a la primera y falló las
# dos veces siguientes con voz real. Una sola pasada no prueba nada.
REPETICIONES = 3

# Respuesta demasiado corta = el muñón que delataba el fallo.
MIN_CHARS = 25
FALLBACKS = ("Acción ejecutada correctamente", "Comando ejecutado en la terminal")


def servicio_activo() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", "asistenteia.service"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() == "active"
    except Exception:
        return False


async def main() -> int:
    if servicio_activo():
        print("El servicio asistenteia está ACTIVO y ocupa la VRAM con el modelo.")
        print("Párualo, ejecuta esto y arráncalo otra vez:\n")
        print("  systemctl --user stop asistenteia.service; \\")
        print("    venv/bin/python scripts/smoke-tools.py; \\")
        print("    systemctl --user start asistenteia.service")
        return 1

    # Las tools reales, tal cual las registra el asistente.
    import src.camera_tool as ct
    import src.command_executor as ce
    import src.document_tool as dt
    import src.vision_tool as vt

    tools = [
        ce.execute_system_command, ce.read_log_file, ce.clipboard_manager, ce.web_search,
        ce.system_diagnostics, ce.read_web_page, ce.play_specific_music,
        ce.play_youtube_music, ce.music_control, ce.open_terminal_and_run_command,
        ce.read_terminal_screen, ce.control_local_browser, ce.send_input_to_terminal,
        ce.interrupt_terminal_command, ce.launch_application, ce.close_application,
        vt.analyze_screen, vt.analyze_clipboard_image, vt.take_screenshot,
        dt.create_document, ct.analyze_camera, ct.show_camera_photo,
    ]

    # Se reutiliza el parser del motor ExLlama: rescata llamadas 'peladas' con AST y
    # paréntesis equilibrados, y solo acepta nombres de tools reales para no confundir
    # prosa con una llamada.
    from src.engines.exllama_engine import _extract_bare_tool_calls

    nombres_de_tools = {t.__name__ for t in tools}

    # El system prompt, montado igual que en producción: assistant_service le añade la
    # fecha y hora actuales. Si el smoke test usa un prompt distinto al real, mide otra
    # cosa — y el modelo es sensible a estas instrucciones.
    from src.assistant_service import AssistantService

    prompt_path = settings.PROJECT_ROOT / "config" / "system_prompt.txt"
    system_prompt = prompt_path.read_text(encoding="utf-8")
    system_prompt += AssistantService._now_context()

    print("Cargando el modelo...")
    client = LiteRTClient()
    if not client.engine:
        print("El motor no cargó.")
        return 1

    fallos = 0
    total = 0
    for pregunta, pista in CASOS:
        print(f"\n─── {pregunta}   ({pista})")
        for intento in range(1, REPETICIONES + 1):
            total += 1
            texto = ""
            async for chunk in client.chat_stream(
                prompt=pregunta, tools=tools, system_prompt=system_prompt, history=[]
            ):
                texto += chunk
            texto = texto.strip()
            usadas = list(client.last_turn_tools_used)

            problemas = []
            if len(texto) < MIN_CHARS:
                problemas.append(f"respuesta cortada ({len(texto)} chars)")
            if any(f in texto for f in FALLBACKS):
                problemas.append("cayó en el fallback genérico")
            if "?" in usadas:
                problemas.append("hay tools registradas como '?'")
            if client.last_turn_tools_disabled:
                problemas.append("el motor se quedó SIN herramientas (parse error)")
            # Marcador de tool call que se fuga como texto en vez de ejecutarse: el
            # modelo intentaba encadenar otra llamada y el stream moría ahí.
            if "<|tool_call" in texto or "call:" in texto:
                problemas.append("marcador de tool call fugado como texto")
            # El modelo a veces ESCRIBE la llamada en Python en vez de ejecutarla
            # (execute_system_command("date")). Sin esto pasaba por buena solo por
            # tener longitud suficiente.
            fugadas = _extract_bare_tool_calls(texto, nombres_de_tools)
            if fugadas:
                problemas.append(
                    f"tool call fugada como texto: {', '.join(c['name'] for c in fugadas)}"
                )

            marca = "❌" if problemas else "✅"
            print(f"  {marca} intento {intento}/{REPETICIONES} · tools={usadas or 'ninguna'} "
                  f"· {len(texto)} chars")
            print(f"       {texto[:160]!r}")
            if problemas:
                fallos += 1
                print(f"       → {'; '.join(problemas)}")

    print("\n" + "=" * 50)
    if fallos:
        print(f"TOOL CALLING ROTO: {fallos}/{total} intentos con problemas.")
        return 1
    print(f"TOOL CALLING OK: {len(CASOS)}/{len(CASOS)} casos bien.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
