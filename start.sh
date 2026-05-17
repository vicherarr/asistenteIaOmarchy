#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Modern Start Script (Professional Edition)
# =============================================================================
# Maneja el ciclo de vida del asistente, asegurando que Ollama esté disponible
# y el modelo cargado antes de iniciar la API de FastAPI.
# =============================================================================

set -euo pipefail

# --- Configuración de rutas y archivos ---
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# Cargar configuraciones del .env si existe para obtener el puerto y modelo
if [ -f ".env" ]; then
    PORT=$(grep '^PORT=' .env | cut -d '=' -f2 | tr -d '[:space:]' || echo "8765")
    MODEL_PATH=$(grep '^LITERT_MODEL_PATH=' .env | cut -d '=' -f2 | tr -d '[:space:]' || echo "models/gemma-4-e2b.litertlm")
else
    PORT="8765"
    MODEL_PATH="models/gemma-4-e2b.litertlm"
fi

PID_FILE="/tmp/asistenteia.pid"
LOG_FILE="/tmp/asistenteia.log"

echo "=== AsistenteIA (LiteRT): Iniciando en puerto $PORT ==="

# --- 1. Gestión de procesos previos ---
EXISTING_PID=$(lsof -ti:"$PORT" 2>/dev/null || true)
if [ -n "$EXISTING_PID" ]; then
    echo "(!) Puerto $PORT ocupado por PID $EXISTING_PID. Intentando liberar..."
    kill "$EXISTING_PID" 2>/dev/null || true
    sleep 2
fi

# --- 2. Validación de entorno ---
if [ ! -d "venv" ]; then
    echo "(!) Error: Entorno virtual 'venv' no encontrado. Ejecuta ./install.sh"
    exit 1
fi

# Verificar modelo LiteRT
if [ ! -f "$MODEL_PATH" ]; then
    echo "(!) Error: Modelo LiteRT no encontrado en $MODEL_PATH."
    exit 1
fi

# --- 3. Lanzamiento del Asistente ---
echo "-> Iniciando Orchestrator con LiteRT..."
./venv/bin/python -m src.main > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Verificación de salud
echo "-> Verificando salud del servicio..."
COUNT=0
until curl -s "http://127.0.0.1:$PORT/status" &>/dev/null; do
    sleep 1
    COUNT=$((COUNT + 1))
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "(!) Error: El proceso del servidor murió inesperadamente."
        cat "$LOG_FILE"
        exit 1
    fi
    if [ $COUNT -ge 20 ]; then
        echo "(!) Error: El asistente no responde en el puerto $PORT."
        exit 1
    fi
done

echo "=== AsistenteIA listo y escuchando en $PORT ==="
echo "Logs disponibles en: tail -f $LOG_FILE"
