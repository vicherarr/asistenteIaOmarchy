#!/usr/bin/env bash
# =============================================================================
# uninstall.sh - Desinstalador de AsistenteIA
# =============================================================================
# Detiene el asistente, elimina el servicio systemd, el lanzador y los atajos.
# Opcionalmente borra la carpeta de instalación y los modelos.
#
# Uso:
#   ./uninstall.sh           # pregunta antes de borrar la carpeta/modelos
#   ./uninstall.sh --purge   # borra todo sin preguntar (incluye modelos)
#   ./uninstall.sh --keep    # conserva la carpeta de instalación y modelos
# =============================================================================

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="asistenteia.service"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
LOCAL_BIN="$HOME/.local/bin"
HYPR_DIR="$HOME/.config/hypr"

log()  { printf '[*] %s\n' "$*"; }
ok()   { printf '[OK] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }

PURGE=""
case "${1:-}" in
    --purge) PURGE=yes ;;
    --keep)  PURGE=no ;;
esac

echo "=== Desinstalando AsistenteIA ==="

# 1. Detener todo.
log "Deteniendo el asistente..."
if [ -x "$PROJECT_DIR/scripts/stop-assistant.sh" ]; then
    bash "$PROJECT_DIR/scripts/stop-assistant.sh" >/dev/null 2>&1 || true
fi

# 2. Servicio systemd.
if [ -f "$SYSTEMD_USER_DIR/$SERVICE_NAME" ]; then
    log "Eliminando el servicio systemd..."
    systemctl --user disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_USER_DIR/$SERVICE_NAME"
    systemctl --user daemon-reload || true
    ok "Servicio eliminado."
fi

# 3. Lanzador en ~/.local/bin.
if [ -L "$LOCAL_BIN/asistenteia" ] || [ -f "$LOCAL_BIN/asistenteia" ]; then
    rm -f "$LOCAL_BIN/asistenteia"
    ok "Comando 'asistenteia' eliminado."
fi

# 4. Atajos de teclado (limpiar binds del asistente, conservar window rules).
for f in "$HYPR_DIR/bindings.lua" "$HYPR_DIR/bindings.conf" "$HYPR_DIR/hyprland.conf"; do
    [ -f "$f" ] || continue
    if grep -qE 'AsistenteIA|handy-toggle\.sh|stop-assistant\.sh' "$f"; then
        cp "$f" "$f.bak.$(date +%s)" 2>/dev/null || true
        grep -vE 'AsistenteIA|handy-toggle\.sh|stop-assistant\.sh' "$f" > "$f.aitmp" 2>/dev/null || true
        [ -s "$f.aitmp" ] && mv "$f.aitmp" "$f" || rm -f "$f.aitmp"
        ok "Atajos eliminados de $(basename "$f")."
    fi
done
command -v hyprctl >/dev/null 2>&1 && [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] && hyprctl reload >/dev/null 2>&1 || true

# 5. Archivos temporales.
rm -f /tmp/asistenteia.pid /tmp/asistenteia-gui.pid /tmp/asistenteia.log 2>/dev/null || true

# 6. Carpeta de instalación y modelos.
if [ -z "$PURGE" ]; then
    printf '\n¿Borrar también la carpeta de instalación y los modelos (%s)? [s/N] ' "$PROJECT_DIR"
    if [ -r /dev/tty ]; then read -r ans </dev/tty || ans=""; else ans=""; fi
    case "$ans" in [sSyY]*) PURGE=yes ;; *) PURGE=no ;; esac
fi

if [ "$PURGE" = yes ]; then
    log "Eliminando $PROJECT_DIR ..."
    # Borrar la carpeta en segundo plano para no autoeliminar el script en uso.
    ( sleep 1; rm -rf "$PROJECT_DIR" ) >/dev/null 2>&1 &
    ok "Carpeta de instalación marcada para eliminación."
    echo "=== AsistenteIA desinstalado por completo ==="
else
    echo "=== AsistenteIA desinstalado (carpeta y modelos conservados en $PROJECT_DIR) ==="
fi
