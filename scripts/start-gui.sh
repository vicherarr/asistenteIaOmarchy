#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Start GUI (Spotlight)
# =============================================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# Asegurar que el entorno virtual está activo para las dependencias
export PYTHONPATH="$PROJECT_DIR"

# Ejecutar con el binario de Python del venv en SEGUNDO PLANO
./venv/bin/python src/gui/spotlight.py > /tmp/asistenteia-gui.log 2>&1 &
