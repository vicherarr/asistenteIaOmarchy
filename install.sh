#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Instalador para CachyOS con Omarchy / Hyprland
# =============================================================================
# Un solo comando lo instala todo en la carpeta del usuario (~/.asistenteia):
#
#   curl -fsSL https://raw.githubusercontent.com/vicherarr/asistenteIaOmarchy/master/install.sh | bash
#
# También funciona desde un clon local:  ./install.sh
#
# Opciones:
#   --dir <ruta>     Carpeta de instalación (por defecto ~/.asistenteia)
#   --service        Instala el servicio systemd (arranque bajo demanda)
#   --enable-boot    Instala el servicio y lo arranca al iniciar sesión
#   --no-service     No instala servicio (modo bajo demanda con Super + Z)
#   --no-keybind     No toca la configuración de atajos de Hyprland/Omarchy
#   -h, --help       Muestra esta ayuda
#
# Sin flags, pregunta de forma interactiva si instalar el servicio.
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuración / valores por defecto
# -----------------------------------------------------------------------------
INSTALL_DIR="$HOME/.asistenteia"
REPO_URL="${ASISTENTEIA_REPO:-https://github.com/vicherarr/asistenteIaOmarchy.git}"
REPO_BRANCH="${ASISTENTEIA_BRANCH:-master}"
LOCAL_BIN="$HOME/.local/bin"

GEMMA_FILE="gemma-4-E4B-it.litertlm"
GEMMA_REPO="litert-community/gemma-4-E4B-it-litert-lm"

WANT_SERVICE=""        # "", "yes" o "no"
ENABLE_BOOT=false
DO_KEYBIND=true

# -----------------------------------------------------------------------------
# Salida con color
# -----------------------------------------------------------------------------
if [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_B=$'\033[1m'; C_BLUE=$'\033[1;34m'
    C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_CYAN=$'\033[1;36m'
else
    C_RESET=""; C_B=""; C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""
fi
log()  { printf '%s[*]%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
err()  { printf '%s[x]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
step() { printf '\n%s=== %s ===%s\n' "$C_CYAN" "$*" "$C_RESET"; }

usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0" 2>/dev/null; }

# Pregunta sí/no leyendo de la terminal (funciona también con `curl | bash`).
ask_yes_no() {
    local prompt="$1" def="${2:-n}" ans hint
    if [ ! -r /dev/tty ]; then
        [ "$def" = "y" ]; return
    fi
    hint="[s/N]"; [ "$def" = "y" ] && hint="[S/n]"
    read -r -p "$(printf '%s%s%s %s ' "$C_B" "$prompt" "$C_RESET" "$hint")" ans </dev/tty || ans=""
    ans="${ans:-$def}"
    case "$ans" in [sSyY]*) return 0 ;; *) return 1 ;; esac
}

# -----------------------------------------------------------------------------
# Parseo de argumentos
# -----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --dir)         shift; INSTALL_DIR="${1:?--dir requiere una ruta}" ;;
        --service)     WANT_SERVICE=yes ;;
        --enable-boot) WANT_SERVICE=yes; ENABLE_BOOT=true ;;
        --no-service)  WANT_SERVICE=no ;;
        --no-keybind)  DO_KEYBIND=false ;;
        -h|--help)     usage; exit 0 ;;
        *)             warn "Opción desconocida: $1" ;;
    esac
    shift
done
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

