"""Fase 0 — Benchmark del motor LiteRT (mide TTFT y prefill/decode tk/s).

Carga el modelo de verdad, así que CONSÚMELO TÚ (no durante el servicio en
marcha, para no competir por la VRAM). Compara MTP on/off y, si encuentra
varios .litertlm en models/, los compara entre sí (p.ej. E2B vs E4B).

Uso:
    venv/bin/python scripts/benchmark_litert.py            # auto: todos los models/*.litertlm en GPU
    venv/bin/python scripts/benchmark_litert.py --cpu      # en CPU
    venv/bin/python scripts/benchmark_litert.py --model models/gemma-4-E4B-it.litertlm
"""
import argparse
import sys
from pathlib import Path

import litert_lm

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = str(Path.home() / ".cache" / "asistenteia" / "litert")


def fmt(info) -> str:
    return (
        f"init={info.init_time_in_second:.2f}s  "
        f"TTFT={info.time_to_first_token_in_second:.3f}s  "
        f"prefill={info.last_prefill_tokens_per_second:.0f} tk/s  "
        f"decode={info.last_decode_tokens_per_second:.1f} tk/s"
    )


def bench(model_path: Path, backend, speculative: bool):
    b = litert_lm.Benchmark(
        str(model_path),
        backend=backend,
        prefill_tokens=256,
        decode_tokens=256,
        cache_dir=CACHE_DIR,
        enable_speculative_decoding=speculative,
    )
    return b.run()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="ruta a un .litertlm concreto")
    ap.add_argument("--cpu", action="store_true", help="usar backend CPU")
    args = ap.parse_args()

    backend = litert_lm.Backend.CPU if args.cpu else litert_lm.Backend.GPU
    backend_name = "CPU" if args.cpu else "GPU"

    if args.model:
        models = [Path(args.model)]
    else:
        models = sorted((ROOT / "models").glob("*.litertlm"))
    models = [m if m.is_absolute() else ROOT / m for m in models]
    models = [m for m in models if m.exists()]

    if not models:
        print("No se encontró ningún .litertlm en models/.", file=sys.stderr)
        sys.exit(1)

    print(f"Backend: {backend_name}   cache: {CACHE_DIR}\n")
    print(f"{'modelo':<34} {'MTP':<5} resultado")
    print("-" * 90)
    for m in models:
        for spec in (False, True):
            try:
                info = bench(m, backend, spec)
                print(f"{m.name:<34} {'on' if spec else 'off':<5} {fmt(info)}")
            except Exception as e:  # noqa: BLE001
                print(f"{m.name:<34} {'on' if spec else 'off':<5} ERROR: {e}")
    print("\nNota: la 1ª corrida puebla la caché; repite para tiempos en caliente.")


if __name__ == "__main__":
    main()
