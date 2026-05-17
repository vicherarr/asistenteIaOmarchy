#!/usr/bin/env bash
# =============================================================================
# start-assistant.sh - Lanzador optimizado para ejecución manual
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -f ".env" ]; then
    MODEL=$(grep '^OLLAMA_MODEL=' .env | cut -d '=' -f2 | tr -d '[:space:]' || echo "ministral-3:3b")
else
    MODEL="ministral-3:3b"
fi

echo "=== AsistenteIA: Modo Manual ==="

if ! curl -s http://localhost:11434/api/tags > /dev/null; then
    echo "(!) Error: Ollama no está respondiendo. Inícialo primero."
    exit 1
fi

# Optimización de GPU: Detener otros modelos cargados
echo "-> Optimizando memoria GPU..."
LOADED_MODELS=$(ollama ps 2>/dev/null | tail -n +2 | awk '{print $1}' || echo "")
for m in $LOADED_MODELS; do
    if [[ "$m" != "$MODEL"* ]]; then
        echo "   - Liberando modelo '$m'..."
        ollama stop "$m" 2>/dev/null || true
    fi
done

if ! ollama list | grep -q "$MODEL"; then
    echo "-> Descargando modelo '$MODEL'..."
    ollama pull "$MODEL"
fi

if [ ! -d "venv" ]; then
    echo "(!) Error: Ejecuta ./install.sh para crear el entorno virtual."
    exit 1
fi

echo "-> Servidor arrancando..."
exec ./venv/bin/python -m src.main
