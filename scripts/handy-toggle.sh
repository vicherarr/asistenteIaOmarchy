#!/usr/bin/env bash
# =============================================================================
# handy-toggle.sh - Script optimizado para alternar la escucha (Arquitectura 2.0)
# =============================================================================
# Toda la lógica de grabación y STT se ha movido al servidor Python para
# reducir la latencia y mejorar la robustez. Este script es ahora un disparador.

PORT=$(grep '^PORT=' "$(dirname "$0")/../.env" 2>/dev/null | cut -d '=' -f2 | tr -d '[:space:]' || echo "8765")

# Enviar señal de toggle al servidor
curl -s -X POST "http://localhost:$PORT/listen/toggle" > /dev/null 2>&1 &
