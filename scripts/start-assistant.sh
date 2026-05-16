#!/usr/bin/env bash
# =============================================================================
# start-assistant.sh - Inicia el servidor orchestrator del asistente
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== AsistenteIA - Iniciando ==="

# Verificar Ollama
if ! command -v ollama &>/dev/null; then
    echo "ERROR: Ollama no está instalado"
    exit 1
fi

if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    echo "ERROR: Ollama no está corriendo. Iniciar con: ollama serve"
    exit 1
fi

# Verificar modelo
if ! ollama list 2>/dev/null | grep -q "gemma4"; then
    echo "Descargando modelo gemma4:e4b..."
    ollama pull gemma4:e4b
fi

# Activar entorno virtual
if [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "ERROR: Entorno virtual no encontrado. Ejecutar install.sh primero"
    exit 1
fi

# Iniciar servidor
echo "Servidor escuchando en http://127.0.0.1:8765"
echo "Presiona Ctrl+C para detener"
echo ""

exec python -m src.main
