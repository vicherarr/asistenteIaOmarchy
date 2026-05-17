#!/usr/bin/env bash
# =============================================================================
# logs.sh - Ver los logs del asistente en tiempo real
# =============================================================================

echo "-> Mostrando logs de AsistenteIA (Pulsa Ctrl+C para salir)..."
journalctl --user -u asistenteia.service -f
