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

# ---- Motor de inferencia y sidecar TabbyAPI (ExLlama) -----------------------
# El motor se elige con AI_ENGINE (litert por defecto, en proceso). Cuando es
# "exllama", el LLM corre en un sidecar TabbyAPI (proceso aparte, su propio venv)
# y la app le habla por HTTP. Aquí va su ciclo de vida (arranque/parada), de modo
# que TabbyAPI se levante junto al asistente —como LiteRT se carga solo— y se pare
# con él. Mutua exclusión: solo un motor activo a la vez.
TABBY_PID_FILE="/tmp/asistenteia-tabby.pid"
TABBY_LOG_FILE="/tmp/asistenteia-tabby.log"

EXLLAMA_BASE_URL="$(ai_read_env EXLLAMA_BASE_URL http://127.0.0.1:5000)"
EXLLAMA_API_KEY="$(ai_read_env EXLLAMA_API_KEY "")"
EXLLAMA_AUTOSTART="$(ai_read_env EXLLAMA_AUTOSTART true)"
EXLLAMA_TABBY_DIR="$(ai_read_env EXLLAMA_TABBY_DIR "$PROJECT_DIR/exllama/tabbyAPI")"

ai_engine()          { ai_read_env AI_ENGINE litert; }
ai_using_exllama()   { [ "$(ai_engine)" = "exllama" ]; }
ai_using_openrouter(){ [ "$(ai_engine)" = "openrouter" ]; }
ai_using_hermes()    { [ "$(ai_engine)" = "hermes" ]; }
# Motores que sirven su LLM desde el sidecar TabbyAPI. Hermes solo si corre en local;
# si Hermes usa OpenRouter en la nube, TabbyAPI no hace falta y no consume VRAM.
ai_needs_tabby()     { ai_using_exllama || (ai_using_hermes && ! ai_hermes_use_openrouter); }

# ¿Usa Hermes un modelo en la nube vía OpenRouter?
ai_hermes_use_openrouter() {
    case "$(printf '%s' "$(ai_read_env HERMES_USE_OPENROUTER False)" | tr 'A-Z' 'a-z')" in
        1|true|yes|on|si|sí) return 0 ;;
        *)                   return 1 ;;
    esac
}

# Modelo activo de Hermes (OpenRouter o Local)
HERMES_OPENROUTER_MODEL_DEFAULT="meta-llama/llama-3.3-70b-instruct"
ai_hermes_model() {
    if ai_hermes_use_openrouter; then
        ai_read_env HERMES_OPENROUTER_MODEL "$HERMES_OPENROUTER_MODEL_DEFAULT"
    else
        ai_read_env HERMES_MODEL "Qwen3.5-9B-exl3-3.00bpw"
    fi
}

# Helper para consultar catálogo de modelos de Hermes
ai_hermes_models() {
    "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/scripts/hermes-models.py" "$@"
}

# Sincroniza ~/.hermes/config.yaml con el modelo activo para cuando se use Hermes desde la terminal
ai_hermes_sync_config() {
    local hermes_home="${HERMES_HOME:-$HOME/.hermes}"
    local cfg="$hermes_home/config.yaml"
    local mcp_url; mcp_url="$(ai_luka_mcp_url)"
    local ssl_ver; ssl_ver="$(ai_luka_mcp_ssl_verify)"
    mkdir -p "$hermes_home"
    local model base_url ctx key_line=""
    if ai_hermes_use_openrouter; then
        model="$(ai_hermes_model)"
        base_url="$(ai_read_env OPENROUTER_BASE_URL "https://openrouter.ai/api/v1")"
        ctx=131072
        local k; k="$(ai_openrouter_key)"
        [ -n "$k" ] && key_line="  api_key: \"$k\""
    else
        model="$HERMES_MODEL"
        base_url="$EXLLAMA_BASE_URL/v1"
        ctx="$HERMES_CTX"
        [ -n "$EXLLAMA_API_KEY" ] && key_line="  api_key: \"$EXLLAMA_API_KEY\""
    fi

    cat > "$cfg" <<YML
# Generado por asistenteia (ai_hermes_sync_config). Config de Hermes para AsistenteIA.
model:
  default: "$model"
  provider: "custom"
  base_url: "$base_url"
  context_length: $ctx
  api_mode: chat_completions
$key_line

mcp_servers:
  luka:
    url: "$mcp_url"
    ssl_verify: $ssl_ver
    timeout: 120
    connect_timeout: 10
YML
}

