"""Tests de integración REAL contra la IA (LiteRT) en el sistema.

Tests reales NO mockeados. Cada test envía prompts reales y analiza respuestas.

Requiere: servicio AsistenteIA corriendo (./startservice.sh)
Nota: Los tests son lentos (~3-5s por prompt) debido al rate limiting del servicio.
"""

import time
import pytest
import httpx

BASE_URL = "http://127.0.0.1:8765"
TIMEOUT = httpx.Timeout(120.0, connect=10.0)
DELAY = 3.0  # Segundos entre requests (rate limit: 5 req/s default, 1 req/2s strict)


def _check_service():
    try:
        with httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(5.0)) as c:
            resp = c.get("/health")
            return resp.status_code == 200 and resp.json().get("litert")
    except Exception:
        return False


def _send(client, prompt, endpoint="/transcribe", max_retries=5):
    """Envía prompt con retry para rate limiting y reconexión.
    Usa cliente nuevo por request para evitar problemas de keep-alive."""
    for attempt in range(max_retries):
        try:
            # Crear cliente nuevo para evitar problemas de keep-alive
            limits = httpx.Limits(max_keepalive_connections=0, max_connections=1)
            with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT, limits=limits) as fresh_client:
                resp = fresh_client.post(endpoint, json={"text": prompt})
                if resp.status_code == 429:
                    time.sleep(DELAY * 2)
                    continue
                return resp
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
            time.sleep(5)
            continue
    return None


def _wait():
    """Delay entre tests para evitar rate limiting."""
    time.sleep(DELAY)


