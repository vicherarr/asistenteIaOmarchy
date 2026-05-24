import asyncio
import sys
import logging
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from src.litert_client import LiteRTClient
from src.command_executor import close_application

logging.basicConfig(level=logging.INFO)

async def main():
    import os
    os.environ["LITERT_BACKEND"] = "cpu"
    client = LiteRTClient()
    
    # Load system prompt
    prompt_path = Path(__file__).parent.parent / "config" / "system_prompt.txt"
    system_prompt = prompt_path.read_text(encoding="utf-8")
    
    # Tools to pass
    tools = [close_application]
    
    print("--- ENVIANDO MENSAJE AL LLM CON SOLICITUD DE CIERRE ---")
    try:
        # Usamos chat de forma síncrona esperando la corrutina
        res = await client.chat(
            prompt="cierra android studio",
            tools=tools,
            system_prompt=system_prompt,
            history=None,
            image_path=None
        )
        print(f"Respuesta del LLM: {res}")
    except Exception as e:
        print(f"EXCEPCIÓN DETECTADA: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