# Configuración del sidecar CUANDO el motor es hermes y corre en local. Es distinta de
# la de exllama a propósito: Hermes pide 64k de contexto mínimo y su system prompt es
# enorme, así que necesita una cuantización más baja y caché Q4.
HERMES_MODEL="$(ai_read_env HERMES_MODEL Qwen3.5-9B-exl3-3.00bpw)"
HERMES_CTX="$(ai_read_env HERMES_CTX 65536)"
HERMES_CACHE_MODE="$(ai_read_env HERMES_CACHE_MODE Q4)"

# Carpeta de la instalación de Hermes (clon + su propio venv). El ~ se expande aquí
# porque el .env guarda la ruta tal cual la escribe el usuario.
ai_hermes_dir() {
    local d; d="$(ai_read_env HERMES_DIR "$HOME/.asistenteia/hermes")"
    printf '%s' "${d/#\~/$HOME}"
}

# Hermes está instalado si existe el intérprete de su venv y el módulo del agente.
ai_hermes_installed() {
    local d; d="$(ai_hermes_dir)"
    [ -x "$d/.venv/bin/python" ] && [ -f "$d/run_agent.py" ]
}

# ¿Está activado el TTS propio de Hermes (text_to_speech)?
ai_hermes_tts_enabled() {
    case "$(printf '%s' "$(ai_read_env HERMES_TTS False)" | tr 'A-Z' 'a-z')" in
        1|true|yes|on|si|sí) return 0 ;;
        *)                   return 1 ;;
    esac
}

# URL del servidor MCP que expone las tools de Luka (src/mcp_server.py). Se sirve desde
# el propio proceso del asistente, así que sale de HOST/PORT del .env. Dos detalles que
# hay que respetar o Hermes no conecta:
#   - El esquema sale de $PROTO, no fijo: con SSL_CERTFILE presente la app sirve HTTPS
#     y solo HTTPS, así que un http:// no llega a ninguna parte.
#   - La barra final: sin ella el mount responde un 307 y algunos clientes pierden el
#     cuerpo del POST al seguir la redirección.
ai_luka_mcp_url() {
    local host port
    host="$(ai_read_env HOST 127.0.0.1)"
    # 0.0.0.0 es una dirección de escucha, no de destino: para conectarse hay que usar
    # una real, y Hermes corre en esta misma máquina.
    case "$host" in ""|0.0.0.0|::) host="127.0.0.1" ;; esac
    port="$(ai_read_env PORT 8765)"
    printf '%s://%s:%s/mcp/' "$PROTO" "$host" "$port"
}

# ¿Hay que decirle a Hermes que no valide el certificado? El de AsistenteIA es
# autofirmado (lo genera el instalador), así que con HTTPS la verificación falla y el
# servidor MCP queda inalcanzable. Es aceptable porque el destino es esta misma máquina.
ai_luka_mcp_ssl_verify() { [ "$PROTO" = https ] && echo false || echo true; }

# Para TODO lo de Hermes: el puente que pilota Luka y cualquier Hermes suelto que se
# haya abierto en una terminal ('asistenteia hermes' / 'hermes').
#
# El puente normalmente muere solo, porque es hijo del servicio y systemd se lleva el
# cgroup entero. Pero un Hermes de terminal NO: sobrevive al asistente, y sigue con un
# agente cargado que puede ejecutar herramientas. Al parar o al cambiar de motor, eso
# tiene que caer también.
#
# Todo va acotado a NUESTRA instalación (rutas absolutas): si alguien tiene otro Hermes
# en la máquina, no se toca.
ai_hermes_stop() {
    local d; d="$(ai_hermes_dir)"
    # El puente, por su ruta de script (aunque haya quedado huérfano).
    pkill -f "$PROJECT_DIR/scripts/hermes_bridge.py" 2>/dev/null || true
    # Hermes suelto: su ejecutable y su intérprete, ambos dentro de nuestro venv.
    pkill -f "$d/.venv/bin/hermes" 2>/dev/null || true
    pkill -f "$d/.venv/bin/python .*run_agent" 2>/dev/null || true
    return 0
}
ai_tabby_autostart(){ case "$(printf '%s' "$EXLLAMA_AUTOSTART" | tr 'A-Z' 'a-z')" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac; }

# El backend ExLlama está "instalado" si existe su venv y el main.py de TabbyAPI.
ai_tabby_installed() { [ -x "$EXLLAMA_TABBY_DIR/venv/bin/python" ] && [ -f "$EXLLAMA_TABBY_DIR/main.py" ]; }