printf '%s\n' "${C_CYAN}${C_B}"
cat <<'BANNER'
   _          _     _             _       ___ _
  /_\   _____(_)___| |_ ___ _ _ _| |_ ___|_ _/_\
 / _ \ (_-<_-< (_-<  _/ -_) ' \  _/ -_)| |/ _ \
/_/ \_\/__/__/_/__/\__\___|_||_\__\___|___/_/ \_\
                AsistenteIA - Instalador
BANNER
printf '%s\n' "${C_RESET}"
log "Carpeta de instalación: ${C_B}$INSTALL_DIR${C_RESET}"

# -----------------------------------------------------------------------------
# Comprobaciones previas
# -----------------------------------------------------------------------------
step "1/8  Comprobaciones previas"
if [ "$(id -u)" -eq 0 ]; then
    err "No ejecutes el instalador como root. Hazlo con tu usuario normal."
    exit 1
fi
if ! command -v pacman >/dev/null 2>&1; then
    err "Este instalador es para sistemas basados en Arch (CachyOS). No se encontró pacman."
    exit 1
fi
ok "Sistema basado en Arch detectado."

# -----------------------------------------------------------------------------
# Obtener el código en INSTALL_DIR (clonar o copiar)
# -----------------------------------------------------------------------------
step "2/8  Obteniendo el código en $INSTALL_DIR"
SELF="${BASH_SOURCE[0]:-$0}"
SOURCE_DIR=""
if [ -f "$SELF" ] && [ -f "$(dirname "$SELF")/requirements.txt" ] && [ -f "$(dirname "$SELF")/src/main.py" ]; then
    SOURCE_DIR="$(cd "$(dirname "$SELF")" && pwd)"
fi

if [ -z "$SOURCE_DIR" ]; then
    # Modo remoto (curl | bash): clonar o actualizar el repositorio.
    if ! command -v git >/dev/null 2>&1; then
        log "Instalando git..."
        sudo pacman -S --needed --noconfirm git
    fi
    if [ -d "$INSTALL_DIR/.git" ]; then
        log "Repositorio ya presente, actualizando..."
        git -C "$INSTALL_DIR" pull --ff-only || warn "No se pudo actualizar; se continúa con la versión local."
    elif [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
        err "$INSTALL_DIR ya existe y no es un clon git. Muévelo o usa --dir con otra ruta."
        exit 1
    else
        log "Clonando $REPO_URL (rama $REPO_BRANCH)..."
        git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR"
    fi
elif [ "$(cd "$SOURCE_DIR" && pwd -P)" = "$(mkdir -p "$INSTALL_DIR"; cd "$INSTALL_DIR" && pwd -P)" ]; then
    log "Instalando en el mismo directorio del código fuente (in situ)."
else
    log "Copiando el código desde $SOURCE_DIR ..."
    command -v rsync >/dev/null 2>&1 || sudo pacman -S --needed --noconfirm rsync
    mkdir -p "$INSTALL_DIR"
    rsync -a \
        --exclude 'venv/' \
        --exclude '.git/' \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '.env' \
        --exclude '.chrome-profile/' \
        --exclude 'models/.cache/' \
        --exclude '*.litertlm*' \
        --exclude '*.xnnpack_cache*' \
        "$SOURCE_DIR"/ "$INSTALL_DIR"/
fi
ok "Código disponible en $INSTALL_DIR"
cd "$INSTALL_DIR"

# -----------------------------------------------------------------------------
# Dependencias de sistema
# -----------------------------------------------------------------------------
step "3/8  Dependencias del sistema (pacman)"
sudo pacman -S --needed --noconfirm \
    pipewire wireplumber pipewire-pulse pipewire-alsa \
    bluez bluez-utils playerctl wl-clipboard \
    jq git espeak-ng ffmpeg grim slurp tmux \
    psmisc libnotify rsync openssl curl \
    python python-pip python-virtualenv
ok "Dependencias base instaladas."

# -----------------------------------------------------------------------------
# Localizar un Python compatible (3.12 o 3.11; 3.13+ no sirve para kokoro)
# -----------------------------------------------------------------------------
step "4/8  Localizando Python compatible (3.11 / 3.12)"
PYTHON_BIN=""
for cand in python3.12 python3.11; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON_BIN="$cand"; break; fi
done
if [ -z "$PYTHON_BIN" ]; then
    warn "No se encontró Python 3.11/3.12. Intentando instalarlo desde AUR..."
    AUR_HELPER=""
    for h in yay paru; do command -v "$h" >/dev/null 2>&1 && { AUR_HELPER="$h"; break; }; done
    if [ -n "$AUR_HELPER" ]; then
        "$AUR_HELPER" -S --needed --noconfirm python312 2>/dev/null \
            || "$AUR_HELPER" -S --needed --noconfirm python311 2>/dev/null || true
    fi
    for cand in python3.12 python3.11; do
        if command -v "$cand" >/dev/null 2>&1; then PYTHON_BIN="$cand"; break; fi
    done
fi
if [ -z "$PYTHON_BIN" ]; then
    err "No hay Python 3.11/3.12 disponible. El intérprete del sistema ($(python --version 2>&1)) es demasiado nuevo para las dependencias."
    err "Instala uno con: yay -S python312   (o python311) y vuelve a ejecutar el instalador."
    exit 1
fi
ok "Usando $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))."

# -----------------------------------------------------------------------------
# Entorno virtual + dependencias Python
# -----------------------------------------------------------------------------
step "5/8  Entorno virtual y dependencias Python"
if [ -d venv ] && ! ./venv/bin/python -c 'import sys,os; sys.exit(0 if sys.prefix==os.path.join(os.getcwd(),"venv") else 1)' 2>/dev/null; then
    warn "venv inválido o copiado de otra ruta. Se recrea."
    rm -rf venv
fi
[ -d venv ] || "$PYTHON_BIN" -m venv venv
./venv/bin/pip install --upgrade pip setuptools wheel
./venv/bin/pip install -r requirements.txt
log "Instalando navegador de Playwright (Chromium)..."
./venv/bin/playwright install chromium
ok "Dependencias Python instaladas."

# -----------------------------------------------------------------------------
# Modelos (wake word ya viene en git; Gemma se copia o se descarga)
# -----------------------------------------------------------------------------
step "6/8  Modelo LiteRT Gemma-4-E4B (~3.6 GB)"
mkdir -p "$INSTALL_DIR/models"
TARGET_MODEL="$INSTALL_DIR/models/$GEMMA_FILE"
if [ -f "$TARGET_MODEL" ]; then
    ok "Modelo ya presente."
elif [ -n "$SOURCE_DIR" ] && [ -f "$SOURCE_DIR/models/$GEMMA_FILE" ]; then
    log "Copiando el modelo desde el origen local (sin descarga)..."
    cp "$SOURCE_DIR/models/$GEMMA_FILE" "$TARGET_MODEL"
    ok "Modelo copiado."
else
    log "Descargando el modelo desde HuggingFace..."
    ./venv/bin/python - "$GEMMA_REPO" "$GEMMA_FILE" "$INSTALL_DIR/models" <<'PY'
import sys
try:
    from huggingface_hub import hf_hub_download
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"])
    from huggingface_hub import hf_hub_download
repo, fname, dest = sys.argv[1], sys.argv[2], sys.argv[3]
path = hf_hub_download(repo_id=repo, filename=fname, local_dir=dest)
print("Descargado:", path)
PY
    ok "Modelo descargado."
fi

# -----------------------------------------------------------------------------
# Configuración: .env, token y certificados
# -----------------------------------------------------------------------------
step "7/8  Configuración (.env, token, certificados)"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    TOKEN="$(./venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))')"
    if grep -q '^API_TOKEN=' "$INSTALL_DIR/.env"; then
        sed -i "s|^API_TOKEN=.*|API_TOKEN=$TOKEN|" "$INSTALL_DIR/.env"
    else
        printf 'API_TOKEN=%s\n' "$TOKEN" >> "$INSTALL_DIR/.env"
    fi
    ok ".env creado con un API_TOKEN seguro."
else
    ok ".env ya existe (se respeta)."
fi
if [ ! -f "$INSTALL_DIR/config/certs/cert.pem" ]; then
    bash "$INSTALL_DIR/scripts/generate-certs.sh" >/dev/null 2>&1 && ok "Certificados SSL generados." \
        || warn "No se pudieron generar los certificados (la app usará HTTP)."
else
    ok "Certificados SSL ya presentes."
fi

# -----------------------------------------------------------------------------
# Lanzador `asistenteia` en ~/.local/bin
# -----------------------------------------------------------------------------
step "8/8  Lanzador, servicio y atajos"
mkdir -p "$LOCAL_BIN"
ln -sf "$INSTALL_DIR/scripts/asistenteia" "$LOCAL_BIN/asistenteia"
chmod +x "$INSTALL_DIR/scripts/asistenteia" 2>/dev/null || true
ok "Comando 'asistenteia' instalado en $LOCAL_BIN."
case ":$PATH:" in
    *":$LOCAL_BIN:"*) : ;;
    *) warn "$LOCAL_BIN no está en tu PATH. Añádelo para usar el comando 'asistenteia'."
       warn "  fish:  fish_add_path $LOCAL_BIN"
       warn "  bash:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc" ;;
