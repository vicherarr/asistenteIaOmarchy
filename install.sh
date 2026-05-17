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
# 1. Actualizar sistema
# -----------------------------------------------------------------------------
echo "[1/8] Actualizando sistema..."
sudo pacman -Syu --noconfirm

# -----------------------------------------------------------------------------
# 2. Instalar dependencias base de sistema
# -----------------------------------------------------------------------------
echo "[2/8] Instalando dependencias base..."
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
    python \
    python-pip \
    python-virtualenv \
    git \
    espeak-ng \
    ffmpeg

# -----------------------------------------------------------------------------
# 3. Instalar Ollama (vía yay si no está disponible)
# -----------------------------------------------------------------------------
echo "[3/8] Instalando Ollama..."
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
ollama pull ministral-3:3b

# -----------------------------------------------------------------------------
# 4. Instalar whisper.cpp para transcripción
# -----------------------------------------------------------------------------
echo "[4/8] Instalando whisper.cpp..."
if ! command -v whisper-cli &>/dev/null; then
    if command -v yay &>/dev/null; then
        yay -S --noconfirm whisper.cpp
    else
        echo "Instalar whisper.cpp manualmente: yay -S whisper.cpp"
    fi
else
    echo "whisper.cpp ya instalado."
fi

# Descargar modelo base de whisper si no existe
WHISPER_MODEL="$HOME/.cache/whisper/ggml-base.bin"
if [ ! -f "$WHISPER_MODEL" ]; then
    echo "Descargando modelo whisper base..."
    curl -L -o "$WHISPER_MODEL" \
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
fi

# -----------------------------------------------------------------------------
# 5. Instalar grim y slurp para capturas de pantalla
# -----------------------------------------------------------------------------
echo "[5/8] Instalando grim y slurp..."
sudo pacman -S --needed --noconfirm grim slurp

# -----------------------------------------------------------------------------
# 6. Configurar entorno Python del proyecto (incluye Kokoro TTS)
# -----------------------------------------------------------------------------
echo "[6/8] Configurando entorno Python..."
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -d "venv" ]; then
    python -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "Nota: Kokoro descargará su modelo (~100MB) la primera vez que se ejecute."
echo "Esto es automático y se guardará en caché."
echo ""

# -----------------------------------------------------------------------------
# 7. Configurar keybinding de Hyprland
# -----------------------------------------------------------------------------
echo "[7/8] Configurando keybindings en Hyprland..."

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
# 8. Instalar servicio systemd
# -----------------------------------------------------------------------------
echo "[8/8] Instalando servicio systemd..."
mkdir -p "$HOME/.config/systemd/user"
cp "$PROJECT_DIR/services/asistenteia.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload

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