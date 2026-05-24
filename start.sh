#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Start Script
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# Cargar configuraciones del .env
PORT="8765"
if [ -f ".env" ]; then
    PORT=$(grep '^PORT=' .env | cut -d '=' -f2 | tr -d '[:space:]' || echo "8765")
fi

PID_FILE="/tmp/asistenteia.pid"
LOG_FILE="/tmp/asistenteia.log"

echo "=== AsistenteIA: Iniciando en puerto $PORT ==="

# --- 1. Limpieza de procesos y puertos ---
# Asegurar que no hay nada bloqueando el puerto
if command -v fuser >/dev/null 2>&1; then
    fuser -k "$PORT"/tcp >/dev/null 2>&1 || true
elif command -v lsof >/dev/null 2>&1; then
    LSOF_PID=$(lsof -ti:"$PORT" 2>/dev/null || true)
    if [ -n "$LSOF_PID" ]; then
        kill -9 "$LSOF_PID" 2>/dev/null || true
    fi
fi

# Matar procesos huérfanos por nombre si es necesario
pkill -f "python -m src.main" || true
sleep 1

# --- 2. Validación y Recreación Dinámica del venv ---
if [ -d "venv" ]; then
    # Verificar si el venv es válido para la ubicación actual y responde correctamente
    if ! ./venv/bin/python -c "import sys, os; sys.exit(0 if sys.prefix == os.path.join(os.getcwd(), 'venv') else 1)" &>/dev/null; then
        echo "(!) Se detectó un entorno virtual inválido o copiado de otro directorio."
        echo "-> Recreando el entorno virtual para asegurar su correcto funcionamiento..."
        rm -rf venv
    fi
fi

if [ ! -d "venv" ]; then
    echo "-> Entorno virtual no encontrado. Ejecutando instalación automática..."
    ./install.sh
fi

# --- 2b. Sincronización Automática de Dependencias ---
REQ_HASH_FILE="venv/.requirements.hash"
CURRENT_HASH=""
if command -v md5sum &>/dev/null; then
    CURRENT_HASH=$(md5sum requirements.txt | cut -d' ' -f1)
elif command -v sha256sum &>/dev/null; then
    CURRENT_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)
fi

if [ -n "$CURRENT_HASH" ]; then
    if [ ! -f "$REQ_HASH_FILE" ] || [ "$(cat "$REQ_HASH_FILE")" != "$CURRENT_HASH" ]; then
        echo "-> Detectados cambios en requirements.txt o instalación de dependencias pendiente..."
        ./venv/bin/pip install --upgrade pip setuptools wheel
        if ./venv/bin/pip install -r requirements.txt; then
            echo "$CURRENT_HASH" > "$REQ_HASH_FILE"
            echo "-> Dependencias actualizadas con éxito."
        else
            echo "(!) Advertencia: Error instalando dependencias. Se intentará arrancar de todos modos."
        fi
    fi
fi

# --- 3. Lanzamiento ---
echo "-> Iniciando Orchestrator..."
./venv/bin/python -m src.main > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Verificación de salud
echo "-> Verificando servicio..."
COUNT=0
until curl -s "http://127.0.0.1:$PORT/status" &>/dev/null; do
    sleep 1
    COUNT=$((COUNT + 1))
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "(!) Error: El proceso murió inesperadamente."
        tail -n 20 "$LOG_FILE"
        exit 1
    fi
    if [ $COUNT -ge 60 ]; then
        echo "(!) Error: El asistente no responde en el puerto $PORT después de 60s."
        exit 1
    fi
done

echo "=== AsistenteIA listo y escuchando en $PORT ==="
