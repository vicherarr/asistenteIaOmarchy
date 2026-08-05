#!/usr/bin/env bash
#
# Monta el entorno para entrenar la wake word "Luka".
#
# Todo vive en un directorio de trabajo FUERA del repo (por defecto
# ~/luka-wakeword): los datasets son decenas de GB y este repositorio es
# público. Aquí solo se versionan los scripts, la configuración y el .tflite
# final, que ocupa ~40 kB.
#
# # Dos venvs, no uno
#
# La generación de muestras necesita PyTorch (que hoy trae CUDA 13) y el
# entrenamiento necesita TensorFlow (que trae CUDA 12). Meterlos en el mismo
# venv es pelearse con dos runtimes de CUDA para no ganar nada, así que van
# separados: .venv genera, .venv-train entrena.
#
# # El detalle que hace perder una tarde
#
# Las libs de CUDA vienen dentro de los venvs, pero ni torch ni TensorFlow las
# encuentran solos: hay que ponerlas en LD_LIBRARY_PATH. Si no, **ambos se caen
# a CPU en silencio** — sin error, solo un aviso perdido entre cien líneas de
# log. Los scripts de este directorio ya lo hacen; si lanzas algo a mano,
# acuérdate.
set -euo pipefail

TRABAJO="${1:-$HOME/luka-wakeword}"
mkdir -p "$TRABAJO"
cd "$TRABAJO"

echo "== Repositorios"
[ -d microWakeWord ] || git clone --depth 1 https://github.com/kahrendt/microWakeWord.git
[ -d piper-sample-generator ] || git clone --depth 1 https://github.com/rhasspy/piper-sample-generator.git

echo "== Venv de generación (.venv): PyTorch + Piper"
[ -d .venv ] || uv venv --python 3.11 .venv
VIRTUAL_ENV="$TRABAJO/.venv" uv pip install -q -e ./piper-sample-generator

echo "== Venv de entrenamiento (.venv-train): TensorFlow + microWakeWord"
[ -d .venv-train ] || uv venv --python 3.11 .venv-train
# `datasets` va anclado a la serie 3 a propósito: desde la 4 decodificar
# audio exige `torchcodec`, que arrastra PyTorch entero a este venv. La 3
# decodifica con soundfile y no añade nada.
VIRTUAL_ENV="$TRABAJO/.venv-train" uv pip install -q -e ./microWakeWord 'tensorflow[and-cuda]>=2.18' \
    'git+https://github.com/whatsnowplaying/audio-metadata@d4ebb238e6a401bb1a5aaaac60c9e2b3cb30929f' \
    'datasets<4' soundfile tensorboard tqdm scipy

echo "== Voces españolas de Piper"
# Las 8 que existen: no hay más en español. El generador multi-hablante de
# microWakeWord (904 voces) es solo inglés; ver generar_muestras.sh.
mkdir -p voices
for voz in \
    es_AR/daniela/high/es_AR-daniela-high \
    es_ES/carlfm/x_low/es_ES-carlfm-x_low \
    es_ES/davefx/medium/es_ES-davefx-medium \
    es_ES/mls_10246/low/es_ES-mls_10246-low \
    es_ES/mls_9972/low/es_ES-mls_9972-low \
    es_ES/sharvard/medium/es_ES-sharvard-medium \
    es_MX/ald/medium/es_MX-ald-medium \
    es_MX/claude/high/es_MX-claude-high
do
    nombre=$(basename "$voz")
    for ext in onnx onnx.json; do
        [ -f "voices/$nombre.$ext" ] || \
            curl -sL -o "voices/$nombre.$ext" \
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/$voz.$ext"
    done
done

echo "== Comprobación de GPU"
LD_LIBRARY_PATH="$TRABAJO/.venv/lib/python3.11/site-packages/nvidia/cu13/lib" \
    ./.venv/bin/python -c "import torch; print('  torch  ->', torch.cuda.is_available())"
LD_LIBRARY_PATH="$(find "$TRABAJO/.venv-train/lib/python3.11/site-packages/nvidia" -name lib -type d | tr '\n' ':')" \
    ./.venv-train/bin/python -c "import tensorflow as tf; print('  tf     ->', bool(tf.config.list_physical_devices('GPU')))" 2>/dev/null

echo
echo "Listo. Siguiente: ./generar_muestras.sh"
