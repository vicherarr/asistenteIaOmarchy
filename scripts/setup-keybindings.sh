#!/usr/bin/env bash
# =============================================================================
# setup-keybindings.sh - Configura los atajos Super+Z / Super+X
# =============================================================================
# Soporta tres estilos de configuración de Hyprland:
#   1. Omarchy con config Lua  -> ~/.config/hypr/bindings.lua   (o.bind ...)
#   2. Omarchy nativo          -> ~/.config/hypr/bindings.conf  (bind = ...)
#   3. Hyprland puro           -> ~/.config/hypr/hyprland.conf  (bind = ...)
#
# Limpia binds antiguos del asistente en todos los archivos y escribe los
# nuevos apuntando a la ruta de instalación real (sin rutas hardcodeadas).
#
# Uso: setup-keybindings.sh [INSTALL_DIR]
# =============================================================================

set -uo pipefail

# Ruta de instalación: argumento, o derivada de la ubicación de este script.
INSTALL_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TOGGLE="$INSTALL_DIR/scripts/handy-toggle.sh"
STOP="$INSTALL_DIR/scripts/stop-assistant.sh"

HYPR_DIR="$HOME/.config/hypr"
LUA="$HYPR_DIR/bindings.lua"
CONF="$HYPR_DIR/bindings.conf"
HCONF="$HYPR_DIR/hyprland.conf"

log()  { printf '[*] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }

# Elimina TODO lo nuestro de un archivo de config: binds antiguos, las
# windowrules de la GUI vieja centrada (asistenteia-gui) y la layer rule del
# overlay (lua o hyprlang). Idempotente: permite reescribir sin duplicar.
strip_ai() {
    local f="$1"
    [ -f "$f" ] || return 0
    grep -vE 'AsistenteIA|handy-toggle\.sh|stop-assistant\.sh|asistenteia-gui|namespace = "asistenteia"|layerrule.*asistenteia' "$f" > "$f.aitmp" 2>/dev/null || true
    if [ -s "$f.aitmp" ]; then mv "$f.aitmp" "$f"; else rm -f "$f.aitmp"; fi
}

# Elegir el archivo destino según el estilo de configuración presente.
TARGET=""; MODE=""
if [ -f "$LUA" ]; then
    TARGET="$LUA"; MODE="lua"
elif [ -f "$CONF" ]; then
    TARGET="$CONF"; MODE="conf"
elif [ -f "$HCONF" ]; then
    TARGET="$HCONF"; MODE="conf"
fi

if [ -z "$TARGET" ]; then
    warn "No se encontró configuración de Hyprland en $HYPR_DIR."
    warn "Añade manualmente estos atajos a tu configuración:"
    warn "  Super + Z -> $TOGGLE"
    warn "  Super + X -> $STOP"
    exit 0
fi

# Copia de seguridad del archivo destino.
cp "$TARGET" "$TARGET.bak.$(date +%s)" 2>/dev/null || true

# Limpiar lo nuestro en todos los archivos posibles (binds, windowrules viejas,
# layer rule del overlay) para reescribir sin duplicar.
strip_ai "$LUA"
strip_ai "$CONF"
strip_ai "$HCONF"
strip_ai "$HYPR_DIR/looknfeel.lua"
strip_ai "$HYPR_DIR/hyprland.lua"

# Escribir los binds nuevos en el destino.
if [ "$MODE" = "lua" ]; then
    {
        printf '\n-- AsistenteIA (gestionado por el instalador)\n'
        printf 'o.bind("SUPER + Z", "AsistenteIA Escuchar", "%s")\n' "$TOGGLE"
        printf 'o.bind("SUPER + X", "AsistenteIA Detener", "%s")\n' "$STOP"
        # Overlay layer-shell: blur para que combine con Omarchy (forma Lua 0.55).
        printf 'hl.layer_rule({ match = { namespace = "asistenteia" }, blur = true, ignore_alpha = 0.3 })\n'
    } >> "$TARGET"
else
    {
        printf '\n# AsistenteIA (gestionado por el instalador)\n'
        printf 'bind = SUPER, Z, exec, %s\n' "$TOGGLE"
        printf 'bind = SUPER, X, exec, %s\n' "$STOP"
        # Overlay layer-shell: blur (forma hyprlang legacy para Hyprland < 0.55).
        printf 'layerrule = blur, asistenteia\n'
        printf 'layerrule = ignorealpha 0.3, asistenteia\n'
    } >> "$TARGET"
fi

log "Atajos escritos en $TARGET ($MODE):"
log "  Super + Z -> arrancar/hablar con el asistente"
log "  Super + X -> detener el asistente"

# Recargar Hyprland (best-effort) y validar.
if command -v hyprctl >/dev/null 2>&1 && [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
    if hyprctl reload >/dev/null 2>&1; then
        log "Hyprland recargado."
        errs="$(hyprctl configerrors 2>/dev/null)"
        if [ -n "$errs" ] && ! printf '%s' "$errs" | grep -qi "no errors"; then
            warn "Hyprland reporta avisos de configuración:"
            printf '%s\n' "$errs" >&2
        fi
    else
        warn "No se pudo recargar Hyprland automáticamente. Ejecuta: hyprctl reload"
    fi
else
    log "Recarga manual pendiente: ejecuta 'hyprctl reload' dentro de tu sesión Hyprland."
fi
