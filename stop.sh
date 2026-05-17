#!/usr/bin/env bash
# =============================================================================
# stop.sh - Script de parada total (Sistema y Servicio)
# =============================================================================
set -euo pipefail

echo "=== Deteniendo AsistenteIA ==="

# 1. Detener el servicio systemd si existe
if systemctl --user list-unit-files | grep -q asistenteia.service; then
    echo "-> Deteniendo servicio systemd..."
    systemctl --user stop asistenteia.service || true
fi

# 2. Asegurar parada de procesos manuales/huérfanos
PID_FILE="/tmp/asistenteia.pid"
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    echo "-> Deteniendo proceso PID $PID..."
    kill "$PID" 2>/dev/null || true
    rm -f "$PID_FILE"
fi

echo "-> Limpiando procesos de Python y liberando puertos..."
pkill -f "python -m src.main" || true

# 3. Liberar puerto forzosamente
PORT="8765"
if [ -f ".env" ]; then
    PORT=$(grep '^PORT=' .env | cut -d '=' -f2 | tr -d '[:space:]' || echo "8765")
fi
fuser -k "$PORT"/tcp >/dev/null 2>&1 || true

notify-send "AsistenteIA" "Servicio detenido correctamente" -i info
echo "=== Todo detenido correctamente ==="
