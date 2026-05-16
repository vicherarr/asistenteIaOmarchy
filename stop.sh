#!/usr/bin/env bash
set -euo pipefail

PID_FILE="/tmp/asistenteia.pid"

echo "Deteniendo AsistenteIA..."

if [ ! -f "$PID_FILE" ]; then
    echo "No se encontró archivo PID. Buscando proceso..."
    PID=$(pgrep -f "uvicorn src.main:app" 2>/dev/null || true)
    if [ -z "$PID" ]; then
        echo "AsistenteIA no está corriendo."
        exit 0
    fi
else
    PID=$(cat "$PID_FILE")
fi

if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$PID" 2>/dev/null; then
        echo "Proceso no respondió a SIGTERM, enviando SIGKILL..."
        kill -9 "$PID" 2>/dev/null || true
    fi
    echo "AsistenteIA detenido (PID: $PID)."
else
    echo "El proceso $PID ya no estaba corriendo."
fi

rm -f "$PID_FILE"
