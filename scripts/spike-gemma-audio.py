"""Spike aislado: ¿puede Gemma E4B transcribir/entender audio directamente?

Carga el motor LiteRT en CPU (para NO chocar con la VRAM que ocupa el servicio)
y le pasa un audio con varias instrucciones, para ver qué devuelve.

Uso: venv/bin/python scripts/spike-gemma-audio.py [audio.wav]
"""

import sys
import time

import litert_lm
from src.config import settings


def run(engine, audio_path, instruction):
    msg = {
        "role": "user",
        "content": [
            {"type": "audio", "path": audio_path},
            {"type": "text", "text": instruction},
        ],
    }
    t0 = time.time()
    with engine.create_conversation(messages=[], tools=None) as conv:
        resp = conv.send_message(msg)
    dt = time.time() - t0
    if isinstance(resp, dict):
        text = "".join(p.get("text", "") for p in resp.get("content", []) if p.get("type") == "text")
    elif hasattr(resp, "text"):
        text = resp.text
    else:
        text = str(resp)
    return text.replace("▁", " ").strip(), dt


def main():
    audio = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gem_audio.wav"
    model = str(settings.PROJECT_ROOT / settings.LITERT_MODEL_PATH)

    print(f"Cargando LiteRT (CPU) desde {model}...")
    t0 = time.time()
    engine = litert_lm.Engine(
        model,
        backend=litert_lm.Backend.CPU,
        vision_backend=litert_lm.Backend.CPU,
        audio_backend=litert_lm.Backend.CPU,
    )
    print(f"  Cargado en {time.time() - t0:.1f}s\n")

    pruebas = [
        "Transcribe exactamente lo que se dice en este audio. Devuelve solo el texto, sin comentarios.",
        "Responde en español a lo que te pide el usuario en este audio.",
    ]
    for instr in pruebas:
        print(f"### Instrucción: {instr}")
        try:
            text, dt = run(engine, audio, instr)
            print(f"  ({dt:.1f}s) -> {text}\n")
        except Exception as e:
            print(f"  ERROR: {e}\n")


if __name__ == "__main__":
    main()