# ¿Responde TabbyAPI? (endpoint OpenAI /v1/models, con token si lo hay).
ai_tabby_up() {
    if [ -n "$EXLLAMA_API_KEY" ]; then
        curl -sk -o /dev/null --max-time 2 -H "Authorization: Bearer $EXLLAMA_API_KEY" "$EXLLAMA_BASE_URL/v1/models" 2>/dev/null
    else
        curl -sk -o /dev/null --max-time 2 "$EXLLAMA_BASE_URL/v1/models" 2>/dev/null
    fi
}

# Espera a que TabbyAPI responda. $1 = segundos de timeout (def. 180; carga el modelo).
ai_tabby_wait() {
    local timeout="${1:-180}" count=0
    until ai_tabby_up; do
        sleep 1; count=$((count + 1))
        [ "$count" -ge "$timeout" ] && return 1
    done
    return 0
}

# Arranca el sidecar TabbyAPI desde su propio venv y espera a que esté listo.
# Devuelve 0 si ya estaba arriba o si arrancó a tiempo. $1 = timeout (def. 180).
ai_tabby_start() {
    ai_tabby_up && return 0
    if ! ai_tabby_installed; then
        warn "Backend ExLlama no instalado en $EXLLAMA_TABBY_DIR."
        warn "Instálalo con: asistenteia engine install"
        return 1
    fi
    log "Arrancando el sidecar TabbyAPI (ExLlama)..."
    # setsid => el proceso es líder de su grupo (PGID == PID), así matamos a TODA
    # su descendencia de una (TabbyAPI lanza un hijo que es quien retiene la VRAM;
    # matar solo al padre dejaría el modelo cargado). main.py con ruta absoluta para
    # que el pkill de respaldo (por ruta) sea fiable; cwd en el dir (config/modelo).
    ( cd "$EXLLAMA_TABBY_DIR" \
        && setsid nohup "$EXLLAMA_TABBY_DIR/venv/bin/python" "$EXLLAMA_TABBY_DIR/main.py" \
            >"$TABBY_LOG_FILE" 2>&1 </dev/null & echo $! >"$TABBY_PID_FILE" )
    if ai_tabby_wait "${1:-180}"; then
        ok "TabbyAPI listo ($EXLLAMA_BASE_URL)."
        return 0
    fi
    err "TabbyAPI no respondió a tiempo. Revisa: tail -f $TABBY_LOG_FILE"
    return 1
}

# Detiene el sidecar TabbyAPI. Solo mata procesos de NUESTRA instalación
# (PID file + main.py bajo EXLLAMA_TABBY_DIR): no toca otros servicios del puerto.
ai_tabby_stop() {
    if [ -f "$TABBY_PID_FILE" ]; then
        local pid; pid="$(cat "$TABBY_PID_FILE")"
        # Mata el grupo entero (-pid): el padre y el hijo que retiene la VRAM.
        # Si el grupo ya no existe, cae al PID suelto como cortesía.
        kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
        rm -f "$TABBY_PID_FILE"
    fi
    # Red de seguridad: cualquier proceso de NUESTRO main.py (ruta absoluta), por si
    # el grupo no cubrió algún huérfano. Acotado a este dir: no toca otros servicios.
    pkill -f "$EXLLAMA_TABBY_DIR/main.py" 2>/dev/null || true
}

# --------------------------------------------------------------------------
# Motor OpenRouter: LLM en la nube. No hay nada que arrancar ni que descargar —
# solo una key y un id de modelo en el .env—, así que aquí no hay ciclo de vida:
# únicamente lectura de config y el catálogo de modelos gratis.
# --------------------------------------------------------------------------
OPENROUTER_MODEL_DEFAULT="google/gemma-4-31b-it:free"

# Reserva por defecto: la misma que src/config.py, para que la CLI enseñe lo que de
# verdad se va a usar cuando la clave no está escrita en el .env.
OPENROUTER_FALLBACKS_DEFAULT="google/gemma-4-26b-a4b-it:free,nvidia/nemotron-nano-12b-v2-vl:free"

ai_openrouter_key()       { ai_read_env OPENROUTER_API_KEY ""; }
ai_openrouter_model()     { ai_read_env OPENROUTER_MODEL "$OPENROUTER_MODEL_DEFAULT"; }
ai_openrouter_fallbacks() { ai_read_env OPENROUTER_FALLBACK_MODELS "$OPENROUTER_FALLBACKS_DEFAULT"; }

