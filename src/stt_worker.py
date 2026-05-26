"""Worker de STT en subproceso aislado.

faster-whisper (CTranslate2) sufre una fuerte contención de hilos cuando convive
en el mismo proceso que el motor LiteRT (la transcripción pasa de ~4s a ~12-15s).
Ejecutarlo en un subproceso dedicado lo aísla y restaura el rendimiento.

Protocolo (líneas JSON sobre stdin/stdout):
  - Al arrancar emite por stdout: {"ready": true}
  - Petición (stdin):  {"path": "...", "language": "es", "beam_size": 5,
                        "initial_prompt": "...", "vad_filter": true}
  - Respuesta (stdout): {"text": "..."}  o  {"error": "..."}

Todo el logging va a stderr para mantener stdout limpio para el protocolo.

Uso: python -m src.stt_worker --model large-v3-turbo --device cpu \
         --compute-type int16 --threads 8
"""

import argparse
import json
import logging
import sys

from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] stt_worker: %(message)s")
logger = logging.getLogger(__name__)


def _emit(obj: dict) -> None:
    """Escribe una línea JSON en stdout y la vuelca de inmediato."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int16")
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()

    logger.info(f"Cargando modelo '{args.model}' ({args.device}/{args.compute_type}, {args.threads} hilos)...")
    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        cpu_threads=args.threads,
    )
    logger.info("Modelo cargado. Worker listo.")
    _emit({"ready": True})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            segments, _info = model.transcribe(
                req["path"],
                language=req.get("language"),
                beam_size=req.get("beam_size", 5),
                initial_prompt=req.get("initial_prompt"),
                vad_filter=req.get("vad_filter", True),
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            _emit({"text": text})
        except Exception as e:
            logger.error(f"Error transcribiendo: {e}", exc_info=True)
            _emit({"error": str(e)})


if __name__ == "__main__":
    main()
