#!/usr/bin/env bash
set -euo pipefail

PID_FILE="/tmp/asistenteia.pid"
OLLAMA_FLAG="/tmp/asistenteia_started_ollama"

echo "Deteniendo AsistenteIA..."

if [ ! -f "$PID_FILE" ]; then
    echo "No se encontró archivo PID. Buscando proceso..."
    PID=$(pgrep -f "uvicorn src.main:app" 2>/dev/null || true)
    if [ -z "$PID" ]; then
        echo "AsistenteIA no está corriendo."
    else
        kill "$PID" 2>/dev/null || true
        sleep 2
        if kill -0 "$PID" 2>/dev/null; then
            echo "Proceso no respondió a SIGTERM, enviando SIGKILL..."
            kill -9 "$PID" 2>/dev/null || true
        fi
        echo "AsistenteIA detenido (PID: $PID)."
    fi
else
    PID=$(cat "$PID_FILE")
fi

if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$PID" 2>/dev/null; then
        echo "Proceso no respondió a SIGTERM, enviando SIGKILL..."
        kill -9 "$PID" 2>/dev/null || true
    fi
    echo "AsistenteIA detenido (PID: $PID)."
else
    echo "El proceso ${PID:-} ya no estaba corriendo."
fi

rm -f "$PID_FILE"

# Descargar modelo de memoria
if command -v ollama &>/dev/null; then
    echo "Descargando modelo de memoria..."
    ollama stop gemma4:e2b 2>/dev/null || true
fi

# Detener Ollama si lo iniciamos nosotros
if [ -f "$OLLAMA_FLAG" ]; then
    echo "Deteniendo Ollama (fue iniciado por AsistenteIA)..."
    OLLAMA_PID=$(pgrep -f "ollama serve" 2>/dev/null || true)
    if [ -n "$OLLAMA_PID" ]; then
        kill $OLLAMA_PID 2>/dev/null || true
        sleep 3
        if pgrep -f "ollama serve" &>/dev/null; then
            echo "Ollama no respondió, forzando..."
            pkill -9 -f "ollama serve" 2>/dev/null || true
        fi
        echo "Ollama detenido."
    else
        echo "Ollama ya no estaba corriendo."
    fi
    rm -f "$OLLAMA_FLAG"
else
    echo "Ollama no fue iniciado por AsistenteIA, se deja corriendo."
fi