# La key SIEMPRE enmascarada al mostrarla: 'status' se pega en incidencias y se ve en
# capturas de pantalla, y una key completa ahí es una key quemada.
ai_openrouter_key_masked() {
    local k; k="$(ai_openrouter_key)"
    if [ -z "$k" ]; then echo "(sin configurar)"; else printf '%s…%s\n' "${k:0:12}" "${k: -4}"; fi
}

# Catálogo de modelos gratis con tool calling (se consulta en vivo a OpenRouter).
# $@ se pasa tal cual al helper: --list | --check <id>.
ai_openrouter_models() {
    "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/scripts/openrouter-models.py" "$@"
}

# LFM2.5 tiene muchas cuantizaciones (bpw). Elige la MÁS ALTA (más calidad) que
# quepa en la VRAM TOTAL de la tarjeta, con margen para KV-cache/CUDA. Así el equipo
# grande baja una versión mejor y el pequeño una que entre, sin tocar nada. Imprime
# la revisión (p.ej. 2.10bpw). Tabla rev:peso(MiB) de HF, de mayor a menor.
ai_lfm25_pick_rev() {
    local vram pair rev size
    vram="$(detect_vram_mib)"; case "$vram" in ''|*[!0-9]*) vram=0 ;; esac
    for pair in 8.00bpw:8615 6.10bpw:6693 5.10bpw:5715 4.10bpw:4737 \
                3.53bpw:4184 3.10bpw:3759 2.53bpw:3206 2.10bpw:2781; do
        rev="${pair%%:*}"; size="${pair##*:}"
        [ $(( size + 1200 )) -le "$vram" ] && { echo "$rev"; return 0; }
    done
    echo "2.10bpw"   # ni la menor entra con margen: se baja la mínima de todos modos
}

# Catálogo de modelos EXL3 del motor exllama. Devuelve, para la clave dada:
#   "REPO|REVISION|DIRNAME|VISION(yes/no)|MAX_SEQ|DESCRIPCIÓN"
# DIRNAME es el directorio bajo models/ y el model_name que usa TabbyAPI.
# MAX_SEQ = ventana/cache. En 8 GiB: texto 8192; el VL (encoder de visión + pesos
# 3.5bpw) solo cabe a 6144 (8192 da "Insufficient VRAM"; 4096 es poco para el
# system prompt + imagen). 6144 carga (~6.3 GB) y sirve texto+visión.
exllama_model_meta() {
    local _r
    case "$1" in
        qwen3-8b) echo "turboderp/Qwen3-8B-exl3|4.0bpw|Qwen3-8B-exl3-4bpw|no|8192|texto+tools (rápido)" ;;
        qwen3-vl) echo "ArtusDev/Qwen_Qwen3-VL-8B-Instruct-EXL3|3.5bpw_H6|Qwen3-VL-8B-Instruct-3.5bpw|yes|6144|texto+tools+VISIÓN" ;;
        # MoE 8B con solo ~1B activo: ligero y rápido. La bpw se elige según la VRAM
        # del equipo (ai_lfm25_pick_rev): el grande baja más calidad, el de 4 GiB el
        # 2.10bpw (~2.7 GB). Trae plantilla de chat con la etiqueta {% generation %}
        # de HF, que ai_tabby_autofix_template adapta sola al cargarlo (ver abajo).
        lfm2.5)   _r="$(ai_lfm25_pick_rev)"
                  echo "turboderp/LFM2.5-8B-A1B-exl3|$_r|LFM2.5-8B-A1B-exl3-$_r|no|8192|texto+razonamiento (MoE 8B-A1B, $_r según VRAM)" ;;
        *)        return 1 ;;
    esac
}
EXLLAMA_MODEL_KEYS="qwen3-8b qwen3-vl lfm2.5"

# Modo enfocado por modelo: ASSISTANT_DEFAULT_TOOLS para ESE modelo. Vacío = todas
# (normal, retrocompatible). Sintaxis: nombres sueltos = lista blanca; nombres con
# prefijo '-' = lista negra (todas MENOS esas). Solo LFM2.5 se restringe: al ser un
# modelo de SOLO TEXTO, le quitamos las tools de visión y dejamos el resto.
# $1 = dirname/model_name del modelo exllama activo.
ai_model_focus_tools() {
    case "$1" in
        # LFM2.5 (8B-A1B): lista BLANCA estricta de terminal/sistema. Con pocas tools y
        # sin duplicados acierta mucho más; execute_system_command ya abre/usa la terminal
        # persistente (read/send/interrupt operan sobre esa misma sesión tmux).
        *LFM2.5*) echo "execute_system_command,read_terminal_screen,send_input_to_terminal,interrupt_terminal_command,read_log_file,system_diagnostics" ;;
        *)        echo "" ;;
    esac
}

