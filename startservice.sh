#!/usr/bin/env bash
# =============================================================================
# startservice.sh - Proxy para systemctl
# =============================================================================
set -euo pipefail

echo "-> Solicitando inicio a systemd..."
systemctl --user start asistenteia.service

echo "-> Estado del servicio:"
systemctl --user status asistenteia.service --no-pager || true
