#!/usr/bin/env bash
# =============================================================================
# stop-assistant.sh - Detenedor global de emergencia
# =============================================================================

set -euo pipefail

notify-send "AsistenteIA" "Deteniendo todos los servicios..."

if systemctl --user is-active asistenteia.service > /dev/null 2>&1; then
    systemctl --user stop asistenteia.service
fi

PID_FILE="/tmp/asistenteia.pid"
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    kill "$PID" 2>/dev/null || true
    rm -f "$PID_FILE"
fi

pkill -f "python -m src.main" || true

GUI_PID_FILE="/tmp/asistenteia-gui.pid"
if [ -f "$GUI_PID_FILE" ]; then
    echo "-> Deteniendo interfaz visual..."
    GUI_PID=$(cat "$GUI_PID_FILE")
    kill "$GUI_PID" 2>/dev/null || true
    rm -f "$GUI_PID_FILE"
fi

if [ -f "/tmp/asistenteia_started_ollama" ]; then
    echo "-> Deteniendo Ollama residual..."
    pkill -f "ollama serve" || true
    rm -f "/tmp/asistenteia_started_ollama"
fi

notify-send "AsistenteIA" "Todo detenido y limpio."
echo "Detención completada."