# Recalcula las preferencias por-modelo en el .env según el modelo que se ejecutará
# de verdad: con engine litert manda Gemma (sin enfoque); con exllama, EXLLAMA_MODEL.
# - ASSISTANT_DEFAULT_TOOLS: modo enfocado (solo LFM2.5 se centra en terminal/código).
# - EXLLAMA_THINKING: LFM2.5 NECESITA razonar para emitir tool calls fiables, así que
#   lo activamos al elegirlo (el usuario puede desactivarlo luego con 'engine thinking
#   off', a costa de que las tools dejen de funcionar). No tocamos otros modelos.
# Se llama tras cambiar de motor o de modelo. Idempotente y retrocompatible.
ai_refresh_focus_tools() {
    local focus="" model=""
    if ai_using_exllama; then
        model="$(ai_read_env EXLLAMA_MODEL '')"
        focus="$(ai_model_focus_tools "$model")"
        case "$model" in *LFM2.5*) ai_set_env_key EXLLAMA_THINKING True ;; esac
    fi
    ai_set_env_key ASSISTANT_DEFAULT_TOOLS "$focus"
}

# ¿Está descargado el modelo cuyo DIRNAME es $1? (dir no vacío bajo models/)
ai_tabby_model_present() {
    local d="$EXLLAMA_TABBY_DIR/models/$1"
    [ -d "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ]
}

# Adapta la plantilla de chat de un modelo para TabbyAPI, si hace falta. Algunos
# modelos (p.ej. LFM2.5) traen un chat_template.jinja con la etiqueta {% generation %}
# de HuggingFace (marca tokens del assistant para entrenar); el Jinja de TabbyAPI no
# la compila y deshabilita /v1/chat/completions. Generamos un tabby_template.jinja
# (que TabbyAPI carga ANTES que el del modelo) partiendo de la PROPIA plantilla del
# modelo y quitando solo esas etiquetas. Transparente y por-modelo: es idempotente
# (si ya existe, no hace nada) y no toca los modelos cuya plantilla ya carga bien
# (qwen3 no tiene esa etiqueta, así que ni entran). $1 = dirname bajo models/.
ai_tabby_autofix_template() {
    local dir="$EXLLAMA_TABBY_DIR/models/$1"
    local src="$dir/chat_template.jinja" dst="$dir/tabby_template.jinja"
    [ -f "$src" ] && [ ! -f "$dst" ] || return 0
    grep -qE '\{%-?[[:space:]]*(end)?generation[[:space:]]*-?%\}' "$src" || return 0
    if sed -E 's/\{%-?[[:space:]]*(end)?generation[[:space:]]*-?%\}//g' "$src" > "$dst"; then
        log "Plantilla de chat adaptada para TabbyAPI: models/$1/tabby_template.jinja"
    else
        rm -f "$dst"
    fi
}

# Descarga un modelo EXL3 a models/<dirname>. $1=repo $2=revision $3=dirname.
# Devuelve 0 si quedó presente (ya estaba o se bajó bien). Usa el venv de TabbyAPI.
ai_tabby_download_model() {
    local repo="$1" rev="$2" name="$3"
    if ai_tabby_model_present "$name"; then
        ai_tabby_autofix_template "$name"
        return 0
    fi
    "$EXLLAMA_TABBY_DIR/venv/bin/python" - "$repo" "$rev" "$EXLLAMA_TABBY_DIR/models/$name" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2], local_dir=sys.argv[3])
print("Modelo descargado en:", sys.argv[3])
PY
    ai_tabby_model_present "$name" || return 1
    ai_tabby_autofix_template "$name"
}

# Formato de tool calling que TabbyAPI debe PARSEAR en el servidor, según el modelo.
# Imprime la clave de ALL_TOOLCALL_FORMATS (endpoints/OAI/utils/tools.py) o nada.
#
# Por qué importa: sin esta clave TabbyAPI deja la llamada como TEXTO dentro de
# `content` (p.ej. Qwen3.5 emite <tool_call><function=X><parameter=Y>…) y NO rellena
# `tool_calls`. A ExLlamaEngine le da igual —_parse_function_xml lo rescata— pero un
# cliente estándar como Hermes descarta ese XML y se queda sin herramientas.
# Con la clave puesta, el servidor entrega `tool_calls` estructuradas y ExLlamaEngine
# toma su camino preferente (_collect_structured), que además es más robusto.
#
# Solo se mapean familias con parser real; para el resto se imprime vacío y el
# comportamiento queda EXACTAMENTE como antes (retrocompatible).
ai_tabby_tool_format() {
    case "$1" in
        Qwen3.5-*|Qwen3_5-*)      echo "qwen3_5" ;;
        *Qwen3-Coder*)            echo "qwen3_coder" ;;
        *[Gg]emma-4*|*[Gg]emma4*) echo "gemma4" ;;
        *GLM-4.5*|*GLM-4.6*|*GLM-4.7*) echo "glm4_5" ;;
        *MiniMax-M2*)             echo "minimax_m2" ;;
        *Mistral*|*Devstral*)     echo "mistral" ;;
        *)                        echo "" ;;   # Qwen3-8B, Qwen3-VL, LFM2.5: sin parser
    esac
}

