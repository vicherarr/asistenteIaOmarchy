#!/usr/bin/env bash
#
# Genera el corpus sintético para entrenar la wake word "Luka".
#
# # Por qué no se usa el generador de microWakeWord tal cual
#
# El flujo estándar de microWakeWord genera los positivos con el checkpoint
# LibriTTS-R de piper-sample-generator: 904 hablantes, muchísima variedad... y
# **solo inglés**. No existe un checkpoint equivalente en español, así que las
# muestras saldrían con acento inglés y el modelo aprendería a reconocer algo
# que nadie va a decir en esta casa.
#
# En su lugar se recorren las 8 voces españolas de Piper (ES, MX, AR). Son
# muchas menos voces, así que la variedad hay que sacarla de otro sitio:
#   - velocidades y "ruidos" de síntesis distintos (--length-scales, --noise-*),
#   - y sobre todo la augmentación posterior (ruido de fondo, reverberación,
#     cambio de tono, EQ), que es la que de verdad simula hablantes distintos.
#
# # Negativos adversarios
#
# "Luka" son dos sílabas cortas: el riesgo no es que no dispare, es que dispare
# con cualquier cosa parecida. Por eso se genera también un corpus de palabras
# vecinas (loca, lupa, luna, nunca...) que entran al entrenamiento como
# negativos. Es lo que enseña al modelo dónde está la frontera.
#
# Ojo con lo que NO se puede arreglar aquí: en español "Luca" se pronuncia
# exactamente igual que "Luka". Ninguna cantidad de entrenamiento distingue dos
# sonidos idénticos. "Lucas" y "Lucía" sí son distinguibles (hay sonido
# después), y por eso están en la lista de negativos.
#
# Uso:  ./generar_muestras.sh [directorio_de_trabajo]
set -euo pipefail

TRABAJO="${1:-$HOME/luka-wakeword}"
GEN="$TRABAJO/piper-sample-generator"
VOCES="$TRABAJO/voices"
PY="$TRABAJO/.venv/bin/python"
# Las libs CUDA viven dentro del venv; sin esto torch no encuentra el runtime y
# se cae a CPU en silencio.
export LD_LIBRARY_PATH="$TRABAJO/.venv/lib/python3.11/site-packages/nvidia/cu13/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$GEN"

# Cuántas muestras de la palabra. Más de lo que sugiere el notebook de
# referencia (1000) precisamente porque hay menos voces que compensar.
POSITIVOS="${POSITIVOS:-4000}"
# Por palabra vecina. La suma de todas debe quedar en el mismo orden de
# magnitud que los positivos.
NEGATIVOS_POR_PALABRA="${NEGATIVOS_POR_PALABRA:-150}"

# Vecinas fonéticas: comparten la forma o los sonidos de "Luka" sin serlo.
VECINAS=(
    "loca" "lupa" "luna" "lucha" "luces" "lugar" "luego" "lujo"
    "boca" "poca" "roca" "toca" "nunca" "busca" "duda" "ruta"
    "cuca" "muñeca" "Lucas" "Lucía" "Bruno" "gruta" "juega" "buscar"
)

modelos=()
for voz in "$VOCES"/*.onnx; do
    modelos+=(--model "$voz")
done
if [ ${#modelos[@]} -eq 0 ]; then
    echo "No hay voces en $VOCES. Ejecuta antes preparar_entorno.sh" >&2
    exit 1
fi

generar() {
    local texto="$1" destino="$2" cuantas="$3"
    if [ -d "$destino" ] && [ -n "$(ls -A "$destino" 2>/dev/null)" ]; then
        echo "  ya existe, se salta: $destino"
        return
    fi
    mkdir -p "$destino"
    # --length-scales cubre desde hablar deprisa hasta arrastrar la palabra;
    # es la variación que más se nota entre personas reales.
    "$PY" -m piper_sample_generator "$texto" \
        "${modelos[@]}" \
        --max-samples "$cuantas" \
        --length-scales 0.7 0.85 1.0 1.15 1.3 \
        --noise-scales 0.667 0.8 0.9 1.0 \
        --output-dir "$destino" \
        > "$destino/.generacion.log" 2>&1
}

echo "== Positivos: \"Luka\" ($POSITIVOS muestras, ${#modelos[@]} voces)"
# El punto final importa: sin él algunas voces se comen la última vocal.
generar "Luka." "$TRABAJO/muestras/positivas" "$POSITIVOS"

echo "== Negativos adversarios (${#VECINAS[@]} palabras x $NEGATIVOS_POR_PALABRA)"
for palabra in "${VECINAS[@]}"; do
    echo "  $palabra"
    generar "$palabra." "$TRABAJO/muestras/adversarias/$palabra" "$NEGATIVOS_POR_PALABRA"
done

echo
echo "Positivas:   $(find "$TRABAJO/muestras/positivas" -name '*.wav' | wc -l)"
echo "Adversarias: $(find "$TRABAJO/muestras/adversarias" -name '*.wav' | wc -l)"
