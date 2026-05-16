#!/usr/bin/env bash
set -euo pipefail

echo "Deteniendo AsistenteIA (servicio)..."

if ! systemctl --user is-active asistenteia.service &>/dev/null; then
    echo "El servicio ya está detenido."
else
    systemctl --user stop asistenteia.service
    sleep 2
    if systemctl --user is-active asistenteia.service &>/dev/null; then
        echo "ERROR: No se pudo detener el servicio"
        systemctl --user status asistenteia.service --no-pager
        exit 1
    fi
    echo "AsistenteIA detenido."
fi

# Detener Ollama también
if command -v ollama &>/dev/null; then
    echo "Descargando modelo de memoria..."
    ollama stop gemma4:e2b 2>/dev/null || true
    echo "Deteniendo Ollama..."
    systemctl --user stop ollama.service 2>/dev/null || true
    OLLAMA_PID=$(pgrep -f "ollama serve" 2>/dev/null || true)
    if [ -n "$OLLAMA_PID" ]; then
        kill $OLLAMA_PID 2>/dev/null || true
        sleep 3
        if pgrep -f "ollama serve" &>/dev/null; then
            pkill -9 -f "ollama serve" 2>/dev/null || true
        fi
    fi
    echo "Ollama detenido."
fi