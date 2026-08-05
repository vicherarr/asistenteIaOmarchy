#!/usr/bin/env python3
"""Augmenta las muestras y las convierte en espectrogramas para entrenar.

Este paso es el que de verdad fabrica la variedad. Las 8 voces españolas de
Piper suenan a 8 personas; lo que hace que el modelo generalice a cualquiera
que entre en el salón es lo que se les hace encima:

- **Ruido de fondo** entre -5 y +10 dB de SNR: música, cacharros, tráfico. Que
  el modelo aprenda a oír la palabra por debajo del ruido, no en un estudio.
- **Reverberación** con impulsos de habitaciones reales: es la diferencia entre
  hablar pegado al micro y hablar desde el sofá.
- **Tono, EQ, distorsión**: cambian el timbre, que es lo más parecido a
  inventar hablantes nuevos.

Se generan dos conjuntos:

- `features_positivas`  — "Luka" (verdad positiva).
- `features_adversarias` — las palabras vecinas (verdad negativa). Enseñan
  dónde está la frontera, que con una palabra tan corta es todo el problema.

Uso:  ./preparar_features.py [directorio_de_trabajo]
"""

import os
import sys
from pathlib import Path

from microwakeword.audio.augmentation import Augmentation
from microwakeword.audio.clips import Clips
from microwakeword.audio.spectrograms import SpectrogramGeneration
from mmap_ninja.ragged import RaggedMmap

TRABAJO = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "luka-wakeword")

# Los tres splits que espera el entrenamiento. `repeticion` >1 en training hace
# que la misma muestra se augmente varias veces: más variedad sin más TTS.
SPLITS = [("training", "train", 2), ("validation", "validation", 1), ("testing", "test", 1)]


def construir(origen: Path, destino: Path, patron: str = "*.wav") -> None:
    # Se comprueba que haya datos DENTRO del mmap, no que exista la carpeta: una
    # ejecución interrumpida deja los directorios creados y vacíos, y con una
    # comprobación más laxa este paso se saltaba en silencio y el entrenamiento
    # arrancaba sin muestras positivas.
    if destino.exists() and all(
        (destino / split / "wakeword_mmap" / "data.ninja").exists() for split, _, _ in SPLITS
    ):
        print(f"{destino.name}: ya está")
        return

    clips = Clips(
        input_directory=str(origen),
        # Ojo: el glob de `Clips` no es recursivo. Las adversarias viven en una
        # subcarpeta por palabra, así que necesitan "**/*.wav" o se quedaría
        # con cero muestras sin decir nada.
        file_pattern=patron,
        max_clip_duration_s=None,
        remove_silence=False,
        random_split_seed=10,
        split_count=0.1,
    )

    augmenter = Augmentation(
        # 3,2 s de contexto por muestra: cabe la palabra con margen por delante
        # y por detrás, que es lo que ve el modelo en streaming.
        augmentation_duration_s=3.2,
        augmentation_probabilities={
            "SevenBandParametricEQ": 0.25,
            "TanhDistortion": 0.25,
            "PitchShift": 0.5,
            "BandStopFilter": 0.25,
            "AddColorNoise": 0.25,
            "AddBackgroundNoise": 0.75,
            "Gain": 1.0,
            "RIR": 0.5,
        },
        impulse_paths=[str(TRABAJO / "mit_rirs")],
        background_paths=[str(TRABAJO / "fma_16k"), str(TRABAJO / "audioset_16k")],
        background_min_snr_db=-5,
        background_max_snr_db=10,
        min_jitter_s=0.195,
        max_jitter_s=0.205,
    )

    for split, nombre_interno, repeticion in SPLITS:
        salida = destino / split
        salida.mkdir(parents=True, exist_ok=True)
        # slide_frames simula, entrenando en modo no-streaming, las inferencias
        # desplazadas que hará el modelo ya en la placa. En test no hace falta:
        # allí se evalúa el modelo en streaming de verdad.
        espectrogramas = SpectrogramGeneration(
            clips=clips,
            augmenter=augmenter,
            slide_frames=10 if split == "training" else (10 if split == "validation" else 1),
            step_ms=10,
        )
        RaggedMmap.from_generator(
            out_dir=str(salida / "wakeword_mmap"),
            sample_generator=espectrogramas.spectrogram_generator(
                split=nombre_interno, repeat=repeticion
            ),
            batch_size=100,
            verbose=True,
        )


if __name__ == "__main__":
    os.chdir(TRABAJO)
    print("== Positivas ('Luka')")
    construir(TRABAJO / "muestras" / "positivas", TRABAJO / "features_positivas")
    print("== Adversarias (palabras vecinas)")
    construir(TRABAJO / "muestras" / "adversarias", TRABAJO / "features_adversarias", "**/*.wav")
    print("Listo.")
