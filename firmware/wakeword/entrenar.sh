#!/usr/bin/env bash
#
# Entrena el modelo de la wake word y lo deja convertido a TFLite en streaming
# y cuantizado a int8, que es lo único que sabe ejecutar la placa.
#
# Tarda del orden de una hora en una GPU de escritorio. Se puede parar y
# relanzar: retoma desde el último checkpoint.
#
# La arquitectura (mixednet con estos filtros y kernels) es la de referencia de
# microWakeWord. No se ha tocado a propósito: es la que está probada en miles
# de dispositivos, y aquí lo que hay que ajustar es el corpus, no la red.
set -euo pipefail

TRABAJO="${1:-$HOME/luka-wakeword}"
CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/training_parameters.yaml"

cd "$TRABAJO"
# Sin esto TensorFlow no encuentra las libs de CUDA que vienen en el propio
# venv y entrena en CPU sin avisar (tardaría un día en vez de una hora).
export LD_LIBRARY_PATH="$(find "$TRABAJO/.venv-train/lib/python3.11/site-packages/nvidia" -name lib -type d | tr '\n' ':')${LD_LIBRARY_PATH:-}"

# La GPU de esta máquina la comparte el motor del propio asistente (TabbyAPI
# tiene ~5 GB de los 8 tomados). Por defecto TensorFlow reserva de golpe casi
# toda la memoria libre y luego no puede crecer: la evaluación del set ambiente
# copia ~1 GB de una vez y revienta con "Dst tensor is not initialized", que no
# menciona en ningún momento que el problema sea la memoria compartida.
#
# Con crecimiento bajo demanda cabe en lo que queda. Si aun así no cabe (porque
# el asistente esté cargando otro modelo), entrenar en CPU es el plan B:
# exporta CUDA_VISIBLE_DEVICES="" antes de llamar a este script.
export TF_FORCE_GPU_ALLOW_GROWTH=true

cp "$CONFIG" "$TRABAJO/training_parameters.yaml"

./.venv-train/bin/python -m microwakeword.model_train_eval \
    --training_config="$TRABAJO/training_parameters.yaml" \
    --train 1 \
    --restore_checkpoint 1 \
    --test_tf_nonstreaming 0 \
    --test_tflite_nonstreaming 0 \
    --test_tflite_nonstreaming_quantized 0 \
    --test_tflite_streaming 0 \
    --test_tflite_streaming_quantized 1 \
    --use_weights "best_weights" \
    mixednet \
    --pointwise_filters "64,64,64,64" \
    --repeat_in_block "1, 1, 1, 1" \
    --mixconv_kernel_sizes '[5], [7,11], [9,15], [23]' \
    --residual_connection "0,0,0,0" \
    --first_conv_filters 32 \
    --first_conv_kernel_size 5 \
    --stride 3

echo
echo "Modelo en: $TRABAJO/trained_models/luka/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite"
echo "Para llevarlo al firmware:"
echo "  cp ese_fichero firmware/crates/luka-wakeword/modelo/luka.tflite"
