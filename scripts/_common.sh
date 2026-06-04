#!/usr/bin/env bash
# =============================================================================
# _common.sh - Funciones y variables compartidas por los scripts de AsistenteIA
# =============================================================================
# Se obtiene con `source` desde los demás scripts. No ejecutar directamente.
# Resuelve la raíz del proyecto sin rutas hardcodeadas y centraliza la lógica
# de servicio/proceso para que todo funcione tanto con systemd como sin él.
# =============================================================================

# Raíz del proyecto: este archivo vive en <PROJECT_DIR>/scripts/
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="asistenteia.service"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
PID_FILE="/tmp/asistenteia.pid"
GUI_PID_FILE="/tmp/asistenteia-gui.pid"
LOG_FILE="/tmp/asistenteia.log"

# Salida con color y helpers de mensajes (compartidos por los scripts).
if [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_B=$'\033[1m'; C_BLUE=$'\033[1;34m'
    C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_CYAN=$'\033[1;36m'
else
    C_RESET=""; C_B=""; C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""
fi
log()  { printf '%s[*]%s %s\n'  "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[!]%s %s\n'  "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[x]%s %s\n'  "$C_RED"    "$C_RESET" "$*" >&2; }

# Lee una clave del .env; devuelve el valor por defecto ($2) si no existe.
ai_read_env() {
    local key="$1" def="${2:-}" val=""
    val=$(grep -E "^${key}=" "$PROJECT_DIR/.env" 2>/dev/null | head -n1 | cut -d '=' -f2- | tr -d '[:space:]') || true
    echo "${val:-$def}"
}

# Fija (o crea) una clave KEY=VALUE en el .env conservando el resto del archivo.
ai_set_env_key() {
    local key="$1" val="$2" f="$PROJECT_DIR/.env"
    if grep -q "^${key}=" "$f" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$f"
    else
        printf '%s=%s\n' "$key" "$val" >> "$f"
    fi
}

# VRAM de la GPU dedicada más grande, en MiB (0 si no hay). Misma lógica que el
# instalador: NVIDIA vía nvidia-smi y AMD vía /sys. Sirve para elegir backend.
detect_vram_mib() {
    local mib=0 v f bytes amd
    if command -v nvidia-smi >/dev/null 2>&1; then
        v="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | sort -nr | head -n1 || true)"
        case "$v" in ''|*[!0-9]*) : ;; *) mib="$v" ;; esac
    fi
    for f in /sys/class/drm/card*/device/mem_info_vram_total; do
        [ -r "$f" ] || continue
        bytes="$(cat "$f" 2>/dev/null || echo 0)"
        case "$bytes" in ''|*[!0-9]*) continue ;; esac
        amd=$(( bytes / 1048576 ))
        [ "$amd" -gt "$mib" ] && mib="$amd"
    done
    echo "$mib"
}

PORT="$(ai_read_env PORT 8765)"
API_TOKEN="$(ai_read_env API_TOKEN "")"

