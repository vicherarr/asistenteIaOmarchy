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

# Iniciar Ollama si no está corriendo
if command -v ollama &>/dev/null; then
    if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
        echo "Ollama no está corriendo. Iniciándolo..."
        ollama serve &>/dev/null &
        OLLAMA_STARTED=true
        echo "Esperando a que Ollama esté listo..."
        for i in $(seq 1 60); do
            if curl -s http://localhost:11434/api/tags &>/dev/null; then
                echo "Ollama listo."
                break
            fi
            if [ "$i" -eq 60 ]; then
                echo "ERROR: Ollama no respondió en 2 minutos"
                exit 1
            fi
            sleep 2
        done
    else
        echo "Ollama ya está corriendo."
    fi

    # Verificar modelo LLM
    if ! ollama list 2>/dev/null | grep -q "gemma4"; then
        echo "Modelo gemma4:e2b no encontrado. Descargándolo..."
        ollama pull gemma4:e2b
    else
        echo "Modelo gemma4:e2b disponible."
    fi

    # Precargar modelo en memoria
    echo "Precargando modelo gemma4:e2b en memoria..."
    curl -s http://localhost:11434/api/chat -d '{"model":"gemma4:e2b","messages":[{"role":"user","content":"hola"}],"stream":false}' &>/dev/null &
    WARMUP_PID=$!
else
    echo "ADVERTENCIA: Ollama no está instalado"
fi

echo "Iniciando AsistenteIA..."

# Ejecutar en background
./venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8765 >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Guardar si iniciamos Ollama nosotros
if [ "${OLLAMA_STARTED:-false}" = "true" ]; then
    echo "$$" > /tmp/asistenteia_started_ollama
else
    rm -f /tmp/asistenteia_started_ollama
fi

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