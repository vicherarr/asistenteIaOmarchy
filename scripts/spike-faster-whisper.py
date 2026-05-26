"""Spike aislado: evaluar faster-whisper (large-v3-turbo, CPU int8) vs whisper-cli.

NO toca el servicio ni el código productivo. Mide:
  - Tiempo de carga del modelo (una vez, residente).
  - Latencia por transcripción con el modelo ya cargado (coste real por comando).
  - Exactitud sobre audios limpio y degradado.

Uso: venv/bin/python scripts/spike-faster-whisper.py [audio1.wav audio2.wav ...]
"""

import sys
import time

from faster_whisper import WhisperModel

MODEL = "large-v3-turbo"
DEVICE = "cpu"
COMPUTE = "int8"
THREADS = 8
LANGUAGE = "es"
PROMPT = (
    "Comandos de voz en español para el asistente Luka: música, volumen, "
    "captura de pantalla, documentos, búsqueda en internet y notas de Obsidian."
)

DEFAULT_FILES = ["/tmp/stt_test.wav", "/tmp/h_noisy.wav"]
EXPECTED = {
    "/tmp/stt_test.wav": "Luka sube el volumen de la música y abre mis notas de Obsidian",
    "/tmp/h_noisy.wav": "Luka haz una captura de pantalla y busca en internet recetas de paella valenciana",
}


def transcribe(model, path, vad):
    t0 = time.time()
    segments, info = model.transcribe(
        path,
        language=LANGUAGE,
        beam_size=5,
        initial_prompt=PROMPT,
        vad_filter=vad,
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, time.time() - t0


def main():
    files = sys.argv[1:] or DEFAULT_FILES

    print(f"Cargando modelo {MODEL} ({DEVICE}/{COMPUTE}, {THREADS} hilos)...")
    t0 = time.time()
    model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE, cpu_threads=THREADS)
    print(f"  Carga: {time.time() - t0:.1f}s (una sola vez, residente)\n")

    for vad in (False, True):
        print(f"########## VAD={'ON' if vad else 'OFF'} ##########")
        for f in files:
            try:
                # Una pasada de calentamiento ya implícita: el modelo está cargado.
                text, dt = transcribe(model, f, vad)
            except Exception as e:
                print(f"  [{f}] ERROR: {e}")
                continue
            exp = EXPECTED.get(f)
            print(f"  [{f}]  {dt:.2f}s")
            print(f"      -> {text}")
            if exp:
                print(f"      esperado: {exp}")
            print()


if __name__ == "__main__":
    main()
