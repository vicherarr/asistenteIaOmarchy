#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# AsistenteIA - Script de Instalación para CachyOS
# =============================================================================
# Instala todas las dependencias necesarias: Ollama, PipeWire, Handy, TTS local
# y las librerías Python del proyecto.
# =============================================================================

echo "=== AsistenteIA - Instalación en CachyOS ==="

# -----------------------------------------------------------------------------
# 1. Actualizar sistema
# -----------------------------------------------------------------------------
echo "[1/7] Actualizando sistema..."
sudo pacman -Syu --noconfirm

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
    python \
    python-pip \
    python-virtualenv \
    git

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

# Descargar modelo Gemma 4:e4b
echo "Descargando modelo gemma4:e4b..."
ollama pull gemma4:e4b

# -----------------------------------------------------------------------------
# 4. Instalar Handy (app de transcripción)
# -----------------------------------------------------------------------------
echo "[4/7] Instalando Handy..."
if command -v yay &>/dev/null; then
    yay -S --noconfirm handy-transcribe 2>/dev/null || \
        echo "Handy no disponible en AUR. Instalar manualmente desde https://github.com/marvinkreis/handy"
else
    echo "Instalar Handy manualmente: yay -S handy-transcribe"
fi

# -----------------------------------------------------------------------------
# 5. Instalar Piper TTS (síntesis de voz local rápida)
# -----------------------------------------------------------------------------
echo "[5/7] Instalando Piper TTS..."
if command -v yay &>/dev/null; then
    yay -S --noconfirm piper-tts 2>/dev/null || \
        echo "piper-tts no disponible en AUR. Instalando vía pip..."
fi

# Fallback: instalar piper-tts via pip si no está en AUR
if ! command -v piper &>/dev/null; then
    pip install piper-tts 2>/dev/null || \
        echo "Advertencia: piper-tts no pudo instalarse. Usar coqui-tts como alternativa."
    pip install coqui-tts 2>/dev/null || true
fi

# Descargar voz en español para Piper
PIPER_VOICES_DIR="${HOME}/.local/share/piper-voices"
mkdir -p "$PIPER_VOICES_DIR"
if [ ! -f "$PIPER_VOICES_DIR/es_ES-mls_10246-low.onnx" ]; then
    echo "Descargando voz española para Piper TTS..."
    curl -L -o "$PIPER_VOICES_DIR/es_ES-mls_10246-low.onnx" \
        "https://huggingface.co/rhassyc/piper-voices/resolve/main/es/es_ES/mls_10246/es_ES-mls_10246-low.onnx"
    curl -L -o "$PIPER_VOICES_DIR/es_ES-mls_10246-low.onnx.json" \
        "https://huggingface.co/rhassyc/piper-voices/resolve/main/es/es_ES/mls_10246/es_ES-mls_10246-low.onnx.json"
fi

# -----------------------------------------------------------------------------
# 6. Configurar entorno Python del proyecto
# -----------------------------------------------------------------------------
echo "[6/7] Configurando entorno Python..."
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -d "venv" ]; then
    python -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# -----------------------------------------------------------------------------
# 7. Configurar keybinding de Hyprland
# -----------------------------------------------------------------------------
echo "[7/7] Configurando keybinding Alt+Z en Hyprland..."

HYPRCONF="$HOME/.config/hypr/hyprland.conf"
KEYBIND_LINE="bind = ALT, Z, exec, ${PROJECT_DIR}/scripts/handy-toggle.sh"

if [ -f "$HYPRCONF" ]; then
    if ! grep -q "handy-toggle.sh" "$HYPRCONF"; then
        echo "" >> "$HYPRCONF"
        echo "# AsistenteIA - Toggle Handy con Alt+Z" >> "$HYPRCONF"
        echo "$KEYBIND_LINE" >> "$HYPRCONF"
        echo "Keybinding Alt+Z añadido a $HYPRCONF"
    else
        echo "Keybinding Alt+Z ya configurado."
    fi
else
    echo "Advertencia: $HYPRCONF no encontrado. Añadir manualmente:"
    echo "  $KEYBIND_LINE"
fi

# -----------------------------------------------------------------------------
# Finalización
# -----------------------------------------------------------------------------
echo ""
echo "=== Instalación completada ==="
echo ""
echo "Siguientes pasos:"
echo "  1. Recargar Hyprland: hyprctl reload"
echo "  2. Iniciar el asistente: ./scripts/start-assistant.sh"
echo "  3. Presionar Alt+Z para iniciar la escucha"
echo ""
echo "Para instalar como servicio systemd:"
echo "  cp services/asistenteia.service ~/.config/systemd/user/"
echo "  systemctl --user enable --now asistenteia.service"
