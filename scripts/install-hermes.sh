#!/usr/bin/env bash
# =============================================================================
# install-hermes.sh - Instala Hermes Agent (motor AI_ENGINE=hermes)
# =============================================================================
# Con este motor el bucle agéntico lo lleva Hermes —con sus propias herramientas—
# y Luka pone la voz, el STT/TTS y el satélite. El LLM sale del MISMO sidecar
# TabbyAPI que usa exllama, pero con otro perfil (64k de contexto y reasoning),
# así que ESTO NO INSTALA NINGÚN MODELO: requiere el backend exllama ya puesto.
#
# Hermes va aislado, con su propio venv, por los mismos motivos que TabbyAPI:
# fija sus dependencias a versión exacta y chocarían con las del asistente
# (litert-lm, PySide6, faster-whisper, kokoro). Tampoco publica wheel: se
# instala clonando el repo y sincronizando con uv.
# =============================================================================

set -uo pipefail

# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

HERMES_REPO="https://github.com/NousResearch/hermes-agent.git"
DEST="$(ai_hermes_dir)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

ASSUME_YES=0
for a in "$@"; do
    case "$a" in
        -y|--yes) ASSUME_YES=1 ;;
        *)        err "Opción desconocida: $a"; exit 1 ;;
    esac
done
confirm() { [ "$ASSUME_YES" = 1 ] && return 0; ai_confirm "$1"; }

# --- 0. Requisitos ----------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    err "Hermes se instala con 'uv' y no está disponible."
    err "Instálalo con:  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# El modelo lo sirve TabbyAPI. Sin ese backend, Hermes no tendría a quién hablarle.
if ! ai_tabby_installed; then
    warn "El backend exllama (TabbyAPI) no está instalado, y Hermes usa su sidecar."
    warn "Instálalo primero con:  asistenteia engine install"
    confirm "¿Instalar Hermes igualmente (no funcionará hasta tener el sidecar)?" || exit 0
fi

echo "Se va a instalar Hermes Agent en: $DEST"
echo "  · repo (clon superficial): ~220 MB     · venv propio: ~120 MB"
echo "  · modelo del perfil hermes: $HERMES_MODEL"
confirm "¿Continuar?" || { echo "Sin cambios."; exit 0; }

# --- 1. Repo ----------------------------------------------------------------
if [ -d "$DEST/.git" ]; then
    log "Hermes ya estaba clonado. Actualizando..."
    git -C "$DEST" pull --ff-only || warn "No se pudo actualizar; sigo con lo existente."
else
    mkdir -p "$(dirname "$DEST")"
    log "Clonando Hermes Agent (clon superficial)..."
    git clone --depth 1 "$HERMES_REPO" "$DEST" || { err "Falló 'git clone'."; exit 1; }
fi

# --- 2. Dependencias --------------------------------------------------------
# --extra mcp NO es opcional para nosotros: es lo que permite a Hermes consumir las
# herramientas de Luka (src/mcp_server.py). Sin él se queda solo con las suyas y
# pierde pantalla, música, correo y la cámara del satélite.
# UV_PYTHON_DOWNLOADS: Hermes pide Python >=3.11,<3.14 y el del sistema puede ser
# más nuevo; que uv se traiga uno compatible en vez de fallar.
log "Instalando dependencias de Hermes (uv sync --extra mcp). Tardará un poco..."
if ! ( cd "$DEST" && UV_PYTHON_DOWNLOADS=automatic uv sync --extra mcp ); then
    err "Falló 'uv sync'. Reintenta:  asistenteia engine hermes install"
    exit 1
fi

if [ ! -x "$DEST/.venv/bin/python" ]; then
    err "uv no dejó un intérprete en $DEST/.venv/bin/python."; exit 1
fi

# --- 3. config.yaml de Hermes ----------------------------------------------
# Solo se declara el servidor MCP. El modelo y el endpoint NO van aquí: se los pasa
# scripts/hermes_bridge.py al construir el agente, para que sigan al .env del
# asistente y no haya dos sitios donde configurar lo mismo.
HERMES_CFG="$HERMES_HOME/config.yaml"
MCP_URL="$(ai_luka_mcp_url)"
if [ -f "$HERMES_CFG" ] && grep -q "mcp_servers:" "$HERMES_CFG" 2>/dev/null; then
    warn "$HERMES_CFG ya tiene 'mcp_servers'; no lo toco."
    warn "Comprueba a mano que 'luka' apunte a: $MCP_URL"
else
    mkdir -p "$HERMES_HOME"
    log "Escribiendo $HERMES_CFG..."
    cat > "$HERMES_CFG" <<YML
# Generado por asistenteia (install-hermes.sh). Config de Hermes para AsistenteIA.
# El modelo y el endpoint NO van aquí: los pasa scripts/hermes_bridge.py desde el .env.
mcp_servers:
  luka:
    # Las herramientas de Luka, servidas por el PROPIO proceso del asistente: es la
    # única forma de que se ejecuten con su estado vivo (audio, satélite, navegador,
    # sesión de tmux). La barra final importa: sin ella el mount responde un 307 y
    # algunos clientes pierden el cuerpo del POST al seguir la redirección.
    url: "$MCP_URL"
    timeout: 120
    connect_timeout: 10
YML
fi

echo
ok "Hermes instalado en $DEST."
echo "    Actívalo con:  asistenteia engine hermes"
if ! ai_tabby_model_present "$HERMES_MODEL"; then
    echo
    warn "Falta el modelo del perfil hermes ($HERMES_MODEL)."
    warn "Descárgalo con:  asistenteia engine pull turboderp/Qwen3.5-9B-exl3 3.00bpw"
fi
