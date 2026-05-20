#!/usr/bin/env bash
# =============================================================================
# handy-toggle.sh - Iniciador Inteligente con Interfaz Visual
# =============================================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT=$(grep '^PORT=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d '=' -f2 | tr -d '[:space:]' || echo "8765")

# 1. Asegurar que el servicio está activo
if ! systemctl --user is-active --quiet asistenteia.service; then
    notify-send "AsistenteIA" "Iniciando servicio..." -i info
    systemctl --user start asistenteia.service
    
    # Esperar a que el servidor responda
    COUNT=0
    until curl -s "http://localhost:$PORT/status" &>/dev/null; do
        sleep 1
        COUNT=$((COUNT + 1))
        if [ $COUNT -ge 15 ]; then
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

# 4. Enviar señal de toggle al servidor para empezar a grabar
curl -s -X POST "http://localhost:$PORT/listen/toggle" > /dev/null 2>&1