# Escribe el config.yml de TabbyAPI. $1=model_name $2=max_seq_len $3=vision(yes/no)
# $4=cache_mode (Q8 por defecto) $5=reasoning(yes/no, no por defecto). host/port salen de
# EXLLAMA_BASE_URL; sin tokens => disable_auth. El formato de herramientas del servidor se
# fija solo si el modelo tiene parser (ver arriba).
#
# Sobre $5: con `reasoning: true` el servidor parte la respuesta en reasoning_content y
# content. Lo quiere HERMES (su cliente lee reasoning_content y así el razonamiento no
# acaba hablado por el TTS). Luka NO lo quiere: su plantilla mete el <think> de apertura
# en el prompt y ExLlamaEngine.leads_with_reasoning se apoya en eso para MOSTRARTE el
# razonamiento en pantalla. Por eso es un parámetro y no una constante.
ai_tabby_write_config() {
    local model_name="$1" max_seq="${2:-8192}" vision="${3:-no}" cache_mode="${4:-Q8}"
    local reasoning="${5:-no}"
    local hp host port disable_auth vbool tool_fmt rbool
    # Asegura una plantilla de chat compatible si el modelo la necesita (transparente).
    ai_tabby_autofix_template "$model_name"
    hp="${EXLLAMA_BASE_URL#*://}"; hp="${hp%%/*}"
    host="${hp%%:*}"; port="${hp##*:}"
    case "$port" in ''|*[!0-9]*) port=5000 ;; esac
    [ -n "$host" ] || host="127.0.0.1"
    if [ -n "$EXLLAMA_API_KEY" ]; then disable_auth=false; else disable_auth=true; fi
    [ "$vision" = yes ] && vbool=true || vbool=false
    tool_fmt="$(ai_tabby_tool_format "$model_name")"
    [ "$reasoning" = yes ] && rbool=true || rbool=false
    cat > "$EXLLAMA_TABBY_DIR/config.yml" <<YML
# Generado por asistenteia (no editar a mano: se regenera). Config del motor exllama.
network:
  host: $host
  port: $port
  disable_auth: $disable_auth
  disable_request_streaming: false

logging:
  log_prompt: false
  log_generation_params: false
  log_requests: false

model:
  model_dir: models
  model_name: $model_name
  max_seq_len: $max_seq
  cache_size: $max_seq
  # Caché KV cuantizada: a 8 bits es ~la mitad de VRAM que FP16 con pérdida mínima, así
  # cabe MÁS contexto en la misma tarjeta (p.ej. 32k en 8 GiB). Con el motor hermes se
  # baja a Q4 porque hacen falta 64k y no entran de otra forma.
  cache_mode: $cache_mode
  gpu_split_auto: true
  inline_model_loading: false
  vision: $vbool
  tool_format: $tool_fmt
  # Parte la respuesta en reasoning_content + content (solo el perfil de hermes).
  reasoning: $rbool

developer:
  unsafe_launch: false
YML
}

# Ventana de contexto que le toca a un modelo con el motor exllama. Prioridad:
# EXLLAMA_CTX del .env (lo fija 'engine use', respetando un --ctx a medida) > el valor
# del catálogo curado (ya validado para 8 GiB) > 8192.
ai_tabby_model_max_seq() {
    local model="$1" ctx k _r _v cdir cvis cseq _d
    ctx="$(ai_read_env EXLLAMA_CTX '')"
    case "$ctx" in ''|*[!0-9]*) ctx="" ;; esac
    [ -n "$ctx" ] && { echo "$ctx"; return 0; }
    for k in $EXLLAMA_MODEL_KEYS; do
        IFS='|' read -r _r _v cdir cvis cseq _d < <(exllama_model_meta "$k")
        [ "$cdir" = "$model" ] && { echo "$cseq"; return 0; }
    done
    echo 8192
}

