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

# 2. Levantar el overlay visual. Está SIEMPRE visible (punto mini que reacciona
#    al estado por SSE), así que no hay que "mostrarlo": start-gui.sh no duplica
#    si ya hay uno vivo.
"$PROJECT_DIR/scripts/start-gui.sh"

# 2b. Si el overlay ya estaba vivo pero oculto (el usuario pulsó Esc), pedirle
#     que se muestre de nuevo con SIGUSR2. Inofensivo si ya estaba visible.
GUI_PID_FILE="/tmp/asistenteia-gui.pid"
if [ -f "$GUI_PID_FILE" ]; then
    kill -USR2 "$(cat "$GUI_PID_FILE")" 2>/dev/null || true
fi

# 3. Pequeña tregua para que la GUI registre el cambio de estado.
sleep 0.2

# 4. Solo enviar toggle si el servidor YA estaba corriendo.
#    Si acabamos de arrancarlo, el wake word ya está escuchando.
if [ "$STATE" = "already" ]; then
    curl -sk -X POST "$BASE_URL/listen/toggle" -H "X-API-Token: $API_TOKEN" >/dev/null 2>&1
fi
