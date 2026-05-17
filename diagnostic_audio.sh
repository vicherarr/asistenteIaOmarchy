#!/usr/bin/env bash
# =============================================================================
# diagnostic_audio.sh - Prueba de calidad de micro y transcripción
# =============================================================================

RECORD_FILE="/tmp/diag_original.wav"
CLEAN_FILE="/tmp/diag_cleaned.wav"
WHISPER_MODEL="$HOME/.cache/whisper/ggml-small.bin"

echo "--- DIAGNÓSTICO DE AUDIO ---"
echo "1. Grabando 5 segundos... ¡HABLA AHORA!"
parecord --rate=16000 --channels=1 --file-format=wav "$RECORD_FILE" &
RECORD_PID=$!
sleep 5
kill -INT $RECORD_PID
wait $RECORD_PID 2>/dev/null

echo "2. Procesando con filtros agresivos (FFmpeg)..."
# Filtro forense: Eliminación de ruido + Normalización de voz + Puerta de ruido
ffmpeg -y -i "$RECORD_FILE" \
    -af "afftdn=nf=-25,highpass=f=200,speechnorm=e=4:r=0.0001,lowpass=f=4000" \
    "$CLEAN_FILE" > /dev/null 2>&1

echo "3. Comparando resultados de Whisper:"
echo "------------------------------------------------"
echo "SOLO ORIGINAL:"
whisper-cli --model "$WHISPER_MODEL" --file "$RECORD_FILE" --language es --no-timestamps 2>/dev/null | grep '^ '

echo "------------------------------------------------"
echo "CON FILTRO AGRESIVO:"
whisper-cli --model "$WHISPER_MODEL" --file "$CLEAN_FILE" --language es --no-timestamps 2>/dev/null | grep '^ '
echo "------------------------------------------------"

echo "Prueba terminada. Archivos en /tmp/diag_original.wav y /tmp/diag_cleaned.wav"