# Reescribe el config.yml del sidecar con el perfil del motor ACTIVO. Se llama al
# cambiar de motor, porque exllama y hermes quieren cosas distintas de la misma tarjeta:
#
#   exllama -> EXLLAMA_MODEL, su ventana, Q8   (Luka lleva el bucle de tools)
#   hermes  -> HERMES_MODEL, 64k, Q4           (Hermes lo lleva y necesita contexto)
#
# Con litert/openrouter no hay sidecar y no se toca nada. Devuelve 1 (sin escribir) si
# el modelo que toca no está descargado, para que quien llame pueda avisar.
ai_tabby_sync_config_for_engine() {
    ai_needs_tabby || return 0
    local model seq vis cache reason
    if ai_using_hermes; then
        model="$HERMES_MODEL"; seq="$HERMES_CTX"; cache="$HERMES_CACHE_MODE"; vis=no
        reason=yes   # Hermes lee reasoning_content; si no, el razonamiento acaba en el TTS
    else
        model="$(ai_read_env EXLLAMA_MODEL Qwen3-8B-exl3-4bpw)"
        seq="$(ai_tabby_model_max_seq "$model")"
        case "$(ai_read_env EXLLAMA_VISION False)" in [Tt]rue|1|yes) vis=yes ;; *) vis=no ;; esac
        cache=Q8
        reason=no    # Luka MUESTRA el razonamiento: se apoya en que llegue inline
    fi
    ai_tabby_model_present "$model" || return 1
    ai_tabby_write_config "$model" "$seq" "$vis" "$cache" "$reason"
}

# ---- Modelos EXL3 libres (cualquiera de HuggingFace, no solo el catálogo) ----
# El catálogo (exllama_model_meta) son los modelos "bendecidos" que el asistente
# usa con tools. Pero TabbyAPI es un servidor OpenAI genérico: puedes bajar y
# servir CUALQUIER modelo EXL3 (p.ej. de turboderp, creador de ExLlama) para
# herramientas externas (aider, Continue, Open WebUI...), funcione o no de
# asistente. Estos helpers dan: explorar el repo del autor, ver sus bpw, y listar
# lo ya descargado. La descarga reaprovecha ai_tabby_download_model.

# Python con huggingface_hub para consultar/descargar de HF. Prefiere el venv de
# TabbyAPI (donde viven los modelos); si no, el del propio asistente.
ai_hf_python() {
    if [ -x "$EXLLAMA_TABBY_DIR/venv/bin/python" ]; then
        echo "$EXLLAMA_TABBY_DIR/venv/bin/python"
    else
        echo "$PROJECT_DIR/venv/bin/python"
    fi
}

# Lista los modelos de un autor en HuggingFace, ordenados por descargas.
# $1=autor  $2=filtro (substring, opcional). Imprime "DESCARGAS<TAB>ID" por línea.
ai_hf_author_models() {
    "$(ai_hf_python)" - "$1" "${2:-}" <<'PY'
import sys
try:
    from huggingface_hub import HfApi
except ImportError:
    sys.exit("huggingface_hub no está disponible en el venv.")
author, filt = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "").lower()
try:
    models = HfApi().list_models(author=author, sort="downloads", limit=500)
except Exception as e:
    sys.exit(f"No se pudo consultar HuggingFace: {e}")
for m in models:
    if filt and filt not in m.id.lower():
        continue
    print(f"{getattr(m, 'downloads', 0) or 0}\t{m.id}")
PY
}

# Lista las revisiones (ramas) de un repo HF: en EXL3 son las cuantizaciones bpw.
# $1=repo. Imprime una rama por línea ('main' la primera).
ai_hf_repo_revisions() {
    "$(ai_hf_python)" - "$1" <<'PY'
import sys
from huggingface_hub import HfApi
try:
    refs = HfApi().list_repo_refs(sys.argv[1])
except Exception as e:
    sys.exit(f"No se pudo consultar el repo: {e}")
for b in refs.branches:
    print(b.name)
PY
}

