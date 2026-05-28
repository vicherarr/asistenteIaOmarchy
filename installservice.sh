#!/usr/bin/env bash
# =============================================================================
# installservice.sh - Instalador del servicio systemd de usuario
# =============================================================================
# Crea ~/.config/systemd/user/asistenteia.service apuntando al directorio real
# de instalación (sin rutas hardcodeadas).
#
# Dos modos de entorno gráfico (Wayland/D-Bus):
#   - Bajo demanda (por defecto): el unit NO hornea WAYLAND_DISPLAY/DISPLAY.
#     handy-toggle.sh importa el entorno gráfico vivo justo antes de arrancar,
#     así siempre es correcto aunque cambie entre sesiones.
#   - Arranque en sesión (--enable): hornea el entorno gráfico actual en el unit
#     y habilita el arranque automático al iniciar sesión.
#
# Uso:
#   ./installservice.sh            # instala el unit (arranque bajo demanda)
#   ./installservice.sh --enable   # además arranca automáticamente con la sesión
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="asistenteia.service"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

ENABLE_BOOT=false
[ "${1:-}" = "--enable" ] && ENABLE_BOOT=true

echo "=== Instalador de servicio AsistenteIA ==="

if [ ! -x "$PROJECT_DIR/venv/bin/python" ]; then
    echo "(!) Error: entorno virtual no detectado en $PROJECT_DIR/venv. Ejecuta ./install.sh primero."
    exit 1
fi

# Entorno gráfico horneado solo en modo arranque-en-sesión.
GRAPHICAL_ENV=""
if [ "$ENABLE_BOOT" = true ]; then
    GRAPHICAL_ENV="Environment=DISPLAY=${DISPLAY:-:0}
Environment=WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-1}
Environment=DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/%U/bus}"
fi

mkdir -p "$SYSTEMD_USER_DIR"
cat > "$SYSTEMD_USER_DIR/$SERVICE_NAME" <<EOF
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
Environment=XDG_RUNTIME_DIR=/run/user/%U
$GRAPHICAL_ENV

[Install]
WantedBy=default.target
EOF

echo "-> Servicio escrito en $SYSTEMD_USER_DIR/$SERVICE_NAME"
echo "-> Recargando systemd..."
systemctl --user daemon-reload

# Propagar el entorno gráfico vivo al gestor systemd --user (modo bajo demanda).
systemctl --user import-environment \
    DISPLAY WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_CURRENT_DESKTOP \
    2>/dev/null || true

if [ "$ENABLE_BOOT" = true ]; then
    echo "-> Habilitando arranque automático al iniciar sesión..."
    systemctl --user enable "$SERVICE_NAME"
fi

echo "=== Servicio instalado ==="
echo "Iniciar ahora:   systemctl --user start $SERVICE_NAME"
echo "Ver logs:        journalctl --user -u $SERVICE_NAME -f"
if [ "$ENABLE_BOOT" = true ]; then
    echo "Estado arranque: habilitado (se inicia automáticamente con la sesión)"
else
    echo "Estado arranque: bajo demanda (no se inicia solo; usa Super + Z)"
fi