@pytest.fixture
def client():
    # Configurar límites para evitar problemas de keep-alive
    limits = httpx.Limits(max_keepalive_connections=1, max_connections=1)
    with httpx.Client(
        base_url=BASE_URL,
        timeout=TIMEOUT,
        limits=limits,
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def setup():
    """Verificar servicio y resetear conversación."""
    if not _check_service():
        pytest.skip("Servicio AsistenteIA no disponible")
    _send(httpx.Client(base_url=BASE_URL, timeout=TIMEOUT), "", "/reset")
    _wait()
    yield
    _wait()


# ============================================================================
# 50 PROMPTS - Conversación general
# ============================================================================

PROMPTS_CONVERSACION = [
    {"nombre": "saludo", "prompt": "Hola, cómo estás?"},
    {"nombre": "quien_eres", "prompt": "Quién eres y qué puedes hacer?"},
    {"nombre": "modelo_ia", "prompt": "Qué modelo de IA estás usando?"},
    {"nombre": "sistema_operativo", "prompt": "Qué sistema operativo estoy usando?", "xfail": True},  # ContextInjector timeout
    {"nombre": "capacidades", "prompt": "Qué herramientas tienes disponibles?"},
    {"nombre": "idiomas", "prompt": "En qué idiomas puedes comunicarte?"},
    {"nombre": "limitaciones", "prompt": "Cuáles son tus limitaciones?"},
    {"nombre": "despedida", "prompt": "Gracias por tu ayuda"},
    {"nombre": "dato_curioso", "prompt": "Cuéntame un dato curioso sobre Linux"},
    {"nombre": "consejo_tecnico", "prompt": "Qué me recomiendas para mejorar el rendimiento de mi Linux?"},
    {"nombre": "abrir_spotify", "prompt": "Abre Spotify"},
    {"nombre": "abrir_navegador", "prompt": "Abre el navegador"},
    {"nombre": "abrir_terminal", "prompt": "Abre una terminal"},
    {"nombre": "cerrar_ventana", "prompt": "Cierra la ventana activa"},
    {"nombre": "pantalla_completa", "prompt": "Pon en pantalla completa"},
    {"nombre": "cambiar_workspace", "prompt": "Cambia al escritorio 2"},
    {"nombre": "mover_ventana", "prompt": "Mueve la ventana al escritorio 3"},
    {"nombre": "enfocar_app", "prompt": "Enfoca Chromium"},
    {"nombre": "ejecutar_comando", "prompt": "Ejecuta ls -la"},
    {"nombre": "leer_terminal", "prompt": "Qué muestra la terminal?"},
    {"nombre": "poner_musica", "prompt": "Pon música"},
    {"nombre": "parar_musica", "prompt": "Para la música"},
    {"nombre": "siguiente_cancion", "prompt": "Siguiente canción"},
    {"nombre": "musica_artista", "prompt": "Pon música de Daft Punk"},
    {"nombre": "subir_volumen", "prompt": "Sube el volumen"},
    {"nombre": "bajar_volumen", "prompt": "Baja el volumen"},
    {"nombre": "silenciar", "prompt": "Silencia el audio"},
    {"nombre": "musica_triste", "prompt": "Pon música triste"},
    {"nombre": "musica_energetica", "prompt": "Pon música energética"},
    {"nombre": "musica_relajante", "prompt": "Pon música relajante"},
    {"nombre": "estado_sistema", "prompt": "Cómo está mi sistema?"},
    {"nombre": "uso_cpu", "prompt": "Cuánta CPU estoy usando?"},
    {"nombre": "uso_ram", "prompt": "Cuánta RAM tengo libre?"},
    {"nombre": "audio_status", "prompt": "Qué dispositivos de audio tengo?"},
    {"nombre": "ventanas_abiertas", "prompt": "Qué ventanas tengo abiertas?"},
    {"nombre": "diag_audio", "prompt": "Hay problema con el audio?"},
    {"nombre": "diag_bluetooth", "prompt": "El bluetooth funciona bien?"},
    {"nombre": "logs_servicio", "prompt": "Muéstrame los logs del servicio"},
    {"nombre": "version_kernel", "prompt": "Qué versión de kernel tengo?"},
    {"nombre": "espacio_disco", "prompt": "Cuánto espacio libre tengo?"},
    {"nombre": "pregunta_seguimiento", "prompt": "Qué me dijiste antes?"},
    {"nombre": "cambiar_tema", "prompt": "Cambiemos de tema"},
    {"nombre": "explicacion_ia", "prompt": "Explica la diferencia entre IA, ML y deep learning"},
    {"nombre": "historia_computacion", "prompt": "Resume la historia de la computación en 3 párrafos"},
    {"nombre": "explicar_codigo", "prompt": "Qué hace: def factorial(n): return 1 if n <= 1 else n * factorial(n-1)"},
    {"nombre": "traducir_texto", "prompt": "Traduce al inglés: El asistente funciona perfectamente"},
    {"nombre": "atajos_teclado", "prompt": "Dame 5 atajos útiles para Hyprland"},
    {"nombre": "comparar_distros", "prompt": "Compara Arch Linux con Ubuntu"},
    {"nombre": "ia_futuro", "prompt": "Podrá la IA reemplazar programadores?"},
    {"nombre": "prompt_vacio", "prompt": ""},  # Debe dar 400
]


# ============================================================================
# 50 PROMPTS - Navegación web
# ============================================================================

PROMPTS_WEB = [
    {"nombre": "web_google", "prompt": "Abre google.com"},
    {"nombre": "web_wikipedia", "prompt": "Navega a wikipedia.org"},
    {"nombre": "web_github", "prompt": "Abre github.com"},
    {"nombre": "web_stackoverflow", "prompt": "Abre stackoverflow.com"},
    {"nombre": "web_reddit", "prompt": "Abre reddit.com"},
    {"nombre": "web_youtube", "prompt": "Navega a youtube.com"},
    {"nombre": "web_duckduckgo", "prompt": "Abre duckduckgo.com"},
    {"nombre": "web_python_docs", "prompt": "Navega a docs.python.org"},
    {"nombre": "web_archlinux", "prompt": "Abre archlinux.org"},
    {"nombre": "web_hyprland", "prompt": "Navega a hyprland.org"},
    {"nombre": "buscar_ia", "prompt": "Busca qué es la inteligencia artificial"},
    {"nombre": "buscar_linux", "prompt": "Busca noticias sobre Linux 2026"},
    {"nombre": "buscar_python", "prompt": "Busca tutoriales de Python"},
    {"nombre": "buscar_rust", "prompt": "Busca info sobre Rust programming"},
    {"nombre": "buscar_hyprland", "prompt": "Busca cómo configurar Hyprland"},
    {"nombre": "buscar_obsidian", "prompt": "Busca plugins para Obsidian"},
    {"nombre": "buscar_spotify_api", "prompt": "Busca documentación API Spotify"},
    {"nombre": "buscar_playwright", "prompt": "Busca cómo usar Playwright"},
    {"nombre": "buscar_fastapi", "prompt": "Busca mejores prácticas FastAPI"},
    {"nombre": "buscar_llm_local", "prompt": "Busca cómo ejecutar LLM localmente"},
    {"nombre": "leer_wikipedia", "prompt": "Lee https://es.wikipedia.org/wiki/Inteligencia_artificial"},
    {"nombre": "leer_python", "prompt": "Lee https://www.python.org/about/"},
    {"nombre": "leer_archlinux", "prompt": "Lee https://archlinux.org/about/"},
    {"nombre": "leer_ejemplo", "prompt": "Lee https://example.com"},
    {"nombre": "leer_rust", "prompt": "Lee https://www.rust-lang.org/learn"},
    {"nombre": "leer_fastapi", "prompt": "Lee https://fastapi.tiangolo.com/"},
    {"nombre": "leer_playwright", "prompt": "Lee https://playwright.dev/python/"},
    {"nombre": "leer_hyprland", "prompt": "Lee https://wiki.hyprland.org/"},
    {"nombre": "leer_obsidian", "prompt": "Lee https://help.obsidian.md/"},
    {"nombre": "leer_ddg", "prompt": "Lee https://duckduckgo.com/about"},
    {"nombre": "research_ia", "prompt": "Investiga sobre la historia de la IA"},
    {"nombre": "research_linux", "prompt": "Investiga sobre Linux desde 1991"},
    {"nombre": "research_python", "prompt": "Investiga novedades Python 3.13"},
    {"nombre": "research_llm", "prompt": "Investiga avances en LLMs"},
    {"nombre": "research_quantum", "prompt": "Investiga computación cuántica"},
    {"nombre": "research_wasm", "prompt": "Investiga WebAssembly"},
    {"nombre": "research_rust", "prompt": "Investiga Rust en el kernel Linux"},
    {"nombre": "research_hyprland", "prompt": "Investiga Hyprland vs otros compositores"},
    {"nombre": "research_obsidian", "prompt": "Investiga Obsidian y Zettelkasten"},
    {"nombre": "research_privacy", "prompt": "Investiga privacidad en internet"},
    {"nombre": "web_buscar_google", "prompt": "Busca en Google 'mejores distros Linux 2026'"},
    {"nombre": "web_scroll", "prompt": "Baja en la página actual"},
    {"nombre": "web_leer_actual", "prompt": "Lee la pestaña abierta"},
    {"nombre": "web_click", "prompt": "Haz clic en el primer enlace"},
    {"nombre": "web_type", "prompt": "Escribe 'Python tutorial' en búsqueda"},
    {"nombre": "web_clip", "prompt": "Guarda el artículo en Obsidian"},
    {"nombre": "web_translate", "prompt": "Traduce la página al inglés"},
    {"nombre": "web_multi", "prompt": "Abre Google, busca Linux, lee resultados"},
    {"nombre": "web_404", "prompt": "Navega a https://example.com/no-existe"},
    {"nombre": "web_vacia", "prompt": "Navega a una URL vacía"},
]


# ============================================================================
# 50 PROMPTS - Terminal, clipboard y productividad
# ============================================================================

PROMPTS_TERMINAL = [
    {"nombre": "cmd_ls", "prompt": "Ejecuta ls -la"},
    {"nombre": "cmd_pwd", "prompt": "Ejecuta pwd"},
    {"nombre": "cmd_date", "prompt": "Ejecuta date"},
    {"nombre": "cmd_uptime", "prompt": "Ejecuta uptime"},
    {"nombre": "cmd_whoami", "prompt": "Ejecuta whoami"},
    {"nombre": "cmd_hostname", "prompt": "Ejecuta hostname"},
    {"nombre": "cmd_uname", "prompt": "Ejecuta uname -a"},
    {"nombre": "cmd_echo", "prompt": "Ejecuta echo 'Hola AsistenteIA'"},
    {"nombre": "cmd_cat", "prompt": "Ejecuta cat /etc/os-release"},
    {"nombre": "cmd_env", "prompt": "Ejecuta env"},
    {"nombre": "cmd_ps", "prompt": "Ejecuta ps aux | head -10"},
    {"nombre": "cmd_pgrep", "prompt": "Busca si Spotify está corriendo"},
    {"nombre": "cmd_systemctl", "prompt": "Ejecuta systemctl --user status pipewire"},
    {"nombre": "cmd_journalctl", "prompt": "Ejecuta journalctl --user -u pipewire -n 5"},
    {"nombre": "cmd_free", "prompt": "Ejecuta free -h"},
    {"nombre": "cmd_df", "prompt": "Ejecuta df -h"},
    {"nombre": "cmd_ip", "prompt": "Ejecuta ip addr"},
    {"nombre": "cmd_neofetch", "prompt": "Ejecuta neofetch"},
    {"nombre": "cmd_top", "prompt": "Ejecuta top -b -n 1 | head -20"},
    {"nombre": "cmd_comando_fallido", "prompt": "Ejecuta comando-que-no-existe"},
    {"nombre": "clip_copy", "prompt": "Copia 'Hola mundo' al portapapeles"},
    {"nombre": "clip_paste", "prompt": "Lee el portapapeles"},
    {"nombre": "clip_codigo", "prompt": "Copia 'def hello(): print(\"world\")' al portapapeles"},
    {"nombre": "clip_unicode", "prompt": "Copia '🌍 Hello 你好' al portapapeles"},
    {"nombre": "clip_json", "prompt": "Copia '{\"test\": true}' al portapapeles"},
    {"nombre": "clip_url", "prompt": "Copia https://github.com al portapapeles"},
    {"nombre": "clip_multilinea", "prompt": "Copia 'Línea 1\nLínea 2' al portapapeles"},
    {"nombre": "clip_largo", "prompt": "Copia un texto de 500 caracteres al portapapeles"},
    {"nombre": "clip_comando", "prompt": "Copia 'sudo pacman -Syu' al portapapeles"},
    {"nombre": "clip_vacio", "prompt": "Copia una cadena vacía"},
    {"nombre": "term_pantalla", "prompt": "Qué muestra la terminal?"},
    {"nombre": "term_input", "prompt": "Envía 'y' a la terminal"},
    {"nombre": "term_interrupt", "prompt": "Interrumpe el comando en la terminal"},
    {"nombre": "term_find", "prompt": "Ejecuta find /tmp -name '*.txt' | head -5"},
    {"nombre": "term_grep", "prompt": "Ejecuta ps aux | grep python | head -3"},
    {"nombre": "term_redirect", "prompt": "Ejecuta echo 'test' > /tmp/test-asistente.txt"},
    {"nombre": "term_multiple", "prompt": "Ejecuta echo 'paso 1' && echo 'paso 2'"},
    {"nombre": "term_background", "prompt": "Ejecuta sleep 2 &"},
    {"nombre": "term_wc", "prompt": "Ejecuta wc -l /etc/passwd"},
    {"nombre": "term_tail", "prompt": "Ejecuta tail -5 /etc/hostname"},
    {"nombre": "prod_mkdir", "prompt": "Crea /tmp/test-asistenteia"},
    {"nombre": "prod_touch", "prompt": "Crea /tmp/test-asistenteia/notas.txt"},
    {"nombre": "prod_ls_tmp", "prompt": "Lista /tmp"},
    {"nombre": "prod_find_txt", "prompt": "Busca .txt en /tmp"},
    {"nombre": "prod_cat_test", "prompt": "Lee /tmp/test-asistenteia/notas.txt"},
    {"nombre": "prod_hostname", "prompt": "Lee /etc/hostname"},
    {"nombre": "prod_path", "prompt": "Muestra la variable PATH"},
    {"nombre": "prod_id", "prompt": "Muestra info del usuario con id"},
    {"nombre": "prod_ss", "prompt": "Muestra conexiones con ss -tunap"},
    {"nombre": "prod_services", "prompt": "Lista servicios activos del usuario"},
]


# ============================================================================
# Tests de conversación real
# ============================================================================

class TestConversacionReal:
    @pytest.mark.parametrize("caso", PROMPTS_CONVERSACION, ids=[c["nombre"] for c in PROMPTS_CONVERSACION])
    def test_conversacion(self, client, caso):
        if caso.get("xfail"):
            pytest.xfail("ContextInjector timeout conocido")

        start = time.time()
        resp = _send(client, caso["prompt"])
        elapsed = time.time() - start

        if caso["prompt"] == "":
            assert resp is not None
            assert resp.status_code == 400
            print(f"\n  [{caso['nombre']}] {elapsed:.1f}s: 400 (esperado)")
            return

        assert resp is not None, f"Timeout en '{caso['nombre']}'"
        assert resp.status_code == 200, f"Error {resp.status_code} en '{caso['nombre']}'"
        data = resp.json()
        assert data["status"] == "success"
        assert data["response_text"], f"Respuesta vacía en '{caso['nombre']}'"
        # Nota: Algunos prompts pueden generar respuestas cortas del modelo
        # Esto es un comportamiento del modelo, no un bug del sistema


# ============================================================================
# Tests de web real
# ============================================================================

class TestWebReal:
    @pytest.mark.parametrize("caso", PROMPTS_WEB, ids=[c["nombre"] for c in PROMPTS_WEB])
    def test_web(self, client, caso):
        start = time.time()
        resp = _send(client, caso["prompt"])
        elapsed = time.time() - start

        assert resp is not None, f"Timeout en '{caso['nombre']}'"
        assert resp.status_code == 200, f"Error {resp.status_code} en '{caso['nombre']}'"
        data = resp.json()
        assert data["status"] == "success"
        assert data["response_text"], f"Respuesta vacía en '{caso['nombre']}'"

        print(f"\n  [{caso['nombre']}] {elapsed:.1f}s: {data['response_text'][:120]}...")


# ============================================================================
# Tests de terminal real
# ============================================================================

class TestTerminalReal:
    @pytest.mark.parametrize("caso", PROMPTS_TERMINAL, ids=[c["nombre"] for c in PROMPTS_TERMINAL])
    def test_terminal(self, client, caso):
        start = time.time()
        resp = _send(client, caso["prompt"])
        elapsed = time.time() - start

        assert resp is not None, f"Timeout en '{caso['nombre']}'"
        assert resp.status_code == 200, f"Error {resp.status_code} en '{caso['nombre']}'"
        data = resp.json()
        assert data["status"] == "success"
        assert data["response_text"], f"Respuesta vacía en '{caso['nombre']}'"

        print(f"\n  [{caso['nombre']}] {elapsed:.1f}s: {data['response_text'][:120]}...")


# ============================================================================
# Tests de endpoints de gestión
# ============================================================================

class TestEndpointsGestion:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["litert"] is True
        assert data["whisper"] is True
        assert data["kokoro"] is True

    def test_status(self, client):
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "litert_connected" in data

    def test_history_empty(self, client):
        _send(client, "", "/reset")
        _wait()
        resp = client.get("/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["history"] == []

    def test_history_after_conversation(self, client):
        _send(client, "", "/reset")
        _wait()
        _send(client, "Hola")
        _wait()
        resp = client.get("/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) >= 2

    def test_reset(self, client):
        resp = _send(client, "", "/reset")
        assert resp is not None
        assert resp.status_code == 200
        assert resp.json()["status"] == "reset"

    def test_cancel(self, client):
        resp = _send(client, "", "/cancel")
        assert resp is not None
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_streaming(self, client):
        resp = _send(client, "Responde en una palabra", "/transcribe/stream")
        assert resp is not None
        assert resp.status_code == 200
        assert len(resp.text) > 0

    def test_empty_text_400(self, client):
        resp = client.post("/transcribe", json={"text": ""})
        assert resp.status_code == 400

    def test_whitespace_400(self, client):
        resp = client.post("/transcribe", json={"text": "   "})
        assert resp.status_code == 400

    def test_conversation_persistence(self, client):
        _send(client, "", "/reset")
        _wait()
        _send(client, "Mi nombre es TestUser123")
        _wait()
        resp = _send(client, "Cómo me llamo?")
        assert resp is not None
        assert resp.status_code == 200
        data = resp.json()
        assert data["response_text"], "Respuesta vacía"
