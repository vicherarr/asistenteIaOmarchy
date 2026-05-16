#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

PID_FILE="/tmp/asistenteia.pid"
LOG_FILE="/tmp/asistenteia.log"

# Verificar si el puerto ya está en uso
EXISTING_PID=$(lsof -ti:8765 2>/dev/null || true)
if [ -n "$EXISTING_PID" ]; then
    echo "ADVERTENCIA: Puerto 8765 en uso por PID $EXISTING_PID"
    echo "Deteniendo proceso existente..."
    kill "$EXISTING_PID" 2>/dev/null || true
    sleep 2
    # Verificar que se liberó
    if lsof -ti:8765 &>/dev/null; then
        echo "Proceso no respondió, forzando..."
        kill -9 "$EXISTING_PID" 2>/dev/null || true
        sleep 1
    fi
fi

# Verificar si ya está corriendo
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "ERROR: AsistenteIA ya está corriendo (PID: $PID)"
        exit 1
    else
        echo "PID stale encontrado. Limpiando..."
        rm -f "$PID_FILE"
    fi
fi

# Verificar entorno virtual
if [ ! -d "venv" ]; then
    echo "ERROR: Entorno virtual no encontrado. Ejecutar install.sh primero"
    exit 1
fi

# Verificar Ollama
if command -v ollama &>/dev/null; then
    if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
        echo "ADVERTENCIA: Ollama no parece estar corriendo. Iniciar con: ollama serve"
    fi
else
    echo "ADVERTENCIA: Ollama no está instalado"
fi

echo "Iniciando AsistenteIA..."

# Ejecutar en background
./venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8765 >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Esperar a que levante
echo "Esperando al servidor..."
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:8765/status &>/dev/null; then
        echo "AsistenteIA corriendo (PID: $SERVER_PID)"
        echo "Logs: tail -f $LOG_FILE"
        exit 0
    fi
    sleep 1
done

echo "ERROR: El servidor no respondió en 30 segundos"
echo "Revisa los logs: cat $LOG_FILE"
kill "$SERVER_PID" 2>/dev/null || true
rm -f "$PID_FILE"
exit 1
