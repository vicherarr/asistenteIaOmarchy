#!/usr/bin/env bash
# =============================================================================
# handy-toggle.sh - Iniciador Inteligente para AsistenteIA
# =============================================================================

# 1. Obtener puerto del .env (o por defecto 8765)
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT=$(grep '^PORT=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d '=' -f2 | tr -d '[:space:]' || echo "8765")

# 2. Comprobar si el servicio systemd está activo
if ! systemctl --user is-active --quiet asistenteia.service; then
    notify-send "AsistenteIA" "Iniciando servicio..." -i info
    systemctl --user start asistenteia.service
    
    # Esperar un máximo de 10 segundos a que el servicio esté listo
    COUNT=0
    until curl -s "http://localhost:$PORT/status" &>/dev/null; do
        sleep 1
        COUNT=$((COUNT + 1))
        if [ $COUNT -ge 10 ]; then
            notify-send "AsistenteIA" "Error: El servicio tarda demasiado en iniciar." -u critical
            exit 1
        fi
    done
fi

# 3. Enviar señal de toggle al servidor (ahora que sabemos que está vivo)
curl -s -X POST "http://localhost:$PORT/listen/toggle" > /dev/null 2>&1
