#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# AsistenteIA - Script de Instalación para CachyOS
# =============================================================================
# Instala todas las dependencias necesarias: Ollama, PipeWire, espeak-ng,
# Kokoro TTS y las librerías Python del proyecto.
# =============================================================================

echo "=== AsistenteIA - Instalación en CachyOS ==="

# -----------------------------------------------------------------------------
# 1. Actualizar sistema (opcional, omitido por defecto para evitar roturas)
# -----------------------------------------------------------------------------
echo "[1/7] Saltando actualización completa del sistema para mayor velocidad..."
# sudo pacman -Syu --noconfirm

# -----------------------------------------------------------------------------
# 2. Instalar dependencias base de sistema
# -----------------------------------------------------------------------------
echo "[2/7] Instalando dependencias base..."
sudo pacman -S --needed --noconfirm \
    pipewire \
    wireplumber \
    pipewire-pulse \
    pipewire-alsa \
    bluez \
    bluez-utils \
    playerctl \
    wl-clipboard \
    jq \
    python3.12 \
    python \
    python-pip \
    python-virtualenv \
    git \
    espeak-ng \
    ffmpeg

# -----------------------------------------------------------------------------
# 3. Instalar Ollama (vía yay si no está disponible)
# -----------------------------------------------------------------------------
echo "[3/7] Instalando Ollama..."
if ! command -v ollama &>/dev/null; then
    if command -v yay &>/dev/null; then
        yay -S --noconfirm ollama
    else
        echo "yay no encontrado. Instalando Ollama desde script oficial..."
        curl -fsSL https://ollama.com/install.sh | sh
    fi
else
    echo "Ollama ya instalado."
fi

# Iniciar y habilitar servicio Ollama
systemctl --user enable --now ollama.service 2>/dev/null || true

# Descargar modelo Ministral 3:3b
echo "Descargando modelo ministral-3:3b..."
ollama pull ministral-3:3b || true

# -----------------------------------------------------------------------------
# 4. Instalar grim y slurp para capturas de pantalla
# -----------------------------------------------------------------------------
echo "[4/7] Instalando grim y slurp..."
sudo pacman -S --needed --noconfirm grim slurp

# -----------------------------------------------------------------------------
# 5. Configurar entorno Python del proyecto (incluye Kokoro TTS y Whisper)
# -----------------------------------------------------------------------------
echo "[5/7] Configurando entorno Python..."
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -d "venv" ]; then
    # Python 3.12 es la versión recomendada (kokoro/misaki no soportan 3.13+)
    if command -v python3.12 &>/dev/null; then
        echo "Usando Python 3.12..."
        python3.12 -m venv venv
    elif command -v python3.11 &>/dev/null; then
        echo "Usando Python 3.11..."
        python3.11 -m venv venv
    else
        echo "ERROR: Se requiere Python 3.11 o 3.12."
        echo "Instala python3.12 o python3.11 y vuelve a ejecutar este script."
        exit 1
    fi
fi

source venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# Instalar navegadores para Playwright (Dynamic Web Tool)
echo "Instalando navegadores de Playwright..."
playwright install chromium

echo ""
echo "Nota: Kokoro descargará su modelo (~100MB) la primera vez que se ejecute."
echo "Esto es automático y se guardará en caché."
echo ""

# -----------------------------------------------------------------------------
# 5b. Descargar modelo LiteRT Gemma-4-E4B desde HuggingFace
# -----------------------------------------------------------------------------
echo "Descargando modelo LiteRT Gemma-4-E4B-it (~3.6GB)..."
mkdir -p "$PROJECT_DIR/models"
if [ ! -f "$PROJECT_DIR/models/gemma-4-E4B-it.litertlm" ]; then
    "$PROJECT_DIR/venv/bin/python" -c "
from huggingface_hub import hf_hub_download
print('Descargando gemma-4-E4B-it.litertlm...')
path = hf_hub_download(
    repo_id='litert-community/gemma-4-E4B-it-litert-lm',
    filename='gemma-4-E4B-it.litertlm',
    local_dir='$PROJECT_DIR/models'
)
print(f'Listo: {path}')
"
else
    echo "Modelo ya descargado."
fi

# -----------------------------------------------------------------------------
# 6. Configurar keybinding de Hyprland
# -----------------------------------------------------------------------------
echo "[6/7] Configurando keybindings en Hyprland..."

BINDINGS_FILE="$HOME/.config/hypr/bindings.lua"
if [ -f "$BINDINGS_FILE" ]; then
    if ! grep -q "handy-toggle.sh" "$BINDINGS_FILE"; then
        echo "" >> "$BINDINGS_FILE"
        echo "-- AsistenteIA" >> "$BINDINGS_FILE"
        echo "o.bind(\"SUPER + Z\", \"AsistenteIA Listen\", \"$PROJECT_DIR/scripts/handy-toggle.sh\")" >> "$BINDINGS_FILE"
        echo "o.bind(\"SUPER + X\", \"AsistenteIA Stop\", \"$PROJECT_DIR/scripts/stop-assistant.sh\")" >> "$BINDINGS_FILE"
        echo "Keybindings añadidos a $BINDINGS_FILE"
    else
        echo "Keybindings ya configurados."
    fi
else
    echo "Advertencia: $BINDINGS_FILE no encontrado. Añadir manualmente:"
    echo "  o.bind(\"SUPER + Z\", \"AsistenteIA\", \"$PROJECT_DIR/scripts/handy-toggle.sh\")"
    echo "  o.bind(\"SUPER + X\", \"AsistenteIA Stop\", \"$PROJECT_DIR/scripts/stop-assistant.sh\")"
fi

# -----------------------------------------------------------------------------
# 7. Instalar servicio systemd
# -----------------------------------------------------------------------------
echo "[7/7] Instalando servicio systemd de forma dinámica..."
"$PROJECT_DIR/installservice.sh"

# -----------------------------------------------------------------------------
# Finalización
# -----------------------------------------------------------------------------
echo ""
echo "=== Instalación completada ==="
echo ""
echo "Siguientes pasos:"
echo "  1. Recargar Hyprland: hyprctl reload"
echo "  2. Iniciar el asistente: ./start.sh"
echo "  3. Super+Z para hablar, Super+X para detener"
echo ""
echo "Para instalar como servicio systemd:"
echo "  systemctl --user enable --now asistenteia.service"