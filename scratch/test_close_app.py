import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from src.command_executor import close_application

logging.basicConfig(level=logging.INFO)

async def main():
    print("=== PROBANDO HERRAMIENTA CLOSE_APPLICATION ===")
    
    # 1. Probar con un término absurdo
    print("\nProbando con una app que no existe:")
    res = await close_application("AplicacionInexistenteQueNoEstaCorriendo")
    print(f"Resultado: {res}")
    
    # 2. Probar con un término vacío
    print("\nProbando con término vacío:")
    res = await close_application("")
    print(f"Resultado: {res}")
    
    # 3. Buscar y tratar de cerrar un proceso ficticio o el mismo test
    # (aunque tenemos salvaguarda para no matar el asistente, podemos ver si encuentra cosas)
    print("\nProbando búsqueda/cierre:")
    res = await close_application("alacritty")
    print(f"Resultado: {res}")

if __name__ == "__main__":
    asyncio.run(main())
