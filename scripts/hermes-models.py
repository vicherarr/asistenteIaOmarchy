#!/usr/bin/env python3
"""Catálogo de modelos para Hermes Agent (OpenRouter en la nube y Local).

Hermes requiere modelos con soporte de tool calling (function calling) y una ventana
de contexto amplia (>= 64k tokens) debido a su system prompt extenso y bucles agénticos.

Uso:
  hermes-models.py --list             -> Lista los modelos curados con formato machine-readable
  hermes-models.py --check <id|alias> -> Valida un modelo o alias y devuelve su id oficial
"""
from __future__ import annotations

import sys
from typing import Optional

import httpx

# Catálogo curado para Hermes Agent.
# Seleccionados por:
#  1. Soporte excelente de tool-calling / function calling para agentes.
#  2. Contexto >= 64k tokens (imprescindible para el bucle agéntico de Hermes).
#  3. Relación calidad / precio sobresaliente.
CURATED_MODELS = [
    {
        "alias": "llama3.3",
        "id": "meta-llama/llama-3.3-70b-instruct",
        "name": "Meta Llama 3.3 70B Instruct",
        "ctx": 131072,
        "price_in": 0.10,
        "price_out": 0.32,
        "recommended": True,
        "tag": "⭐ RECOMENDADO (Mejor calidad/precio agéntico)",
        "desc": "70B muy robusto en tool-calling y seguimiento de instrucciones complejas.",
    },
    {
        "alias": "deepseek",
        "id": "deepseek/deepseek-chat",
        "name": "DeepSeek V3 (Chat)",
        "ctx": 163840,
        "price_in": 0.26,
        "price_out": 1.03,
        "recommended": False,
        "tag": "Máxima inteligencia a muy bajo coste",
        "desc": "Líder en benchmarks agénticos y razonamiento multi-paso.",
    },
    {
        "alias": "gemini-flash",
        "id": "google/gemini-2.5-flash",
        "name": "Google Gemini 2.5 Flash",
        "ctx": 1048576,
        "price_in": 0.30,
        "price_out": 1.20,
        "recommended": False,
        "tag": "Contexto masivo (1M tokens) y ultra rápido",
        "desc": "Latencia mínima y 1 millón de tokens de ventana de contexto.",
    },
    {
        "alias": "qwen-plus",
        "id": "qwen/qwen-plus",
        "name": "Qwen Plus (Alibaba)",
        "ctx": 1000000,
        "price_in": 0.40,
        "price_out": 1.20,
        "recommended": False,
        "tag": "1M contexto, razonamiento avanzado",
        "desc": "Gran capacidad de análisis y llamadas a funciones estructuradas.",
    },
    {
        "alias": "gemma-free",
        "id": "google/gemma-4-31b-it:free",
        "name": "Google Gemma 4 31B (Gratis)",
        "ctx": 262144,
        "price_in": 0.00,
        "price_out": 0.00,
        "recommended": False,
        "tag": "100% Gratuito en OpenRouter",
        "desc": "31B denso en la nube sin coste de API.",
    },
    {
        "alias": "local",
        "id": "local",
        "name": "Local GPU (TabbyAPI - Qwen3.5-9B)",
        "ctx": 65536,
        "price_in": 0.00,
        "price_out": 0.00,
        "recommended": False,
        "tag": "Inferencia 100% local en tu GPU",
        "desc": "Corre en el sidecar TabbyAPI local (sin enviar datos a la nube).",
    },
]

ALIAS_MAP = {
    "1": "meta-llama/llama-3.3-70b-instruct",
    "llama": "meta-llama/llama-3.3-70b-instruct",
    "llama3": "meta-llama/llama-3.3-70b-instruct",
    "llama3.3": "meta-llama/llama-3.3-70b-instruct",
    "llama70b": "meta-llama/llama-3.3-70b-instruct",
    "2": "deepseek/deepseek-chat",
    "deepseek": "deepseek/deepseek-chat",
    "deepseek-v3": "deepseek/deepseek-chat",
    "v3": "deepseek/deepseek-chat",
    "3": "google/gemini-2.5-flash",
    "gemini": "google/gemini-2.5-flash",
    "gemini-flash": "google/gemini-2.5-flash",
    "4": "qwen/qwen-plus",
    "qwen": "qwen/qwen-plus",
    "qwen-plus": "qwen/qwen-plus",
    "5": "google/gemma-4-31b-it:free",
    "free": "google/gemma-4-31b-it:free",
    "gemma": "google/gemma-4-31b-it:free",
    "gemma-free": "google/gemma-4-31b-it:free",
    "6": "local",
    "local": "local",
    "gpu": "local",
    "tabby": "local",
}


def check_openrouter_model(model_id: str) -> Optional[dict]:
    """Verifica si el modelo existe en OpenRouter y devuelve su info básica."""
    try:
        r = httpx.get("https://openrouter.ai/api/v1/models", timeout=8)
        if r.status_code == 200:
            for m in r.json().get("data", []):
                if m["id"].lower() == model_id.lower():
                    return m
    except Exception:
        pass
    return None


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("--list", "-l"):
        for m in CURATED_MODELS:
            rec = "yes" if m["recommended"] else "no"
            print(f"{m['id']}|{m['alias']}|{m['name']}|{m['ctx']}|${m['price_in']:.2f}|${m['price_out']:.2f}|{rec}|{m['tag']}|{m['desc']}")
        return 0

    if args[0] == "--check":
        if len(args) < 2:
            print("Uso: hermes-models.py --check <id|alias>", file=sys.stderr)
            return 2
        query = args[1].strip()
        low = query.lower()

        # 1. Alias conocido
        if low in ALIAS_MAP:
            target = ALIAS_MAP[low]
            print(target)
            return 0

        # 2. Match exacto con ID curado
        for m in CURATED_MODELS:
            if m["id"].lower() == low:
                print(m["id"])
                return 0

        # 3. Si es "local"
        if low == "local":
            print("local")
            return 0

        # 4. Comprobar en OpenRouter API en vivo (si contiene '/')
        if "/" in query:
            info = check_openrouter_model(query)
            if info:
                ctx = info.get("context_length") or 0
                has_tools = "tools" in (info.get("supported_parameters") or [])
                if ctx < 32768:
                    print(f"aviso: '{info['id']}' tiene solo {ctx} tokens de contexto. Hermes funciona mejor con >=64k.", file=sys.stderr)
                if not has_tools:
                    print(f"aviso: '{info['id']}' no declara soporte explícito de tools en OpenRouter.", file=sys.stderr)
                print(info["id"])
                return 0
            # Si no se pudo contactar a OpenRouter o no está en la lista pero tiene formato org/model, lo aceptamos
            print(query)
            return 0

        print(f"Modelo o alias no reconocido: '{query}'", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
