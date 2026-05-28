#!/usr/bin/env bash
# =============================================================================
# handy-toggle.sh - Iniciador inteligente con interfaz visual (Super + Z)
# =============================================================================
# Funciona tanto con servicio systemd como en modo bajo demanda (sin servicio).
# - Si el asistente no está activo, lo arranca y muestra la GUI.
# - Si ya estaba activo, alterna la escucha del micrófono (toggle).
# =============================================================================

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# 1. Asegurar que el motor está activo (servicio o proceso directo).
STATE="$(ai_ensure_running 45)"
if [ "$STATE" = "error" ]; then
    notify-send "AsistenteIA" "Error: el motor no inicia." -u critical 2>/dev/null || true
    exit 1
fi
if [ "$STATE" = "started" ]; then
    notify-send "AsistenteIA" "Iniciando asistente..." 2>/dev/null || true
fi

# 2. Levantar la interfaz visual (Spotlight) o mostrarla si ya existe.
LAUNCH_GUI=true
if [ -f "$GUI_PID_FILE" ]; then
    GUI_PID=$(cat "$GUI_PID_FILE")
    if kill -0 "$GUI_PID" 2>/dev/null; then
        kill -USR2 "$GUI_PID"   # señal para que la GUI se muestre
        LAUNCH_GUI=false
    else
        rm -f "$GUI_PID_FILE"
    fi
fi
[ "$LAUNCH_GUI" = true ] && "$PROJECT_DIR/scripts/start-gui.sh"

# 3. Pequeña tregua para que la GUI registre el cambio de estado.
sleep 0.2

# 4. Solo enviar toggle si el servidor YA estaba corriendo.
#    Si acabamos de arrancarlo, el wake word ya está escuchando.
if [ "$STATE" = "already" ]; then
    curl -sk -X POST "$BASE_URL/listen/toggle" -H "X-API-Token: $API_TOKEN" >/dev/null 2>&1
fi
