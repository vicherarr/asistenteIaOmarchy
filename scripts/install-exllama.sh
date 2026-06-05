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

# Args: [-y|--yes] [clave-de-modelo]. La clave sale del catálogo (exllama_model_meta);
# por defecto qwen3-8b (texto+tools). Ej: ./install-exllama.sh --yes qwen3-vl
ASSUME_YES=0
MODEL_KEY="qwen3-8b"
for a in "$@"; do
    case "$a" in
        -y|--yes) ASSUME_YES=1 ;;
        -*)       err "Opción desconocida: $a"; exit 1 ;;
        *)        MODEL_KEY="$a" ;;
    esac
done
confirm() { [ "$ASSUME_YES" = 1 ] && return 0; ai_confirm "$1"; }

TABBY_REPO="https://github.com/theroyallab/tabbyAPI.git"
DEST="$EXLLAMA_TABBY_DIR"

# Modelo a instalar (del catálogo). DIRNAME es el model_name de TabbyAPI.
if ! META="$(exllama_model_meta "$MODEL_KEY")"; then
    err "Modelo exllama no válido: '$MODEL_KEY'. Disponibles: $EXLLAMA_MODEL_KEYS"; exit 1
fi
IFS='|' read -r EXL_REPO EXL_REV EXL_NAME EXL_VISION EXL_SEQ EXL_DESC <<<"$META"

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
echo "    Modelo:   $MODEL_KEY ($EXL_DESC)"
echo "              $EXL_REPO@$EXL_REV  ->  models/$EXL_NAME"
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
    if ! confirm "¿Reinstalar/actualizar de todos modos?"; then
        echo "Sin cambios."; exit 0
    fi
fi

echo
warn "Esto descargará varios GB (dependencias ~5-6 GB + modelo ~5 GB)."
if ! confirm "¿Continuar con la instalación?"; then
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

# --- 4. config.yml ----------------------------------------------------------
log "Escribiendo config.yml ($MODEL_KEY, visión: $EXL_VISION)..."
ai_tabby_write_config "$EXL_NAME" "$EXL_SEQ" "$EXL_VISION"

# --- 5. Modelo EXL3 ---------------------------------------------------------
if ai_tabby_model_present "$EXL_NAME"; then
    ok "El modelo ya está en models/$EXL_NAME."
else
    log "Descargando el modelo $EXL_REPO@$EXL_REV..."
    if ! ai_tabby_download_model "$EXL_REPO" "$EXL_REV" "$EXL_NAME"; then
        err "La descarga del modelo falló. Reintenta: asistenteia engine install $MODEL_KEY"
        exit 1
    fi
fi

# --- 6. Dejar el .env coherente con el modelo instalado ---------------------
ai_set_env_key EXLLAMA_MODEL "$EXL_NAME"
[ "$EXL_VISION" = yes ] && ai_set_env_key EXLLAMA_VISION True || ai_set_env_key EXLLAMA_VISION False

echo
ok "Backend ExLlama instalado en $DEST (modelo $MODEL_KEY, visión: $EXL_VISION)."
echo "    Actívalo con:  asistenteia engine exllama"
