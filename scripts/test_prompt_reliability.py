"""Script de prueba para validar la fiabilidad del prompt y del modelo."""

import asyncio
import json
import logging
from src.ollama_client import OllamaClient, OllamaMessage
from src.context_injector import build_full_system_prompt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PromptTest")

async def test_prompt():
    client = OllamaClient()
    
    # 1. Construir el prompt completo
    system_prompt = build_full_system_prompt()
    
    queries = [
        "Hola",
        "Abre la wikipedia en español",
        "Dime cuanta memoria RAM tengo libre"
    ]
    
    print("\n--- INICIANDO LABORATORIO DE PROMPT ---\n")
    
    for query in queries:
        print(f"TESTING QUERY: '{query}'")
        messages = [
            OllamaMessage(role="system", content=system_prompt),
            OllamaMessage(role="user", content=f"{query}\n\n(Responde SOLO en JSON)")
        ]
        
        try:
            raw_response = await client.generate(messages)
            print(f"RAW RESPONSE: {raw_response}")
            
            # Validar si es JSON
            try:
                # Limpiar posibles bloques markdown si los hay
                clean_json = raw_response.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(clean_json)
                print(f"RESULTADO: ✅ JSON VÁLIDO")
                print(f"TEXTO: {parsed.get('response_text')}")
                print(f"COMANDOS: {parsed.get('commands')}")
            except Exception as e:
                print(f"RESULTADO: ❌ FALLO DE FORMATO ({e})")
                
        except Exception as e:
            print(f"ERROR DE CONEXIÓN: {e}")
            
        print("-" * 50)

    await client.close()

if __name__ == "__main__":
    asyncio.run(test_prompt())
