import asyncio
import sys
import logging
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from src.config import settings
settings.LITERT_BACKEND = "cpu"

from src.litert_client import LiteRTClient
from src.command_executor import close_application, launch_application

logging.basicConfig(level=logging.INFO)

async def main():
    client = LiteRTClient()
    
    # Load system prompt
    prompt_path = Path(__file__).parent.parent / "config" / "system_prompt.txt"
    system_prompt = prompt_path.read_text(encoding="utf-8")
    
    # Tools to pass
    tools = [launch_application, close_application]
    
    print("--- ENVIANDO MENSAJE AL LLM CON SOLICITUD DE CIERRE EN CPU ---")
    
    # Simular historial
    from src.schema import ChatMessage
    history = [
        ChatMessage(role="user", content="abre Android Studio"),
        ChatMessage(role="assistant", content="He lanzado exitosamente 'Android Studio' de forma gráfica.")
    ]
    
    try:
        async for chunk in client.chat_stream(
            prompt="cierra android studio",
            tools=tools,
            system_prompt=system_prompt,
            history=history
        ):
            print(chunk, end="", flush=True)
        print("\n--- FIN ---")
    except Exception as e:
        print(f"\nEXCEPCIÓN EN TEST: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
