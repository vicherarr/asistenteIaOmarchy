# AsistenteIA - Contexto del Proyecto para Sesiones IA

> **Última actualización:** 23 de mayo de 2026 (Fase 7)
> **Propósito:** Este documento sirve como referencia completa para cualquier sesión de IA que trabaje en este proyecto. Contiene toda la arquitectura, decisiones técnicas, estado actual y convenciones del código.

---

## 1. Resumen Ejecutivo

**AsistenteIA** es un asistente de voz inteligente para **Linux (CachyOS/Hyprland)** que funciona **100% en local y offline**. Es una extensión agéntica del sistema operativo que permite interactuar mediante lenguaje natural para:
- Gestionar ventanas (Hyprland)
- Automatizar navegación web (Playwright CDP)
- Controlar multimedia (Spotify, PipeWire)
- Interactuar con terminal persistente (TMUX)
- Diagnosticar el sistema con visión de pantalla
- Investigar web profunda y clipping a Obsidian

---

## 2. Stack Tecnológico

| Componente | Tecnología | Detalle |
|---|---|---|
| **LLM Engine** | LiteRT-LM (Google AI Edge) | Modelo: `gemma-4-E4B-it.litertlm` (~3.6GB) |
| **Backend** | FastAPI + Uvicorn | Puerto: 8765, async/await |
| **STT** | whisper-cli (whisper.cpp) | Modelo: `ggml-small.bin`, beam-size 5 |
| **TTS** | Kokoro-82M (principal) + gTTS (fallback) | Voz: `em_alex` (español), CPU-only |
| **UI** | PySide6 (Spotlight) | Catppuccin colors, animaciones |
| **Audio** | PipeWire/WirePlumber | Bluetooth auto-config, wpctl |
| **Window Manager** | Hyprland | hyprctl para gestión de ventanas |
| **Terminal** | TMUX + Alacritty/Kitty/Foot | Sesión persistente `asistenteia` |
| **Browser** | Chromium + Playwright CDP | Puerto debug 9222, visible |
| **Python** | 3.11 o 3.12 (NO 3.13+) | venv local |

---

## 3. Arquitectura de Archivos

```
asistenteia/
├── src/
│   ├── main.py              # FastAPI app, lifespan, endpoints, AppState
│   ├── litert_client.py     # Motor LiteRT-LM, chat_stream, chat, truncamiento contexto
│   ├── assistant_service.py # Orquestador: STT -> LiteRT -> Tools -> TTS
│   ├── command_executor.py  # Herramientas del sistema (tmux, clipboard, web, etc.)
│   ├── browser/             # Browser automation (CDP + Playwright)
│   │   ├── __init__.py      # Exporta todos los sub-módulos
│   │   ├── launcher.py      # Chromium launch + CDP connection
│   │   ├── navigation.py    # navigate, click, type, read, scroll
│   │   ├── clip.py          # Obsidian clipping
│   │   ├── research.py      # Deep research (hasta 30 pasos)
│   │   └── translate.py     # Page translation con panel overlay
│   ├── tts_engine.py        # Kokoro-82M + gTTS fallback, pipeline doble cola
│   ├── stt_engine.py        # whisper-cli con ffmpeg loudnorm
│   ├── audio_manager.py     # PipeWire/WirePlumber, Bluetooth auto-config
│   ├── audio_recorder.py    # parecord + webrtcvad (auto-stop por silencio)
│   ├── context_injector.py  # Hardware info async paralelo (CPU, GPU, RAM, Hyprland, audio)
│   ├── vision_tool.py       # grim + slurp capturas de pantalla
│   ├── config.py            # Pydantic Settings (.env)
│   ├── schema.py            # ChatMessage (pydantic BaseModel)
│   ├── bt_button_listener.py # Listener async de botones Bluetooth AVRCP (evdev)
│   ├── mpris_dummy_player.py # Dummy MPRIS player para interceptar comandos AVRCP
│   ├── utils.py             # strip_markdown, pending_image (deprecated)
│   └── gui/
│       └── spotlight.py     # UI PySide6 con animaciones
├── config/
│   ├── system_prompt.txt    # Personalidad y reglas del asistente
│   └── omarchy_commands.md  # Referencia comandos Omarchy
├── scripts/
│   ├── handy-toggle.sh      # Alt+Z toggle escucha (inicia servicio + GUI)
│   ├── start-assistant.sh   # Lanzador manual con validación venv
│   ├── start-gui.sh         # Lanza Spotlight UI
│   ├── stop-assistant.sh    # Detiene grabación
│   ├── test-mic.py          # Test micrófono
│   ├── test-bluetooth-buttons.py  # Test interactivo botones AVRCP HOME SPA-133
│   └── test-bluetooth-dbus.py   # Monitoreo D-Bus BlueZ para HOME SPA-133
├── services/
│   └── asistenteia.service  # systemd user service
├── models/
│   └── gemma-4-E4B-it.litertlm  # Modelo LiteRT (descargado de HF)
├── install.sh               # Instalación completa
├── installservice.sh        # Instala servicio systemd
├── startservice.sh          # systemctl --user start
├── stopservice.sh           # systemctl --user stop
├── logs.sh                  # journalctl --user -u asistenteia -f
└── requirements.txt         # 19 dependencias
```