# Lista los modelos ya descargados bajo models/ (dirs con pesos o config.json),
# uno por línea. Sirve tanto para el catálogo como para los traídos con 'pull'.
ai_tabby_list_downloaded() {
    local d
    for d in "$EXLLAMA_TABBY_DIR"/models/*/; do
        [ -d "$d" ] || continue
        d="${d%/}"
        if [ -f "$d/config.json" ] || ls "$d"/*.safetensors >/dev/null 2>&1; then
            basename "$d"
        fi
    done
}

# Tamaño total (MiB) de un repo@revision en HF, SIN descargarlo. $1=repo $2=rev.
# Imprime un entero o nada si no se pudo medir.
ai_hf_repo_size_mib() {
    "$(ai_hf_python)" - "$1" "${2:-main}" <<'PY' 2>/dev/null
import sys
from huggingface_hub import HfApi
try:
    info = HfApi().model_info(sys.argv[1], revision=sys.argv[2], files_metadata=True)
except Exception:
    sys.exit(1)
total = sum((getattr(f, "size", None) or 0) for f in (info.siblings or []))
if total:
    print(int(total / 1024 / 1024))
PY
}

# Lista las ramas (cuantizaciones bpw en EXL3) de un repo con su tamaño total.
# $1=repo. Imprime "RAMA<TAB>MIB" por línea (MIB vacío si no se pudo medir).
ai_hf_repo_revisions_sized() {
    "$(ai_hf_python)" - "$1" <<'PY'
import sys
from huggingface_hub import HfApi
api, repo = HfApi(), sys.argv[1]
try:
    refs = api.list_repo_refs(repo)
except Exception as e:
    sys.exit(f"No se pudo consultar el repo: {e}")
for b in refs.branches:
    mib = ""
    try:
        info = api.model_info(repo, revision=b.name, files_metadata=True)
        total = sum((getattr(f, "size", None) or 0) for f in (info.siblings or []))
        if total:
            mib = str(int(total / 1024 / 1024))
    except Exception:
        pass
    print(f"{b.name}\t{mib}")
PY
}

# Tamaño en disco (MiB) de un modelo ya descargado. $1=dirname bajo models/.
ai_dir_size_mib() {
    local d="$EXLLAMA_TABBY_DIR/models/$1"
    [ -d "$d" ] || { echo 0; return; }
    du -sm "$d" 2>/dev/null | awk '{print $1}'
}

# Veredicto de si un modelo de PESO $1 MiB cabe en la VRAM TOTAL de la tarjeta.
# Mira la CAPACIDAD total de la GPU (no la libre): se asume la VRAM limpia y la
# tarjeta dedicada al LLM. Reserva un margen para la KV-cache + contexto de CUDA.
# Es una ESTIMACIÓN orientativa y NUNCA bloquea: solo informa (texto con color).
ai_vram_verdict() {
    local need_mib="$1" vram overhead total_need g_need g_vram
    vram="$(detect_vram_mib)"
    overhead=1200   # KV-cache + contexto CUDA + activaciones (aprox.)
    case "$vram" in ''|*[!0-9]*) vram=0 ;; esac
    if [ "$vram" -le 0 ]; then
        printf "%sGPU NVIDIA/AMD no detectada%s" "$C_YELLOW" "$C_RESET"; return 0
    fi
    total_need=$(( need_mib + overhead ))
    g_need="$(awk "BEGIN{printf \"%.1f\", $need_mib/1024}")"
    g_vram="$(awk "BEGIN{printf \"%.1f\", $vram/1024}")"
    if [ "$total_need" -le "$vram" ]; then
        printf "%scabe%s (~%s GiB pesos + margen / %s GiB VRAM)" "$C_GREEN" "$C_RESET" "$g_need" "$g_vram"
    elif [ "$need_mib" -le "$vram" ]; then
        printf "%sajustado%s (~%s GiB / %s GiB; poco margen para contexto)" "$C_YELLOW" "$C_RESET" "$g_need" "$g_vram"
    else
        printf "%sNO cabe%s (~%s GiB pesos / %s GiB VRAM)" "$C_RED" "$C_RESET" "$g_need" "$g_vram"
    fi
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
    # Motor exllama: levanta el sidecar TabbyAPI antes que la app. SOLO en modo sin
    # servicio: con systemd lo gestiona la unit companion `asistenteia-tabby.service`
    # (Wants/After), así no se arranca dos veces (colisión de puerto). Todo su stdout
    # va a stderr para no contaminar el "started/already/error" que devolvemos.
    if ai_needs_tabby && ai_tabby_autostart && ! ai_service_installed; then
        ai_tabby_start 180 1>&2 \
            || warn "Arrancando el asistente sin motor listo (ExLlama no disponible)."
    fi
    if ai_service_installed; then
        ai_import_graphical_env
        systemctl --user start "$SERVICE_NAME"
    else
        ai_start_direct
    fi
    ai_wait_server "${1:-60}" && echo "started" || { echo "error"; return 1; }
}
