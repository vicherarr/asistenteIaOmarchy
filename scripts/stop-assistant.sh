#!/usr/bin/env bash
# =============================================================================
# stop-assistant.sh - Detiene AsistenteIA y Ollama por completo
# =============================================================================

notify-send "AsistenteIA" "Deteniendo servicio de voz asistente..."

echo "Deteniendo AsistenteIA..."

# Detener servicio systemd si está activo
if systemctl --user is-active asistenteia.service &>/dev/null; then
    systemctl --user stop asistenteia.service
    echo "Servicio AsistenteIA detenido."
fi

# Detener proceso manual si está corriendo
PID_FILE="/tmp/asistenteia.pid"
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null || true
        sleep 2
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID" 2>/dev/null || true
        fi
        echo "Proceso AsistenteIA detenido (PID: $PID)."
    fi
    rm -f "$PID_FILE"
fi

# Matar cualquier uvicorn residual
pkill -f "uvicorn src.main:app" 2>/dev/null || true

# Detener Ollama
notify-send "AsistenteIA" "Deteniendo Ollama..."
echo "Deteniendo Ollama..."
if systemctl --user is-active ollama.service &>/dev/null; then
    systemctl --user stop ollama.service
    echo "Servicio Ollama detenido."
else
    OLLAMA_PID=$(pgrep -f "ollama serve" 2>/dev/null || true)
    if [ -n "$OLLAMA_PID" ]; then
        kill $OLLAMA_PID 2>/dev/null || true
        sleep 2
        if pgrep -f "ollama serve" &>/dev/null; then
            pkill -9 -f "ollama serve" 2>/dev/null || true
        fi
        echo "Ollama detenido."
    else
        echo "Ollama no está corriendo."
    fi
fi

# Limpiar archivos temporales
rm -f /tmp/asistenteia_started_ollama

notify-send "AsistenteIA" "Todo detenido"
echo "AsistenteIA y Ollama detenidos."