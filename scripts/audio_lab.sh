#!/usr/bin/env bash
# =============================================================================
# scripts/audio_lab.sh - El laboratorio de STT para micros "asquerosos"
# =============================================================================
# Este script permite probar múltiples filtros de audio sobre la misma grabación
# para encontrar la configuración perfecta antes de tocar el código principal.

RECORD_FILE="/tmp/lab_original.wav"
WHISPER_MODEL="$HOME/.cache/whisper/ggml-small.bin"

mkdir -p /tmp/audio_lab

echo "--- 🎙️ LABORATORIO DE AUDIO ASISTENTEIA ---"
echo "Grabando 10 segundos. Por favor, di la frase completa."
echo "¡HABLA AHORA!"
parecord --rate=16000 --channels=1 --file-format=wav "$RECORD_FILE" &
RECORD_PID=$!
sleep 10
kill -INT $RECORD_PID
wait $RECORD_PID 2>/dev/null

echo ""
echo "--- 🧪 PROCESANDO PRUEBAS ---"
# ... (mantener funciones)
# Exp 6: Extreme Precision (Beam 10)
test_config "ULTRA_PRECISION" "loudnorm" "10"


# Función para probar una configuración
test_config() {
    local label=$1
    local filters=$2
    local beam=$3
    local output="/tmp/audio_lab/test_${label}.wav"

    # 1. Aplicar filtros
    ffmpeg -y -i "$RECORD_FILE" -af "$filters" -ar 16000 -ac 1 "$output" > /dev/null 2>&1
    
    # 2. Transcribir
    local result=$(whisper-cli --model "$WHISPER_MODEL" --file "$output" --language es --no-timestamps --beam-size "$beam" 2>/dev/null | grep '^ ' | sed 's/^[[:space:]]*//')
    
    echo -e "Config [$label]:\n   - Filtros: $filters\n   - Beam: $beam\n   - RESULTADO: \"$result\"\n"
}

# --- LISTA DE EXPERIMENTOS ---

# Exp 1: Baseline (Lo que tenemos ahora)
test_config "BASELINE" "loudnorm" "1"

# Exp 2: Solo Beam Size 5 (Sin filtros nuevos)
test_config "BEAM_ONLY" "loudnorm" "5"

# Exp 3: Denoiser Adaptativo + Beam 5
test_config "DENOISER_ANLMDN" "anlmdn=nhw=20,loudnorm" "5"

# Exp 4: Ecualizador de Consonantes + Denoiser + Beam 5
test_config "VOICE_BOOST" "anlmdn=nhw=20,equalizer=f=3000:width_type=h:w=2000:g=6,loudnorm" "5"

# Exp 5: Reducción de ruido FFT fuerte + Compresor + Beam 5
test_config "FORENSIC" "afftdn=nf=-25,speechnorm=e=4,loudnorm" "5"

echo "------------------------------------------------"
echo "Laboratorio terminado. Prueba a escuchar los archivos en /tmp/audio_lab/ si quieres."
