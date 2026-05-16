#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# installservice.sh - Instala AsistenteIA como servicio systemd de usuario
# =============================================================================

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_FILE="$PROJECT_DIR/services/asistenteia.service"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo "=== AsistenteIA - Instalación de servicio systemd ==="

# Verificar que el servicio existe
if [ ! -f "$SERVICE_FILE" ]; then
    echo "ERROR: No se encontró $SERVICE_FILE"
    exit 1
fi

# Verificar que el entorno virtual existe
if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "ERROR: Entorno virtual no encontrado. Ejecutar install.sh primero"
    exit 1
fi

# Verificar que start-assistant.sh existe y es ejecutable
START_SCRIPT="$PROJECT_DIR/scripts/start-assistant.sh"
if [ ! -f "$START_SCRIPT" ]; then
    echo "ERROR: No se encontró $START_SCRIPT"
    exit 1
fi
chmod +x "$START_SCRIPT"

# Crear directorio systemd si no existe
mkdir -p "$SYSTEMD_DIR"

# Instalar el servicio
echo "Instalando servicio..."
cp "$SERVICE_FILE" "$SYSTEMD_DIR/asistenteia.service"

# Recargar systemd
systemctl --user daemon-reload

# Habilitar el servicio (arranca con el usuario)
systemctl --user enable asistenteia.service

echo ""
echo "=== Servicio instalado correctamente ==="
echo ""
echo "Comandos útiles:"
echo "  systemctl --user start asistenteia        # Iniciar servicio"
echo "  systemctl --user stop asistenteia         # Detener servicio"
echo "  systemctl --user restart asistenteia      # Reiniciar servicio"
echo "  systemctl --user status asistenteia       # Ver estado"
echo "  journalctl --user -u asistenteia -f       # Ver logs en vivo"
echo ""
echo "Nota: El servicio se inicia automáticamente al iniciar sesión."