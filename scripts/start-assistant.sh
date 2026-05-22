#!/usr/bin/env bash
# =============================================================================
# start-assistant.sh - Lanzador optimizado para ejecución manual
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -f ".env" ]; then
    MODEL_PATH=$(grep '^LITERT_MODEL_PATH=' .env | cut -d '=' -f2 | tr -d '[:space:]' || echo "models/gemma-4-E4B-it.litertlm")
else
    MODEL_PATH="models/gemma-4-E4B-it.litertlm"
fi

echo "=== AsistenteIA: Modo LiteRT ==="

# --- 1. Validación y Recreación Dinámica del venv ---
if [ -d "venv" ]; then
    # Verificar si el venv es válido para la ubicación actual y responde correctamente
    if ! ./venv/bin/python -c "import sys, os; sys.exit(0 if sys.prefix == os.path.join(os.getcwd(), 'venv') else 1)" &>/dev/null; then
        echo "(!) Se detectó un entorno virtual inválido o copiado de otro directorio."
        echo "-> Recreando el entorno virtual para asegurar su correcto funcionamiento..."
        rm -rf venv
    fi
fi

if [ ! -d "venv" ]; then
    echo "-> Entorno virtual no encontrado. Ejecutando instalación automática..."
    ./install.sh
fi

# --- 2. Sincronización Automática de Dependencias ---
REQ_HASH_FILE="venv/.requirements.hash"
CURRENT_HASH=""
if command -v md5sum &>/dev/null; then
    CURRENT_HASH=$(md5sum requirements.txt | cut -d' ' -f1)
elif command -v sha256sum &>/dev/null; then
    CURRENT_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)
fi

if [ -n "$CURRENT_HASH" ]; then
    if [ ! -f "$REQ_HASH_FILE" ] || [ "$(cat "$REQ_HASH_FILE")" != "$CURRENT_HASH" ]; then
        echo "-> Detectados cambios en requirements.txt o instalación de dependencias pendiente..."
        ./venv/bin/pip install --upgrade pip setuptools wheel
        if ./venv/bin/pip install -r requirements.txt; then
            echo "$CURRENT_HASH" > "$REQ_HASH_FILE"
            echo "-> Dependencias actualizadas con éxito."
        else
            echo "(!) Advertencia: Error instalando dependencias. Se intentará arrancar de todos modos."
        fi
    fi
fi

# Verificar modelo LiteRT
if [ ! -f "$MODEL_PATH" ]; then
    echo "(!) Error: Modelo LiteRT no encontrado en $MODEL_PATH."
    echo "    Asegúrate de haberlo descargado correctamente."
    exit 1
fi

echo "-> Servidor arrancando..."
exec ./venv/bin/python -m src.main
