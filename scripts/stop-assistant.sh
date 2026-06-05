#!/usr/bin/env bash
# =============================================================================
# stop-assistant.sh - Detenedor global (Super + X)
# =============================================================================
# Detiene el asistente sea cual sea el modo: servicio systemd o proceso directo.
# =============================================================================

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

notify-send "AsistenteIA" "Deteniendo el asistente..." 2>/dev/null || true

# 1. Detener el servicio systemd si está activo.
if ai_service_active; then
    systemctl --user stop "$SERVICE_NAME"
fi

# 2. Detener cualquier proceso directo y liberar el puerto.
ai_stop_process

# 2b. Detener el sidecar TabbyAPI (ExLlama). Con servicio systemd, el stop de
#     arriba ya lo paró (PartOf); esto cubre el modo sin servicio. Seguro siempre:
#     solo afecta a NUESTRA instalación.
ai_tabby_stop

# 3. Cerrar la interfaz visual.
ai_stop_gui

notify-send "AsistenteIA" "Asistente detenido." 2>/dev/null || true
echo "Detención completada."
