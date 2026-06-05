#!/usr/bin/env bash
# =============================================================================
# install-exllama.sh - Instala el backend ExLlama (TabbyAPI + ExLlamaV3)
# =============================================================================
# Igual que LiteRT trae su modelo, esto instala el motor exllama de forma
# AISLADA y opt-in: clona TabbyAPI, crea su PROPIO venv (deps pesadas de
# torch/CUDA fuera del venv principal) y descarga el modelo EXL3. Solo se usa
# con AI_ENGINE=exllama. No toca nada del motor LiteRT (retrocompatible).
#
# Las dependencias son wheels precompilados (torch cu128, exllamav3, flash-attn):
# no requieren toolkit de CUDA ni compilación, igual que validó el spike.
# =============================================================================

set -uo pipefail

# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

TABBY_REPO="https://github.com/theroyallab/tabbyAPI.git"
DEST="$EXLLAMA_TABBY_DIR"

# Modelo EXL3 por defecto (texto + tools). Se baja de HuggingFace a un directorio
# cuyo nombre es el model_name que usa TabbyAPI (= EXLLAMA_MODEL del .env).
EXL_REPO="turboderp/Qwen3-8B-exl3"
EXL_REV="4.0bpw"
EXL_NAME="$(ai_read_env EXLLAMA_MODEL Qwen3-8B-exl3-4bpw)"

# Elige un intérprete Python compatible con los wheels de exllamav3 (cp310-cp313).
pick_python() {
    local p ver
    for p in python3.11 python3.12 python3.10 python3.13 python3; do
        command -v "$p" >/dev/null 2>&1 || continue
        ver="$("$p" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || continue
        case "$ver" in 3.10|3.11|3.12|3.13) echo "$p"; return 0 ;; esac
    done
    return 1
}

# --- 0. Pre-vuelo -----------------------------------------------------------
log "Instalación del backend ExLlama (TabbyAPI + ExLlamaV3)"
echo "    Destino:  $DEST"
echo "    Modelo:   $EXL_REPO@$EXL_REV  ->  models/$EXL_NAME"
echo

if ! command -v git >/dev/null 2>&1; then
    err "Falta 'git'. Instálalo y vuelve a intentarlo."; exit 1
fi

if ! PY="$(pick_python)"; then
    err "No se encontró un Python compatible (3.10-3.13) para crear el venv de TabbyAPI."
    err "Instala python3.11 (recomendado) y reintenta."
    exit 1
fi
log "Usando intérprete: $PY ($("$PY" --version 2>&1))"

# Backend de GPU para el extra de pip (cu12 NVIDIA / amd ROCm).
if command -v nvidia-smi >/dev/null 2>&1; then
    EXTRA="cu12"; log "GPU NVIDIA detectada -> backend CUDA 12.x (cu12)."
elif command -v rocm-smi >/dev/null 2>&1 || command -v amd-smi >/dev/null 2>&1; then
    EXTRA="amd";  log "GPU AMD detectada -> backend ROCm (amd)."
else
    warn "No se detectó GPU NVIDIA ni AMD. ExLlamaV3 necesita GPU; el modelo no cargará."
    warn "Se asumirá cu12, pero probablemente debas instalar drivers/CUDA antes de usarlo."
    EXTRA="cu12"
fi

if ai_tabby_installed; then
    warn "Ya hay una instalación de TabbyAPI en $DEST."
    if ! ai_confirm "¿Reinstalar/actualizar de todos modos?"; then
        echo "Sin cambios."; exit 0
    fi
fi

echo
warn "Esto descargará varios GB (dependencias ~5-6 GB + modelo ~5 GB)."
if ! ai_confirm "¿Continuar con la instalación?"; then
    echo "Cancelado."; exit 0
fi

# Antes de tocar nada, para un sidecar viejo que pudiera estar usando el dir.
ai_tabby_stop

# --- 1. Clonar TabbyAPI -----------------------------------------------------
if [ -d "$DEST/.git" ]; then
    log "TabbyAPI ya clonado; actualizando..."
    git -C "$DEST" pull --ff-only || warn "No se pudo actualizar; continúo con lo existente."
else
    mkdir -p "$(dirname "$DEST")"
    log "Clonando TabbyAPI..."
    git clone --depth 1 "$TABBY_REPO" "$DEST" || { err "Falló 'git clone'."; exit 1; }
fi

# --- 2. venv propio ---------------------------------------------------------
if [ ! -x "$DEST/venv/bin/python" ]; then
    log "Creando el entorno virtual de TabbyAPI..."
    "$PY" -m venv "$DEST/venv" || { err "No se pudo crear el venv."; exit 1; }
fi
VPIP="$DEST/venv/bin/pip"
"$VPIP" install -U pip wheel >/dev/null 2>&1 || true

# --- 3. Dependencias (wheels precompilados) ---------------------------------
log "Instalando dependencias [$EXTRA] (torch cu128 + exllamav3 + flash-attn). Tardará..."
if ! ( cd "$DEST" && "$VPIP" install -U ".[$EXTRA]" ); then
    err "Falló la instalación de dependencias de TabbyAPI."; exit 1
fi

# --- 4. config.yml de producción --------------------------------------------
# host/port salen de EXLLAMA_BASE_URL; sin tool_format (ExLlamaEngine parsea
# <tool_call> del contenido). Sin token => disable_auth (sidecar local).
_hp="${EXLLAMA_BASE_URL#*://}"; _hp="${_hp%%/*}"
THOST="${_hp%%:*}"; TPORT="${_hp##*:}"
case "$TPORT" in ''|*[!0-9]*) TPORT=5000 ;; esac
[ -n "$THOST" ] || THOST="127.0.0.1"
if [ -n "$EXLLAMA_API_KEY" ]; then DISABLE_AUTH=false; else DISABLE_AUTH=true; fi

log "Escribiendo config.yml..."
cat > "$DEST/config.yml" <<EOF
# Generado por asistenteia (install-exllama.sh). Edita .env, no este archivo:
# se regenera al reinstalar. Sin tool_format a propósito (lo parsea ExLlamaEngine).
network:
  host: $THOST
  port: $TPORT
  disable_auth: $DISABLE_AUTH
  disable_request_streaming: false

logging:
  log_prompt: false
  log_generation_params: false
  log_requests: false

model:
  model_dir: models
  model_name: $EXL_NAME
  max_seq_len: 8192
  cache_size: 8192
  gpu_split_auto: true
  inline_model_loading: false

developer:
  unsafe_launch: false
EOF

# --- 5. Modelo EXL3 ---------------------------------------------------------
MODEL_DIR="$DEST/models/$EXL_NAME"
if [ -d "$MODEL_DIR" ] && [ -n "$(ls -A "$MODEL_DIR" 2>/dev/null)" ]; then
    ok "El modelo ya está en models/$EXL_NAME."
else
    log "Descargando el modelo $EXL_REPO@$EXL_REV (~5 GB)..."
    "$DEST/venv/bin/python" - "$EXL_REPO" "$EXL_REV" "$MODEL_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo, rev, dest = sys.argv[1], sys.argv[2], sys.argv[3]
snapshot_download(repo_id=repo, revision=rev, local_dir=dest)
print("Modelo descargado en:", dest)
PY
    if [ ! -d "$MODEL_DIR" ] || [ -z "$(ls -A "$MODEL_DIR" 2>/dev/null)" ]; then
        err "La descarga del modelo falló. Reintenta: asistenteia engine install"
        exit 1
    fi
fi

echo
ok "Backend ExLlama instalado en $DEST."
echo "    Actívalo con:  asistenteia engine exllama"
