#!/usr/bin/env bash
# =============================================================================
# start-assistant.sh - Lanzador optimizado para ejecución manual
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -f ".env" ]; then
    MODEL_PATH=$(grep '^LITERT_MODEL_PATH=' .env | cut -d '=' -f2 | tr -d '[:space:]' || echo "models/gemma-4-e2b.litertlm")
else
    MODEL_PATH="models/gemma-4-e2b.litertlm"
fi

echo "=== AsistenteIA: Modo LiteRT ==="

# Verificar modelo LiteRT
if [ ! -f "$MODEL_PATH" ]; then
    echo "(!) Error: Modelo LiteRT no encontrado en $MODEL_PATH."
    echo "    Asegúrate de haberlo descargado correctamente."
    exit 1
fi

if [ ! -d "venv" ]; then
    echo "(!) Error: Ejecuta ./install.sh para crear el entorno virtual."
    exit 1
fi

echo "-> Servidor arrancando..."
exec ./venv/bin/python -m src.main