esac

# -----------------------------------------------------------------------------
# Servicio systemd (según elección)
# -----------------------------------------------------------------------------
if [ -z "$WANT_SERVICE" ]; then
    if ask_yes_no "¿Instalar como servicio systemd (recomendado para uso permanente)?" n; then
        WANT_SERVICE=yes
        ask_yes_no "¿Arrancar automáticamente al iniciar sesión?" n && ENABLE_BOOT=true
    else
        WANT_SERVICE=no
    fi
fi
if [ "$WANT_SERVICE" = yes ]; then
    if [ "$ENABLE_BOOT" = true ]; then
        bash "$INSTALL_DIR/installservice.sh" --enable
    else
        bash "$INSTALL_DIR/installservice.sh"
    fi
else
    log "Modo bajo demanda: no se instala servicio. El asistente arranca con Super + Z."
fi

# -----------------------------------------------------------------------------
# Atajos de teclado (Omarchy / Hyprland)
# -----------------------------------------------------------------------------
if [ "$DO_KEYBIND" = true ]; then
    bash "$INSTALL_DIR/scripts/setup-keybindings.sh" "$INSTALL_DIR" || warn "No se pudieron configurar los atajos automáticamente."
else
    log "Se omite la configuración de atajos (--no-keybind)."
fi

# -----------------------------------------------------------------------------
# Resumen
# -----------------------------------------------------------------------------
step "Instalación completada"
cat <<EOF
${C_GREEN}AsistenteIA está instalado en:${C_RESET} $INSTALL_DIR

  ${C_B}Super + Z${C_RESET}  -> Arrancar / hablar con el asistente
  ${C_B}Super + X${C_RESET}  -> Detener el asistente

Comandos de terminal (si ~/.local/bin está en el PATH):
  asistenteia start | stop | toggle | status | logs | gui | uninstall

EOF
if [ "$DO_KEYBIND" = true ]; then
    echo "Recarga la configuración de Hyprland para activar los atajos: hyprctl reload"
fi
