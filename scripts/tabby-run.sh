#!/usr/bin/env bash
# =============================================================================
# tabby-run.sh - Lanza TabbyAPI en FOREGROUND para que lo supervise systemd
# =============================================================================
# Lo ejecuta la unit companion `asistenteia-tabby.service` (ExecStart). Al correr
# en primer plano, systemd es el supervisor: en `stop` mata el cgroup entero
# (padre + hijo que retiene la VRAM), sin PID files ni huérfanos.
#
# Solo arranca si el motor activo usa el sidecar (exllama o hermes); si no, sale 0
# de modo que la unit, aunque se "quiera" junto al asistente, no consume VRAM.
# Resuelve EXLLAMA_TABBY_DIR vía _common.sh (respeta el .env).
# =============================================================================

set -uo pipefail

# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if ! ai_needs_tabby; then
    echo "AI_ENGINE sin sidecar (ni exllama ni hermes): no se arranca TabbyAPI." >&2
    exit 0
fi

if ! ai_tabby_installed; then
    echo "Backend ExLlama no instalado en $EXLLAMA_TABBY_DIR (asistenteia engine install)." >&2
    exit 1
fi

cd "$EXLLAMA_TABBY_DIR" || exit 1
exec "$EXLLAMA_TABBY_DIR/venv/bin/python" "$EXLLAMA_TABBY_DIR/main.py"
