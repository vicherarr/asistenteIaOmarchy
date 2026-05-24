import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from src.litert_client import LiteRTClient
from src.command_executor import launch_application, close_application

logging.basicConfig(level=logging.INFO)

async def main():
    print("=== PROBANDO FLUJO COMPLETO DE HERRAMIENTAS EN EL LLM ===")
    
    # Cargar cliente
    client = LiteRTClient()
    
    # Cargar system prompt
    prompt_path = Path(__file__).parent.parent / "config" / "system_prompt.txt"
    system_prompt = prompt_path.read_text(encoding="utf-8")
    
    # Herramientas
    tools = [launch_application, close_application]
    
    print("\n--- Test 1: Pedir abrir Android Studio ---")
    async for chunk in client.chat_stream(
        prompt="Abre Android Studio",
        tools=tools,
        system_prompt=system_prompt
    ):
        print(chunk, end="", flush=True)
    print("\n")
    
    print("\n--- Test 2: Pedir cerrar Android Studio ---")
    # Para simular historial:
    from src.schema import ChatMessage
    history = [
        ChatMessage(role="user", content="Abre Android Studio"),
        ChatMessage(role="assistant", content="He lanzado exitosamente 'Android Studio' de forma gráfica.")
    ]
    
    async for chunk in client.chat_stream(
        prompt="cierra android studio",
        tools=tools,
        system_prompt=system_prompt,
        history=history
    ):
        print(chunk, end="", flush=True)
    print("\n")

if __name__ == "__main__":
    asyncio.run(main())
