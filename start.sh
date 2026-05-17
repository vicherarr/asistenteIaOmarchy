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
    MODEL=$(grep '^OLLAMA_MODEL=' .env | cut -d '=' -f2 | tr -d '[:space:]' || echo "ministral-3:3b")
else
    PORT="8765"
    MODEL="ministral-3:3b"
fi

PID_FILE="/tmp/asistenteia.pid"
LOG_FILE="/tmp/asistenteia.log"
OLLAMA_FLAG="/tmp/asistenteia_started_ollama"

echo "=== AsistenteIA: Iniciando en puerto $PORT ==="

# --- 1. Gestión de procesos previos ---
EXISTING_PID=$(lsof -ti:"$PORT" 2>/dev/null || true)
if [ -n "$EXISTING_PID" ]; then
    echo "(!) Puerto $PORT ocupado por PID $EXISTING_PID. Intentando liberar..."
    kill "$EXISTING_PID" 2>/dev/null || true
    sleep 2
    if lsof -ti:"$PORT" &>/dev/null; then
        echo "(!) El proceso no cedió, forzando cierre..."
        kill -9 "$EXISTING_PID" 2>/dev/null || true
        sleep 1
    fi
fi

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "(!) Error: Ya existe una instancia corriendo (PID: $PID)"
        exit 1
    fi
    rm -f "$PID_FILE"
fi

# --- 2. Validación de entorno ---
if [ ! -d "venv" ]; then
    echo "(!) Error: Entorno virtual 'venv' no encontrado. Ejecuta ./install.sh"
    exit 1
fi

# --- 3. Preparación de Ollama ---
if ! command -v ollama &>/dev/null; then
    echo "(!) Error: 'ollama' no está instalado en el sistema."
    exit 1
fi

# Iniciar Ollama si no responde
if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    echo "-> Ollama no detectado. Iniciando servicio..."
    if systemctl --user list-unit-files | grep -q ollama.service; then
        systemctl --user start ollama.service
    else
        ollama serve > /dev/null 2>&1 &
    fi
    touch "$OLLAMA_FLAG"
    echo "-> Esperando a que Ollama despierte..."
    COUNT=0
    until curl -s http://localhost:11434/api/tags &>/dev/null; do
        sleep 2
        COUNT=$((COUNT + 1))
        if [ $COUNT -ge 30 ]; then
            echo "(!) Error: Ollama tardó demasiado en iniciar."
            exit 1
        fi
    done
    echo "-> Ollama listo."
fi

# Optimización de GPU: Detener otros modelos cargados
echo "-> Optimizando memoria GPU..."
LOADED_MODELS=$(ollama ps 2>/dev/null | tail -n +2 | awk '{print $1}' || echo "")
for m in $LOADED_MODELS; do
    if [[ "$m" != "$MODEL"* ]]; then
        echo "   - Liberando modelo '$m'..."
        ollama stop "$m" 2>/dev/null || true
    fi
done

# Verificar/Descargar modelo
if ! ollama list | grep -q "$MODEL"; then
    echo "-> Modelo '$MODEL' no encontrado. Descargando (esto puede tardar)..."
    ollama pull "$MODEL"
fi

# --- 4. Lanzamiento del Asistente ---
echo "-> Iniciando Orchestrator..."
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
