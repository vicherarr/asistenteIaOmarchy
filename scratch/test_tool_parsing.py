import os
import sys
import logging
import asyncio

# Configurar logging para ver errores detallados
logging.basicConfig(level=logging.INFO)

# Añadir el directorio raíz al path de importación
sys.path.insert(0, "/home/victor/develop/asistenteia")

import litert_lm
from src.config import settings
from src.command_executor import (
    execute_system_command,
    read_log_file,
    clipboard_manager,
    web_search,
    system_diagnostics,
    read_web_page,
    interact_web,
    play_specific_music,
    open_terminal_and_run_command,
    read_terminal_screen,
    control_local_browser,
    send_input_to_terminal,
    interrupt_terminal_command
)

# Envolver las herramientas asíncronas para el SDK de LiteRT
import inspect
import functools

tools = [
    execute_system_command,
    read_log_file,
    clipboard_manager,
    web_search,
    system_diagnostics,
    read_web_page,
    interact_web,
    play_specific_music,
    open_terminal_and_run_command,
    read_terminal_screen,
    control_local_browser,
    send_input_to_terminal,
    interrupt_terminal_command
]

def make_sync(async_func):
    @functools.wraps(async_func)
    def wrapper(*args, **kwargs):
        return "mock"
    wrapper.__signature__ = inspect.signature(async_func)
    return wrapper

sync_tools = [make_sync(t) if inspect.iscoroutinefunction(t) else t for t in tools]

# Cargar el motor LiteRT
model_path = "/home/victor/develop/asistenteia/models/gemma-4-E4B-it.litertlm"
if not os.path.exists(model_path):
    model_path = "/home/victor/develop/asistenteia/models/gemma-4-e4b.litertlm"
if not os.path.exists(model_path):
    model_path = "/home/victor/develop/asistenteia/models/gemma-4-e2b.litertlm"
if not os.path.exists(model_path):
    model_path = "/home/victor/develop/asistenteia/models/gemma-4-E2B-it.litertlm"

print(f"Cargando modelo desde {model_path}...")
try:
    engine = litert_lm.Engine(model_path)
    print("¡Motor cargado exitosamente!")
    
    print("\nProvando registro de herramientas de una en una...")
    for idx, tool in enumerate(sync_tools):
        t_name = tools[idx].__name__
        try:
            with engine.create_conversation(messages=[], tools=[tool]) as conversation:
                print(f"  Tool '{t_name}' => OK")
        except Exception as tool_err:
            print(f"  Tool '{t_name}' => ERROR: {tool_err}")
            
    print("\nProvando registro de todas las herramientas juntas...")
    try:
        with engine.create_conversation(messages=[], tools=sync_tools) as conversation:
            print("  ¡Todas las herramientas juntas => OK!")
    except Exception as all_err:
        print(f"  ¡Todas las herramientas juntas => ERROR: {all_err}")

except Exception as e:
    print(f"Error cargando motor: {e}")
