#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Service Uninstaller (Professional Edition)
# =============================================================================
# Detiene, deshabilita y elimina el servicio systemd del asistente.
# =============================================================================

set -euo pipefail

SERVICE_NAME="asistenteia.service"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

echo "=== AsistenteIA: Desinstalando Servicio Systemd ==="

# 1. Comprobar si el servicio existe
if [ ! -f "$SYSTEMD_USER_DIR/$SERVICE_NAME" ]; then
    echo "-> El servicio '$SERVICE_NAME' no parece estar instalado en $SYSTEMD_USER_DIR."
    echo "-> Nada que hacer."
    exit 0
fi

# 2. Detener el servicio si está activo
if systemctl --user is-active "$SERVICE_NAME" > /dev/null 2>&1; then
    echo "-> Deteniendo servicio activo..."
    systemctl --user stop "$SERVICE_NAME"
fi

# 3. Deshabilitar el inicio automático
if systemctl --user is-enabled "$SERVICE_NAME" > /dev/null 2>&1; then
    echo "-> Deshabilitando inicio automático..."
    systemctl --user disable "$SERVICE_NAME"
fi

# 4. Eliminar el archivo del servicio
echo "-> Eliminando archivo de servicio..."
rm "$SYSTEMD_USER_DIR/$SERVICE_NAME"

# 5. Recargar systemd
echo "-> Recargando demonio de systemd..."
systemctl --user daemon-reload

# 6. Limpieza de archivos temporales de ejecución
echo "-> Limpiando archivos temporales..."
rm -f /tmp/asistenteia.pid
rm -f /tmp/asistenteia.log

echo "=== Servicio desinstalado correctamente ==="
echo "Nota: El entorno virtual (venv) y los archivos del proyecto permanecen intactos."
