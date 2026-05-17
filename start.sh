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

# --- 2. Validación de entorno ---
if [ ! -d "venv" ]; then
    echo "(!) Error: Entorno virtual 'venv' no encontrado. Ejecuta ./install.sh"
    exit 1
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
    if [ $COUNT -ge 30 ]; then
        echo "(!) Error: El asistente no responde en el puerto $PORT después de 30s."
        exit 1
    fi
done

echo "=== AsistenteIA listo y escuchando en $PORT ==="
