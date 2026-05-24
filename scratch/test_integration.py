import asyncio
import logging
from src.command_executor import launch_application

# Configurar logging para ver la salida detallada
logging.basicConfig(level=logging.INFO)

async def test():
    print("--- TEST 1: Buscar y lanzar una app inexistente ---")
    res1 = await launch_application("AppAbsurdaQueNoExiste")
    print(f"Resultado: {res1}\n")

    print("--- TEST 2: Buscar y lanzar Steam ---")
    res2 = await launch_application("steam")
    print(f"Resultado: {res2}\n")
    
    print("--- TEST 3: Buscar y lanzar Heroic ---")
    res3 = await launch_application("heroic")
    print(f"Resultado: {res3}\n")

if __name__ == "__main__":
    asyncio.run(test())
