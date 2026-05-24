#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Service Installer
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="asistenteia.service"
SERVICE_SRC="$PROJECT_DIR/services/$SERVICE_NAME"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

echo "=== Instalador de Servicio AsistenteIA ==="

if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "(!) Error: Entorno virtual no detectado. Ejecuta ./install.sh primero."
    exit 1
fi

# Crear archivo de servicio dinámico
mkdir -p "$SYSTEMD_USER_DIR"
cat <<EOF > "$SYSTEMD_USER_DIR/$SERVICE_NAME"
[Unit]
Description=AsistenteIA - Voice Assistant Orchestrator
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStartPre=/usr/bin/bash -c "/usr/bin/fuser -k 8765/tcp || /usr/bin/true"
ExecStart=$PROJECT_DIR/venv/bin/python -m src.main
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=DISPLAY=$DISPLAY
Environment=WAYLAND_DISPLAY=$WAYLAND_DISPLAY
Environment=XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus

[Install]
WantedBy=default.target
EOF

echo "-> Recargando systemd..."
systemctl --user daemon-reload
# NOTA: No habilitamos el servicio por defecto para evitar que se ejecute en el arranque.
# Se iniciará bajo demanda al presionar Super + Z.
# systemctl --user enable "$SERVICE_NAME"

echo "=== Instalación completada ==="
echo "Puedes iniciarlo con: ./startservice.sh"
echo "O ver logs con: journalctl --user -u $SERVICE_NAME -f"