---

## 4. Flujo de Ejecución Completo

### 4.1. Flujo de Voz (Alt+Z / handy-toggle.sh)
```
1. Usuario pulsa Alt+Z → handy-toggle.sh
2. Script verifica servicio systemd activo (si no, lo inicia)
3. Lanza/muestra Spotlight UI (PySide6)
4. POST /listen/toggle → inicia grabación (parecord + webrtcvad)
5. VAD detecta silencio → auto-stop → POST /listen/toggle de nuevo
6. stop_recording() → archivo .wav
7. process_audio() → STT (whisper-cli) → texto transcrito
8. process_transcription() → LiteRT chat_stream con tools
9. TTS worker consume frases → Kokoro streaming → paplay
10. Respuesta hablada + texto en Spotlight
```

### 4.2. Flujo de Texto (Spotlight)
```
1. Usuario escribe en Spotlight → POST /transcribe/stream
2. process_transcription_stream() → LiteRT chat_stream con tools
3. Chunks de texto se yield en tiempo real (SSE-style)
4. Frases se extraen por puntuación → cola TTS → Kokoro
5. Respuesta en Spotlight + audio
```

### 4.3. Tool Calling Nativo (LiteRT)
```
1. LiteRT analiza docstrings y firmas de tipo de las funciones Python
2. El modelo decide llamar herramientas sin parseo JSON manual
3. Funciones async se envuelven con asyncio.run_coroutine_threadsafe
4. Resultados se inyectan en la conversación automáticamente
5. analyze_screen() → set_pending_image() → segunda pasada multimodal
```

---

## 5. Herramientas Registradas (Tool Registry)

Todas están en `src/command_executor.py` y `src/vision_tool.py`:

| Función | Firma | Descripción |
|---|---|---|
| `execute_system_command` | `(command: str)` | Abre apps / ejecuta comandos (redirige a terminal TMUX) |
| `open_terminal_and_run_command` | `(command: str)` | Envía comando a terminal tmux visible |
| `read_terminal_screen` | `()` | Lee últimas 40 líneas de tmux |
| `send_input_to_terminal` | `(input_text: str)` | Envía texto a tmux (ej: contraseñas) |
| `interrupt_terminal_command` | `()` | Envía Ctrl+C a tmux |
| `control_local_browser` | `(action, target, value)` | Playwright CDP: launch, navigate, click, type, read, scroll, clip, research, translate |
| `analyze_screen` | `(region: str)` | grim + slurp → imagen → multimodal |
| `get_system_status` | `()` | CPU, RAM, audio, ventanas |
| `system_diagnostics` | `(component: str)` | Logs audio/bluetooth |
| `read_log_file` | `(service: str)` | journalctl últimos 10 logs |
| `clipboard_manager` | `(action, content)` | wl-copy / wl-paste |
| `web_search` | `(query: str)` | DuckDuckGo search |
| `read_web_page` | `(url: str)` | trafilatura extracción limpia |
| `interact_web` | `(action, target, value)` | Playwright headless para SPAs |
| `play_specific_music` | `(query: str)` | Spotify MPRIS D-Bus |
| `manage_windows` | `(action, target)` | hyprctl: focus, close, fullscreen, workspace, movetoworkspace |

---