# Protocolo (http/https) según haya un certificado SSL configurado y presente.
_ai_ssl_cert="$(ai_read_env SSL_CERTFILE "")"
if [ -n "$_ai_ssl_cert" ]; then
    case "$_ai_ssl_cert" in
        /*) _ai_ssl_abs="$_ai_ssl_cert" ;;
        *)  _ai_ssl_abs="$PROJECT_DIR/$_ai_ssl_cert" ;;
    esac
    [ -f "$_ai_ssl_abs" ] && PROTO="https" || PROTO="http"
else
    PROTO="http"
fi
BASE_URL="$PROTO://localhost:$PORT"

# ---- Servicio systemd -------------------------------------------------------
ai_service_installed() { [ -f "$SYSTEMD_USER_DIR/$SERVICE_NAME" ]; }
ai_service_active()    { systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null; }

# Propaga el entorno gráfico vivo al gestor systemd --user (necesario para que
# el servicio pueda hablar con Wayland/D-Bus al arrancar bajo demanda).
ai_import_graphical_env() {
    systemctl --user import-environment \
        DISPLAY WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_CURRENT_DESKTOP \
        2>/dev/null || true
}

# Averigua la IP de este equipo en la red local (la que usaría para salir a
# Internet, que es la de la LAN). Vacío si no se puede determinar.
ai_lan_ip() {
    local ip=""
    ip=$(ip -4 route get 1.1.1.1 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
    [ -z "$ip" ] && ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "$ip"
}

# Pregunta sí/no por la terminal. Devuelve 0 (sí) solo ante respuesta afirmativa.
# Sin TTY devuelve 1 (no) para no bloquear ejecuciones no interactivas.
ai_confirm() {
    local prompt="$1" ans=""
    [ -r /dev/tty ] || return 1
    read -r -p "$prompt [s/N]: " ans </dev/tty || return 1
    case "$ans" in [sSyY]*) return 0 ;; *) return 1 ;; esac
}

# Detecta un firewall ACTIVO sin requerir sudo (vía systemctl is-active).
# Imprime el nombre (firewalld|ufw|nftables|iptables) o vacío si no hay ninguno.
ai_active_firewall() {
    local s
    for s in firewalld ufw nftables iptables; do
        systemctl is-active --quiet "$s" 2>/dev/null && { echo "$s"; return 0; }
    done
    echo ""; return 1
}

# Comprueba el firewall y, si está activo, ofrece abrir el puerto TCP $1
# (interactivo, usa sudo). Informa cuando no hay nada que abrir.
ai_firewall_open() {
    local port="$1" fw
    fw="$(ai_active_firewall)"
    if [ -z "$fw" ]; then
        log "No hay firewall gestionado activo: no hace falta abrir el puerto $port/tcp."
        return 0
    fi
    case "$fw" in
        ufw)
            warn "Firewall ufw activo: para el acceso externo debe permitir el puerto $port/tcp."
            if ai_confirm "¿Abrir $port/tcp en ufw ahora (sudo)?"; then
                sudo ufw allow "$port/tcp" && ok "Puerto $port/tcp permitido en ufw." \
                    || err "No se pudo modificar ufw."
            fi
            ;;
        firewalld)
            warn "Firewall firewalld activo: para el acceso externo debe permitir el puerto $port/tcp."
            if ai_confirm "¿Abrir $port/tcp en firewalld ahora (sudo, runtime + permanente)?"; then
                if sudo firewall-cmd --add-port="$port/tcp" >/dev/null \
                   && sudo firewall-cmd --permanent --add-port="$port/tcp" >/dev/null; then
                    ok "Puerto $port/tcp permitido en firewalld."
                else
                    err "No se pudo modificar firewalld."
                fi
            fi
            ;;
        nftables|iptables)
            warn "Firewall $fw activo (sin gestor amistoso para automatizarlo)."
            warn "Si la conexión externa falla, abre manualmente el puerto $port/tcp en $fw."
            ;;
    esac
}

# ---- Estado del servidor ----------------------------------------------------
ai_server_up() { curl -sk -o /dev/null --max-time 2 "$BASE_URL/status" 2>/dev/null; }

# Espera hasta que el servidor responda. $1 = segundos de timeout (def. 60).
ai_wait_server() {
    local timeout="${1:-60}" count=0
    until ai_server_up; do
        sleep 1
        count=$((count + 1))
        [ "$count" -ge "$timeout" ] && return 1
    done
    return 0
}

# ---- Arranque/parada del proceso (modo sin servicio) ------------------------
ai_start_direct() {
    cd "$PROJECT_DIR" || return 1
    nohup "$PROJECT_DIR/venv/bin/python" -m src.main >"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
}

ai_stop_process() {
    if [ -f "$PID_FILE" ]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    pkill -f "python -m src.main" 2>/dev/null || true
    fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
}

ai_stop_gui() {
    if [ -f "$GUI_PID_FILE" ]; then
        kill "$(cat "$GUI_PID_FILE")" 2>/dev/null || true
        rm -f "$GUI_PID_FILE"
    fi
}

# Arranca el asistente por la vía adecuada (servicio si está instalado, si no
# proceso directo) y espera a que responda. Devuelve 0 si ya estaba arriba.
# Imprime "started" o "already" en stdout para que el llamador lo distinga.
ai_ensure_running() {
    if ai_server_up; then
        echo "already"
        return 0
    fi
    if ai_service_installed; then
        ai_import_graphical_env
        systemctl --user start "$SERVICE_NAME"
    else
        ai_start_direct
    fi
    ai_wait_server "${1:-60}" && echo "started" || { echo "error"; return 1; }
}
