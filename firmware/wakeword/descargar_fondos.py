#!/usr/bin/env python3
"""Descarga el audio de fondo con el que se augmentan las muestras.

Son tres cosas distintas y cada una cumple un papel:

- **Respuestas de impulso (MIT RIR)**: reverberación de habitaciones reales. Sin
  esto el modelo solo conoce voz "pegada al micro" y falla en cuanto hablas
  desde el otro lado del salón, que es justo el caso de uso.
- **AudioSet**: ruido doméstico de todo tipo (cacharros, tráfico, puertas).
- **FMA**: música. La tele y el altavoz de fondo son la fuente número uno de
  falsos positivos.

Todo acaba en WAV PCM16 mono a 16 kHz, que es lo que espera la augmentación de
microWakeWord.

# Por qué ffmpeg y no `datasets`

El notebook de referencia usa `datasets` para decodificar. Hoy eso exige
`torchcodec`, que arrastra PyTorch entero al venv de TensorFlow — dos runtimes
de CUDA en el mismo sitio para no ganar nada. ffmpeg ya está en el sistema,
convierte más rápido y no añade dependencias.

Uso:  ./descargar_fondos.py [directorio_de_trabajo]
"""

import json
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TRABAJO = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "luka-wakeword")
HILOS = 8


def descargar(url: str, destino: Path, minimo_bytes: int = 1024) -> None:
    """Descarga si falta. `minimo_bytes` no es paranoia: cuando una ruta de
    HuggingFace cambia, el servidor responde 200 con un JSON de 15 bytes que
    dice "Entry not found", y sin este umbral se guarda como si fuera el
    dataset y el fallo aparece mucho después, al intentar descomprimirlo."""
    if destino.exists() and destino.stat().st_size >= minimo_bytes:
        return
    destino.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["curl", "-sL", "-o", str(destino), url], check=True)
    if destino.stat().st_size < minimo_bytes:
        raise RuntimeError(f"descarga sospechosamente pequeña: {url} -> {destino.stat().st_size} B")


def a_wav_16k(origen: Path, destino: Path) -> None:
    """Convierte a WAV PCM16 mono 16 kHz. Silencioso salvo que falle."""
    if destino.exists():
        return
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(origen),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destino)],
        check=False,
    )


def convertir_lote(ficheros: list[Path], destino: Path) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        list(pool.map(lambda f: a_wav_16k(f, destino / (f.stem + ".wav")), ficheros))
    print(f"  {destino.name}: {len(list(destino.glob('*.wav')))} wav")


def rirs() -> None:
    """270 impulsos, ya vienen a 16 kHz: se bajan tal cual."""
    destino = TRABAJO / "mit_rirs"
    if destino.exists() and len(list(destino.glob("*.wav"))) > 200:
        print("mit_rirs: ya está")
        return
    destino.mkdir(parents=True, exist_ok=True)
    api = ("https://huggingface.co/api/datasets/davidscripka/"
           "MIT_environmental_impulse_responses/tree/main?recursive=true")
    with urllib.request.urlopen(api) as r:
        arbol = json.load(r)
    rutas = [e["path"] for e in arbol if e.get("path", "").endswith(".wav")]
    base = ("https://huggingface.co/datasets/davidscripka/"
            "MIT_environmental_impulse_responses/resolve/main/")
    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        list(pool.map(lambda p: descargar(base + p, destino / Path(p).name), rutas))
    print(f"  mit_rirs: {len(list(destino.glob('*.wav')))} impulsos")


def audioset() -> None:
    """Ruido doméstico de AudioSet.

    El notebook de referencia baja `data/bal_train09.tar`, pero ese fichero ya
    no existe: hoy el dataset se sirve en parquet con el audio embebido. Se
    baja un solo trozo (~690 MB, unos 4.000 clips de 10 s): más que suficiente
    como fondo, y el resto solo alargaría la augmentación.
    """
    destino = TRABAJO / "audioset_16k"
    if destino.exists() and any(destino.glob("*.wav")):
        print("audioset_16k: ya está")
        return

    import pyarrow.parquet as pq

    crudo = TRABAJO / "audioset"
    parquet = crudo / "00.parquet"
    descargar(
        "https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/00.parquet",
        parquet,
        minimo_bytes=10_000_000,
    )

    extraidos = crudo / "flac"
    extraidos.mkdir(parents=True, exist_ok=True)
    if not any(extraidos.iterdir()):
        tabla = pq.read_table(parquet, columns=["audio"])
        for i, fila in enumerate(tabla.column("audio")):
            registro = fila.as_py()
            (extraidos / f"{i:05d}.flac").write_bytes(registro["bytes"])
        print(f"  audioset: {i + 1} clips extraídos del parquet")

    convertir_lote(sorted(extraidos.glob("*.flac")), destino)


def fma() -> None:
    destino = TRABAJO / "fma_16k"
    if destino.exists() and any(destino.glob("*.wav")):
        print("fma_16k: ya está")
        return
    crudo = TRABAJO / "fma"
    zip_path = crudo / "fma_xs.zip"
    descargar("https://huggingface.co/datasets/mchl914/fma_xsmall/resolve/main/fma_xs.zip", zip_path)
    subprocess.run(["unzip", "-q", "-o", str(zip_path)], cwd=crudo, check=True)
    convertir_lote(sorted(crudo.glob("**/*.mp3")), destino)


if __name__ == "__main__":
    rirs()
    audioset()
    fma()
    print("Fondos listos en", TRABAJO)