## 6. Endpoints FastAPI

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/transcribe` | Texto → respuesta completa (no streaming) |
| POST | `/transcribe/stream` | Texto → streaming de chunks + TTS |
| POST | `/listen/toggle` | Toggle grabación de micrófono |
| POST | `/cancel` | Cancela procesamiento + TTS + grabación |
| GET | `/status` | Estado: LiteRT, BT, processing, speaking, GPU |
| GET | `/health` | Health check: LiteRT, whisper, Kokoro, tmux, CDP |
| GET | `/history` | Historial de conversación |
| POST | `/reset` | Reinicia conversación + procesos |
| POST | `/audio/configure` | Reconfigura audio Bluetooth |

---

## 7. Estado de la Aplicación (AppState)

En `main.py`, la clase `AppState` reemplaza variables globales:
```python
class AppState:
    audio_manager: AudioManager
    litert_client: LiteRTClient
    tts_engine: TTSEngine
    audio_recorder: AudioRecorder
    stt_engine: STTEngine
    assistant_service: AssistantService
    conversation_history: list[ChatMessage]
    current_task: Optional[asyncio.Task]
    processing: bool
    is_recording: bool
```

Se inyecta vía `Depends(get_app_state)` en cada endpoint.

---

## 8. Configuración (.env)

```
HOST=127.0.0.1
PORT=8765
DEBUG=False
LITERT_MODEL_PATH=models/gemma-4-E4B-it.litertlm
LITERT_BACKEND=gpu        # auto | gpu | cpu
MAX_HISTORY=10
KOKORO_VOICE=em_alex      # em_alex, em_santa, ef_dora
KOKORO_LANG=e             # e=español
# Paths (automáticos desde config.py)
OBSIDIAN_VAULT=~/Documentos/Obsidian Vault
OBSIDIAN_CLIPPINGS=~/Documentos/Obsidian Vault/Clippings
```

---

## 9. Decisiones Técnicas Clave

### 9.1. LiteRT sobre Ollama
- **Motivo:** Tool calling nativo sin parseo JSON, multimodal integrado
- **Modelo:** Gemma 4:E4B-it (ventana 4096 tokens)
- **Truncamiento:** Smart trunc con protección de bloques de código
  - System prompt: 2400 chars
  - Mensajes recientes: 6000 chars
  - Historial antiguo: 600 chars/mensaje
  - Prompt actual: 2000 chars

### 9.2. Async/await obligatorio
- Todas las herramientas son `async def`
- `asyncio.create_subprocess_exec` en vez de `subprocess.run`
- `asyncio.to_thread` para operaciones bloqueantes (trafilatura, duckduckgo)
- LiteRT se ejecuta en thread separado con `asyncio.to_thread`

### 9.3. Seguridad de comandos
- Lista blanca de prefijos en `CommandExecutor.ALLOWED_PREFIXES`
- Bloqueo de `;`, `&&`, `||` en comandos
- `shlex.split()` para parseo seguro
- `asyncio.create_subprocess_exec` (no shell=True)

### 9.4. Audio
- **STT:** whisper-cli con ffmpeg loudnorm (I=-16:TP=-1.5:LRA=11), beam-size 5
- **TTS:** Kokoro en CPU (evita colisión VRAM con LiteRT GPU), fallback gTTS
- **VAD:** webrtcvad agresividad 2, 30ms frames, 2s silencio = auto-stop
- **Reproducción:** paplay (PipeWire), sounddevice para Kokoro streaming

### 9.5. Terminal Persistente
- TMUX sesión `asistenteia`
- Terminales soportados: alacritty > kitty > foot
- Comandos se envuelven con banner de éxito/error
- `read_terminal_screen` lee 40 líneas + detecta banners

### 9.6. Browser Automation
- Chromium visible con CDP puerto 9222
- Playwright `connect_over_cdp`
- Research: hasta 30 pasos, Google/DuckDuckGo, trafilatura
- Clip: Obsidian Vault `~/Documentos/Obsidian Vault/Clippings/`
- Translate: inyecta panel overlay con traducción

---

## 10. Problemas Conocidos y Deuda Técnica

### 10.1. RESUELTOS (Mayo 2026)
- [x] **Shell injection:** `read_log_file` migrado de `create_subprocess_shell` a `create_subprocess_exec`
- [x] **ContextInjector sincrónico:** Migrado a async completo con `asyncio.gather()` paralelo (8× más rápido)
- [x] **Funciones tmux síncronas:** 4 funciones migradas a async con helper `_run_tmux_cmd()`
- [x] **TTS gap entre frases:** Pipeline de doble cola implementado (síntesis || reproducción en paralelo)
- [x] **`control_local_browser` monolítico:** Refactorizado en `src/browser/` con 5 sub-módulos (launcher, navigation, clip, research, translate)
- [x] **Estado global imagen:** `pending_image_path` movido a `AppState` (utils.py marcado deprecated)
- [x] **Graceful shutdown:** `lifespan` espera tasks pendientes, cierra TTS, grabación y LiteRT limpiamente
- [x] **Paths hardcodeados:** Unificados bajo `settings.PROJECT_ROOT`, `settings.TEMP_DIR`, `settings.OBSIDIAN_CLIPPINGS`
- [x] **Health endpoint:** `GET /health` verifica LiteRT, whisper-cli, Kokoro, tmux session, CDP port
- [x] **Rate limiting:** Middleware de protección contra spam (5 req/s default, 1 req/2s estricto, 3 req/s streaming)

### 10.2. Restantes
- [ ] **Loop multimodal:** `analyze_screen()` genera imagen pero no hay segunda pasada automática en `assistant_service.py` (pausado — esperando mejora en tool calling multimodal de LiteRT-LM)

---

## 11. Convenciones de Código

- **Imports:** absolutos desde `src.` (ej: `from src.config import settings`)
- **Logging:** `logger = logging.getLogger(__name__)` en cada módulo
- **Nomenclatura:** snake_case para funciones, PascalCase para clases
- **Docstrings:** Google style con Args y Returns
- **Errores:** Custom exceptions (`TTSError`, `CommandExecutorError`, etc.)
- **Tipado:** Type hints obligatorios en firmas de función
- **Async:** Todo lo que haga I/O debe ser async

---

## 12. Comandos de Desarrollo

```bash
# Iniciar servicio
./startservice.sh

