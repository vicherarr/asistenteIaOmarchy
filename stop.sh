#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Modern Stop Script (Professional Edition)
# =============================================================================

set -euo pipefail

PID_FILE="/tmp/asistenteia.pid"
OLLAMA_FLAG="/tmp/asistenteia_started_ollama"

# Cargar configuración para saber qué modelo detener
MODEL="ministral-3:3b"
if [ -f ".env" ]; then
    MODEL=$(grep '^OLLAMA_MODEL=' .env | cut -d '=' -f2 | tr -d '[:space:]' || echo "ministral-3:3b")
fi

echo "=== Deteniendo AsistenteIA ==="

# 1. Detener el proceso principal del servidor
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "-> Enviando señal de parada al servidor (PID $PID)..."
        kill "$PID" 2>/dev/null || true
        for i in {1..5}; do
            if ! kill -0 "$PID" 2>/dev/null; then break; fi
            sleep 1
        done
        if kill -0 "$PID" 2>/dev/null; then
            echo "-> Proceso persistente, forzando SIGKILL..."
            kill -9 "$PID" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
else
    pkill -f "python -m src.main" || true
fi

# 2. Gestión de Ollama
if command -v ollama &>/dev/null; then
    echo "-> Liberando modelo '$MODEL' de la memoria de la GPU..."
    ollama stop "$MODEL" 2>/dev/null || true
fi

if [ -f "$OLLAMA_FLAG" ]; then
    echo "-> Deteniendo instancia de Ollama iniciada por este script..."
    if systemctl --user list-unit-files | grep -q ollama.service; then
        systemctl --user stop ollama.service || true
    else
        pkill -f "ollama serve" || true
    fi
    rm -f "$OLLAMA_FLAG"
fi

echo "=== Todo detenido correctamente ==="
