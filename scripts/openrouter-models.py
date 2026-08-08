#!/usr/bin/env python3
"""Catálogo de modelos GRATIS de OpenRouter que sirven para Luka.

Lo consume la CLI (`asistenteia engine openrouter model`). El catálogo se saca en vivo
de la API pública de OpenRouter —no requiere key— porque la lista de modelos gratis
cambia cada pocas semanas: hardcodearla sería garantizar que envejece mal.

Se filtran tres cosas, y las tres son requisitos, no gustos:
  - precio 0 en prompt Y en completion (de momento solo gratis),
  - tool calling: sin él Luka no puede abrir la terminal, ni poner música, ni nada,
  - se marca cuáles aceptan imagen, que es lo que da visión de cámara y de pantalla.

Uso:
  openrouter-models.py --list           -> id|nombre|contexto|vision(yes/no) por línea
  openrouter-models.py --check <id>     -> imprime 'yes'/'no' (visión); sale 1 si no vale
"""
from __future__ import annotations

import sys

import httpx

API_URL = "https://openrouter.ai/api/v1/models"

# Respaldo por si la API no responde: lo justo para no dejar al usuario bloqueado sin
# poder elegir modelo. Mismo formato que la salida normal.
FALLBACK = [
    ("google/gemma-4-31b-it:free", "Google: Gemma 4 31B (free)", 262144, True),
    ("google/gemma-4-26b-a4b-it:free", "Google: Gemma 4 26B A4B (free)", 262144, True),
    ("poolside/laguna-s-2.1:free", "Poolside: Laguna S 2.1 (free)", 262144, False),
]


def _is_free(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    try:
        return float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
    except (TypeError, ValueError):
        return False


def fetch() -> list[tuple[str, str, int, bool]]:
    """Devuelve [(id, nombre, contexto, visión)] de los free con tools. Vacío si falla."""
    try:
        r = httpx.get(API_URL, timeout=10)
        r.raise_for_status()
        data = r.json().get("data") or []
    except Exception as e:  # noqa: BLE001 — sin red no es un error fatal: hay respaldo
        print(f"aviso: no se pudo consultar OpenRouter ({e}); usando catálogo local.",
              file=sys.stderr)
        return []

    out = []
    for m in data:
        if not _is_free(m):
            continue
        if "tools" not in (m.get("supported_parameters") or []):
            continue
        arch = m.get("architecture") or {}
        vision = "image" in (arch.get("input_modalities") or [])
        out.append((m["id"], m.get("name") or m["id"], m.get("context_length") or 0, vision))
    # Los multimodales primero y, dentro, por contexto: arriba lo más capaz.
    out.sort(key=lambda t: (not t[3], -t[2], t[0]))
    return out


def main() -> int:
    args = sys.argv[1:]
    catalogo = fetch() or FALLBACK

    if args and args[0] == "--check":
        if len(args) < 2:
            print("uso: openrouter-models.py --check <id>", file=sys.stderr)
            return 2
        for mid, _name, _ctx, vision in catalogo:
            if mid == args[1]:
                print("yes" if vision else "no")
                return 0
        return 1

    for mid, name, ctx, vision in catalogo:
        print(f"{mid}|{name}|{ctx}|{'yes' if vision else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