# Ver logs en tiempo real
./logs.sh

# Detener servicio
./stopservice.sh

# Lanzar manualmente (debug)
./scripts/start-assistant.sh

# Lanzar GUI Spotlight
./scripts/start-gui.sh

# Instalar desde cero
./install.sh

# Ejecutar tests (98 tests)
./venv/bin/python -m pytest tests/ -v
```

---

## 13. Atajos de Teclado (Hyprland)

| Atajo | Acción | Script |
|---|---|---|
| `Super + Z` | Toggle escucha | `scripts/handy-toggle.sh` |
| `Super + X` | Detener | `scripts/stop-assistant.sh` |
| `Super + Shift + D` | Spotlight UI | `scripts/start-gui.sh` |

---

## 14. System Prompt del Asistente

El archivo `config/system_prompt.txt` define:
- Personalidad: OS Companion autónomo para Linux
- Idioma: Español natural, conciso, optimizado para TTS
- Tool calling: Proactivo, sin pedir permiso
- Protocolos agénticos:
  1. Terminal loop: ejecutar → leer pantalla → corregir
  2. Browser loop: launch → navigate → interactuar → leer
  3. Vision loop: capturar → inyectar imagen → analizar
- Formato de errores: Estilo hacker experto con diagnóstico

---

## 15. Notas para Sesiones Futuras

1. **Antes de hacer cambios:** Leer siempre `improvement_plan.md` e `implementation_plan.md` para contexto de trabajo pendiente
2. **No romper LiteRT:** El tool calling nativo depende de docstrings limpios y firmas de tipo correctas
3. **Mantener async:** Cualquier nueva herramienta debe ser `async def`
4. **Tests:** 101 tests unitarios cubren command_executor, context_injector, assistant_service, tts_engine, audio_manager, main, browser
5. **GPU vs CPU:** LiteRT puede usar GPU, pero Kokoro TTS debe ir en CPU (evitar colisión VRAM)
6. **Contexto limitado:** 4096 tokens = ser conciso en system prompt y respuestas de herramientas
7. **Wayland:** Todo está diseñado para Wayland (grim, slurp, wl-copy, hyprctl)
8. **TTS Pipeline:** Doble cola con `synthesize_only()` + `play_audio_array()` y `sd.OutputStream` persistente
9. **ContextInjector:** 100% async con `asyncio.gather()` para consultas paralelas de hardware
10. **Browser package:** `src/browser/` con 5 sub-módulos (launcher, navigation, clip, research, translate)
11. **Tests:** 102 tests unitarios cubren todos los módulos
12. **Health endpoint:** `GET /health` verifica LiteRT, whisper-cli, Kokoro, tmux, CDP (puerto 9222)
13. **Graceful shutdown:** `lifespan` espera tasks pendientes (3s timeout), cierra TTS stream, grabación y LiteRT
14. **Paths unificados:** `settings.PROJECT_ROOT`, `settings.TEMP_DIR`, `settings.OBSIDIAN_VAULT`, `settings.OBSIDIAN_CLIPPINGS`
15. **Rate limiting:** Middleware con 3 niveles: default (5 req/s), strict (1 req/2s para /listen/toggle, /reset), streaming (3 req/s)
16. **Bluetooth AVRCP evdev (no funciona para trigger):** HOME SPA-133 expone evdev `/dev/input/event20`, pero BlueZ moderno NO genera eventos evdev para AVRCP cuando no hay reproductor activo. Hyprland consume el dispositivo y los eventos se traducen a MPRIS vía `mpris-proxy`.
17. **Dummy MPRIS Player para trigger:** El módulo `src/mpris_dummy_player.py` se registra como reproductor `org.mpris.MediaPlayer2.asistenteia` en el bus de sesión. Cuando se pulsa Play/Pause en el HOME SPA-133, BlueZ → `mpris-proxy` → llama `Play()`/`Pause()` en nuestro dummy player, que invoca `toggle_listen()` / `cancel_processing()`. Requiere `dbus-next>=0.2.3`.
18. **mpris-proxy:** `main.py` inicia automáticamente `mpris-proxy` si no está corriendo. El servicio systemd `--user` tiene acceso al bus de sesión, por lo que todo funciona dentro de la sesión del usuario.
19. **HOME SPA-133: botón Play no envía AVRCP:** Investigación exhaustiva (evdev, BlueZ D-Bus, MPRIS, `dbus-monitor`) confirma que el HOME SPA-133 anuncia perfil AVRCP pero su botón físico de Play NO envía comandos por Bluetooth al host. Probablemente solo funciona en modos USB/Radio/AUX internos del dispositivo. El código está correcto y fue verificado manualmente (`dbus-send Play()` dispara `toggle_listen()` correctamente). Dispositivos que SÍ implementan AVRCP correctamente (ej: JBL Tune 670NC) deberían funcionar.

---

## 16. Historial de Cambios Relevantes

| Fecha | Cambio |
|---|---|
| Mayo 2026 | Migración completa Ollama → LiteRT-LM |
| Mayo 2026 | Tool calling nativo eliminando parseo JSON |
| Mayo 2026 | Terminal persistente TMUX con lectura de pantalla |
| Mayo 2026 | Browser automation CDP con research profundo |
| Mayo 2026 | Obsidian clipping con frontmatter YAML |
| Mayo 2026 | Traducción web con panel overlay |
| Mayo 2026 | VAD con webrtcvad para auto-stop por silencio |
| Mayo 2026 | AppState eliminando variables globales |
| Mayo 2026 | Configuración centralizada con Pydantic Settings |
| **Mayo 2026** | **Fase 1: Shell injection fix, ContextInjector async paralelo, funciones tmux async** |
| **Mayo 2026** | **Fase 2: TTS pipeline doble cola (synth \|\| play), _extract_sentences con coma** |
| **Mayo 2026** | **Fase 3: Browser refactor en 5 sub-módulos, pending_image movido a AppState** |
| **Mayo 2026** | **Fase 4: Graceful shutdown, paths unificados bajo settings, health endpoint** |
| **Mayo 2026** | **Fase 5: Rate limiting middleware (3 niveles: default, strict, streaming)** |
| **Mayo 2026** | **Fase 6: Investigación botones Bluetooth AVRCP (HOME SPA-133), módulo `bt_button_listener.py`, scripts de test** |
| **Mayo 2026** | **Fase 7: Dummy MPRIS Player para capturar AVRCP como trigger, integración en lifespan, mpris-proxy auto-start, dependencia `dbus-next`** |

---

*Este documento debe actualizarse con cada cambio significativo en la arquitectura o funcionalidad del proyecto.*
