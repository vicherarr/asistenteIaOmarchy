#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Service Installer
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="asistenteia.service"
SERVICE_SRC="$PROJECT_DIR/services/$SERVICE_NAME"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

echo "=== Instalador de Servicio Systemd (Modo Usuario) ==="

if [ ! -f "$SERVICE_SRC" ]; then
    echo "(!) Error: No se encuentra el archivo de servicio en $SERVICE_SRC"
    exit 1
fi

if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "(!) Error: Entorno virtual no detectado. Ejecuta ./install.sh primero."
    exit 1
fi

TEMP_SERVICE=$(mktemp)
cp "$SERVICE_SRC" "$TEMP_SERVICE"
ESCAPED_DIR=$(echo "$PROJECT_DIR" | sed 's/\//\\\//g')

sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" "$TEMP_SERVICE"
sed -i "s|^ExecStart=.*|ExecStart=$PROJECT_DIR/venv/bin/python -m src.main|" "$TEMP_SERVICE"

mkdir -p "$SYSTEMD_USER_DIR"
cp "$TEMP_SERVICE" "$SYSTEMD_USER_DIR/$SERVICE_NAME"
rm "$TEMP_SERVICE"

echo "-> Recargando demonio de systemd..."
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"

echo "=== Servicio instalado y habilitado ==="
echo "Para iniciar ahora: systemctl --user start $SERVICE_NAME"
echo "Para ver logs: journalctl --user -u $SERVICE_NAME -f"
