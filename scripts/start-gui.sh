#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Start GUI (overlay Luka, layer-shell de Omarchy/Hyprland)
# =============================================================================
# El overlay es una superficie layer-shell GTK4: se ejecuta con el PYTHON DEL
# SISTEMA (tiene PyGObject), NO con el venv de inferencia. Habla con el backend
# por HTTP/SSE, así que no necesita las dependencias del venv.
# =============================================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

GUI_PID_FILE="/tmp/asistenteia-gui.pid"

# Si ya hay un overlay vivo, no lanzar otro.
if [ -f "$GUI_PID_FILE" ] && kill -0 "$(cat "$GUI_PID_FILE")" 2>/dev/null; then
    exit 0
fi

# Lanzar en segundo plano con el Python del sistema.
/usr/bin/python src/gui/luka_overlay.py > /tmp/asistenteia-gui.log 2>&1 &
echo $! > "$GUI_PID_FILE"
