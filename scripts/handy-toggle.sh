#!/usr/bin/env bash
# =============================================================================
# handy-toggle.sh - Script para alternar la escucha con Super+Z
# =============================================================================

ORCHESTRATOR_URL="http://localhost:8765/transcribe"
CANCEL_URL="http://localhost:8765/cancel"
STATUS_URL="http://localhost:8765/status"
LOCK_FILE="/tmp/handy-assistant.lock"
WHISPER_MODEL="$HOME/.cache/whisper/ggml-base.bin"
LOG_FILE="/tmp/asistente-toggle.log"

log() {
    echo "$(date '+%H:%M:%S') - $1" >> "$LOG_FILE"
}

log "=== Script iniciado ==="

# Verificar si el servicio está corriendo
if ! curl -s "$STATUS_URL" &>/dev/null; then
    log "Servicio no disponible, iniciando..."
    notify-send "AsistenteIA" "Iniciando servicio de voz..."

    PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
    if [ -f "$PROJECT_DIR/start.sh" ]; then
        bash "$PROJECT_DIR/start.sh" &>/dev/null &
    fi

    for i in $(seq 1 30); do
        if curl -s "$STATUS_URL" &>/dev/null; then
            log "Servicio listo tras $((i)) segundos"
            notify-send "AsistenteIA" "Servicio listo. Pulsa Super+Z de nuevo para hablar."
            rm -f "$LOCK_FILE"
            exit 0
        fi
        sleep 1
    done

    log "ERROR: Servicio no respondió en 30 segundos"
    notify-send "AsistenteIA" "No se pudo iniciar el servicio"
    rm -f "$LOCK_FILE"
    exit 1
fi

cleanup() {
    rm -f "$LOCK_FILE"
    log "Cleanup done"
}
trap cleanup EXIT

# Si estamos grabando, parar la grabación (segunda pulsación)
if [ -f "$LOCK_FILE" ]; then
    log "Lock existe, deteniendo grabación (eliminando lock file)"
    rm -f "$LOCK_FILE"
    notify-send "AsistenteIA" "Procesando..."
    exit 0
fi

# Si el servidor está procesando, cancelar y empezar a grabar
PROCESSING=$(curl -s "$STATUS_URL" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('processing','False'))" 2>/dev/null || echo "False")
if [ "$PROCESSING" = "True" ]; then
    log "Servidor ocupado, cancelando respuesta..."
    curl -s -X POST "$CANCEL_URL" &>/dev/null
    notify-send "AsistenteIA" "Interrumpido. Escuchando... habla ahora"
    pkill -f "paplay\|ffplay\|aplay" 2>/dev/null || true
    sleep 0.5
else
    notify-send "AsistenteIA" "Escuchando... habla ahora"
fi

log "Iniciando escucha..."
echo $$ > "$LOCK_FILE"

# Detectar micrófono Bluetooth
BT_SOURCE=$(wpctl status 2>/dev/null | grep -i "bluez_input" | head -1 | grep -oP '\d+\.\s*bluez_input' | grep -oP '^\d+' || true)
log "BT_SOURCE=$BT_SOURCE"

if [ -n "$BT_SOURCE" ]; then
    log "Configurando mic BT como default..."
    wpctl set-default "$BT_SOURCE" 2>/dev/null || true
fi

TEMP_WAV=$(mktemp /tmp/handy-rec-XXXXXX.wav)
log "Grabando en $TEMP_WAV"

# Grabar con parecord (compatible PipeWire, usa el source default)
parecord --rate=16000 --channels=1 --file-format=wav "$TEMP_WAV" &
RECORD_PID=$!
log "parecord PID=$RECORD_PID"

# Esperar a que el usuario presione Super+Z de nuevo
while [ -f "$LOCK_FILE" ]; do
    sleep 0.5
done

log "Lock eliminado, parando grabación..."

# Parar grabación limpiamente
kill -INT $RECORD_PID 2>/dev/null
sleep 1
wait $RECORD_PID 2>/dev/null || true

notify-send "AsistenteIA" "Procesando audio..."

if [ ! -s "$TEMP_WAV" ]; then
    log "ERROR: WAV vacío o no existe"
    notify-send "AsistenteIA" "No se detectó audio"
    rm -f "$TEMP_WAV"
    exit 0
fi

log "Audio grabado: $(du -h "$TEMP_WAV" | cut -f1)"

# Transcribir con whisper-cli
if command -v whisper-cli &>/dev/null && [ -f "$WHISPER_MODEL" ]; then
    log "Ejecutando whisper-cli..."
    WHISPER_RAW=$(whisper-cli --model "$WHISPER_MODEL" --file "$TEMP_WAV" --language es --no-timestamps 2>&1)
    log "Whisper terminado"

    TRANSCRIPTION=$(echo "$WHISPER_RAW" | grep -v ':' | grep '^ ' | sed 's/^[[:space:]]*//' | head -1 || true)
    log "Transcripción: '$TRANSCRIPTION'"

    if [ -n "$TRANSCRIPTION" ]; then
        log "Enviando al servidor..."
        notify-send "AsistenteIA" "Has dicho: $TRANSCRIPTION"
        RESPONSE=$(curl -s -X POST "$ORCHESTRATOR_URL" \
            -H "Content-Type: application/json" \
            -d "{\"text\": \"$TRANSCRIPTION\"}" 2>&1)
        log "Respuesta: $RESPONSE"
        notify-send "AsistenteIA" "Respuesta generada"
    else
        log "Sin transcripción"
        notify-send "AsistenteIA" "No se pudo transcribir"
    fi
else
    log "whisper-cli no disponible"
    notify-send "AsistenteIA" "whisper-cli no disponible"
fi

rm -f "$TEMP_WAV"
log "=== Script terminado ==="