#!/usr/bin/env bash
# =============================================================================
# test-overlay.sh - Lanza el overlay (GUI nueva) contra el asistente DESPLEGADO
# =============================================================================
# Prueba en vivo sin desplegar nada: usa el token/puerto de ~/.asistenteia/.env
# y arranca src/gui/luka_overlay.py con el python del sistema (PyGObject).
#
#   bash scripts/test-overlay.sh
#
# Salir: Ctrl+C en esta terminal (el overlay no tiene foco de teclado a propósito).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-$HOME/.asistenteia/.env}"

if [ ! -f "$ENV_FILE" ]; then
    echo "No encuentro $ENV_FILE (¿está instalado el asistente?)." >&2
    exit 1
fi

TOKEN="$(grep -E '^API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
PORT="$(grep -E '^PORT=' "$ENV_FILE" | cut -d= -f2-)"; PORT="${PORT:-8765}"

export LUKA_API_URL="https://127.0.0.1:${PORT}"
export LUKA_API_TOKEN="$TOKEN"

echo "Conectando el overlay a $LUKA_API_URL ..."
exec /usr/bin/python "$HERE/../src/gui/luka_overlay.py"
