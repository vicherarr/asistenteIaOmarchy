#!/usr/bin/env bash
# =============================================================================
# handy-toggle.sh - Iniciador Inteligente con Interfaz Visual
# =============================================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT=$(grep '^PORT=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d '=' -f2 | tr -d '[:space:]' || echo "8765")
TOKEN=$(grep '^API_TOKEN=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d '=' -f2 | tr -d '[:space:]')

# 1. Asegurar que el servicio está activo
SERVICE_WAS_RUNNING=true
if ! systemctl --user is-active --quiet asistenteia.service; then
    SERVICE_WAS_RUNNING=false
    notify-send "AsistenteIA" "Iniciando servicio..." -i info
    systemctl --user start asistenteia.service
    
    # Esperar a que el servidor responda
    COUNT=0
    until curl -sk "https://localhost:$PORT/status" &>/dev/null; do
        sleep 1
        COUNT=$((COUNT + 1))
        if [ $COUNT -ge 45 ]; then
            notify-send "AsistenteIA" "Error: El motor no inicia." -u critical
            exit 1
        fi
    done
fi

# 2. Levantar la Interfaz Visual (Spotlight) o mostrarla si ya existe
GUI_PID_FILE="/tmp/asistenteia-gui.pid"
LAUNCH_GUI=true

if [ -f "$GUI_PID_FILE" ]; then
    GUI_PID=$(cat "$GUI_PID_FILE")
    if kill -0 "$GUI_PID" 2>/dev/null; then
        # El proceso existe realmente
        kill -USR2 "$GUI_PID"
        LAUNCH_GUI=false
    else
        # El archivo PID es basura, lo borramos
        rm -f "$GUI_PID_FILE"
    fi
fi

if [ "$LAUNCH_GUI" = true ]; then
    "$PROJECT_DIR/scripts/start-gui.sh"
fi

# 3. Dar una pequeña tregua para que la GUI registre el cambio de estado
sleep 0.2

# 4. Solo enviar toggle si el servicio YA estaba corriendo.
#    Si acabamos de iniciar el servicio, no grabamos — el wake word ya está escuchando.
if [ "$SERVICE_WAS_RUNNING" = true ]; then
    curl -sk -X POST "https://localhost:$PORT/listen/toggle" -H "X-API-Token: $TOKEN" > /dev/null 2>&1
fi
