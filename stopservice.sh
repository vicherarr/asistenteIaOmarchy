#!/usr/bin/env bash
# =============================================================================
# stopservice.sh - Proxy para systemctl
# =============================================================================
set -euo pipefail

echo "-> Solicitando parada a systemd..."
systemctl --user stop asistenteia.service
echo "-> Servicio detenido."
