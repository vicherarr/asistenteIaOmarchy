# AsistenteIA

Asistente de voz para Linux CachyOS con entorno Hyprland/Omarchy, diseñado para funcionar **100% en local**. Permite interactuar con tu sistema mediante voz: lanzar aplicaciones, controlar música, ajustar volumen, ver tu pantalla y más.

## Tabla de contenidos

- [Arquitectura](#arquitectura)
- [Tecnologías](#tecnologías)
- [Componentes principales](#componentes-principales)
- [Flujo de funcionamiento](#flujo-de-funcionamiento)
- [API REST](#api-rest)
- [Instalación](#instalación)
- [Uso](#uso)
- [Integración con Hyprland](#integración-con-hyprland)
- [Servicio systemd](#servicio-systemd)
- [Testing](#testing)
- [Estructura del proyecto](#estructura-del-proyecto)

---

## Arquitectura

AsistenteIA sigue una arquitectura de **orquestador FastAPI** que coordina múltiples módulos independientes:

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Server (:8765)                │
│  ┌───────────────────────────────────────────────────┐  │
│  │                  main.py (Orchestrator)            │  │
│  │                                                   │  │
│  │  ┌──────────┐  ┌──────────────┐  ┌────────────┐  │  │
│  │  │ AudioManager│  │CommandExecutor│  │ VisionTool │  │  │
│  │  └──────────┘  └──────────────┘  └────────────┘  │  │
│  │       │               │               │          │  │
│  │  ┌────┴──────┐  ┌─────┴──────┐  ┌────┴───────┐  │  │
│  │  │PipeWire/BT│  │omarchy/    │  │grim/slurp  │  │  │
│  │  │           │  │hyprctl/etc │  │            │  │  │
│  │  └───────────┘  └────────────┘  └────────────┘  │  │
│  │                                                   │  │
│  │  ┌──────────────┐  ┌──────────────┐              │  │
│  │  │ OllamaClient │  │  TTSEngine   │              │  │
│  │  │ (Gemma 4:e2b)│  │(Kokoro/gTTS) │              │  │
│  │  └──────────────┘  └──────────────┘              │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
         ▲                                    │
         │                                    │
    whisper.cpp                          paplay/ffplay
   (transcripción)                     (reproducción BT)
```

---

## Tecnologías

| Capa | Tecnología | Propósito |
|------|-----------|-----------|
| **LLM** | Ollama + Gemma 4:e2b | Motor de inteligencia local |
| **TTS** | Kokoro TTS (primary) / gTTS (fallback) | Síntesis de voz en español |
| **STT** | whisper.cpp (ggml-base.bin) | Transcripción de voz a texto |
| **API** | FastAPI + Uvicorn | Servidor asíncrono REST |
| **Audio** | PipeWire + WirePlumber + BlueZ | Gestión de audio Bluetooth |
| **WM** | Hyprland + Omarchy | Control de ventanas y sistema |
| **Captura** | grim + slurp | Capturas de pantalla para visión |
| **Reproducción** | paplay / ffplay / aplay | Output de audio al dispositivo BT |

---

## Componentes principales

### `src/main.py` - Orchestrator FastAPI

Servidor principal que coordina todos los módulos. Expone una API REST en `127.0.0.1:8765`.

**Responsabilidades:**
- Inicializa todos los componentes al arrancar (`lifespan`)
- Recibe transcripciones vía `/transcribe`
- Envía el texto a Ollama con contexto del sistema inyectado
- Parsea la respuesta JSON de Gemma para extraer comandos y tipo de acción
- Ejecuta comandos del sistema de forma segura
- Genera audio TTS de la respuesta
- Mantiene historial de conversación (máximo 10 mensajes)
- Soporta cancelación de procesamiento en curso (`/cancel`)

**Modelos de datos:**
- `TranscriptionRequest`: `{ "text": "..." }`
- `TranscriptionResponse`: `{ "status", "response_text", "commands_executed", "audio_file" }`
- `StatusResponse`: `{ "ollama_connected", "bluetooth_audio", "conversation_length", "processing" }`

### `src/ollama_client.py` - Cliente Ollama

Cliente asíncrono HTTP para comunicarse con la API local de Ollama.

**Características:**
- Modelo por defecto: `ministral-3:3b`
- Temperatura: 0.3, contexto: 4096 tokens
- Soporte para generación con imágenes (multimodal)
- Streaming token a token disponible
- Health check para verificar conectividad
- Timeout configurable (120s por defecto)

### `src/audio_manager.py` - Gestión de Audio Bluetooth

Detecta y configura automáticamente dispositivos Bluetooth mediante PipeWire/WirePlumber.

**Funcionalidades:**
- Parsea la salida de `wpctl status` para identificar dispositivos
- Detección inteligente de dispositivos BT por keywords (bluez, jbl, sony, bose, airpods, etc.)
- Configuración automática de source (micrófono) y sink (altavoz) BT como defaults
- Resumen de estado de audio para inyección de contexto

### `src/tts_engine.py` - Motor de Texto a Voz

Prioriza **Kokoro TTS** (local, español, alta calidad) con fallback a **gTTS** (requiere internet).

**Flujo:**
1. Intenta inicializar Kokoro con voz `em_alex` en español
2. Si Kokoro falla o no está disponible, usa gTTS
3. Genera audio en formato WAV (Kokoro, 24kHz) o MP3 (gTTS)
4. Reproduce al sink Bluetooth configurado vía `paplay`
5. Limpieza automática de archivos temporales antiguos

### `src/command_executor.py` - Ejecutor de Comandos

Ejecuta comandos del sistema de forma **segura** con lista blanca de prefijos permitidos.

**Comandos permitidos:**
- `omarchy`, `hyprctl`, `playerctl`, `wpctl`
- `chromium`, `firefox`, `spotify`, `vlc`, etc.
- `notify-send`, `grim`, `slurp`, `screenshot`
- Y más (ver `ALLOWED_PREFIXES` en el código)

**Parseo de respuestas:** Extrae JSON estructurado de las respuestas de Gemma (tanto en bloques ```json como inline) para obtener `response_text`, `commands` y `action_type`.

### `src/context_injector.py` - Inyección de Contexto

Recopila información del sistema en tiempo real para inyectar en el system prompt.

**Contexto recopilado:**
- CPU (`lscpu`)
- GPU (`lspci`)
- Memoria RAM (`free -h`)
- Disco (`df -h`)
- Monitores (`hyprctl monitors`)
- Ventana activa (`hyprctl activewindow`)
- Audio (`wpctl status`)
- Red (`ip addr`)

Construye el system prompt completo combinando: prompt base + contexto hardware + comandos disponibles + formato de respuesta requerido.

### `src/vision_tool.py` - Herramienta de Visión

Captura la pantalla y la convierte a base64 para modelos multimodales.

**Funcionalidades:**
- Captura completa con `grim`
- Captura de región con `grim + slurp`
- Redimensionado automático (máx 800px) para optimizar tokens
- Conversión a base64 para envío a Ollama
- Limpieza automática de archivos temporales

---

## Flujo de funcionamiento

1. **Usuario pulsa Super+Z** → se inicia `handy-toggle.sh`
2. **Grabación** → `parecord` graba audio del micrófono BT
3. **Usuario pulsa Super+Z de nuevo** → se detiene la grabación
4. **Transcripción** → `whisper-cli` convierte audio a texto
5. **Envío al orchestrator** → POST `/transcribe` con el texto
6. **Contexto inyectado** → se recopila info del sistema actual
7. **Generación LLM** → Ollama (Gemma 4:e2b) genera respuesta JSON
8. **Parseo** → se extraen comandos y tipo de acción
9. **Ejecución de comandos** → se ejecutan contra lista blanca
10. **Visión (si aplica)** → captura pantalla y re-genera respuesta
11. **TTS** → Kokoro/gTTS genera audio de la respuesta
12. **Reproducción** → audio enviado al altavoz Bluetooth

---

## API REST

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/transcribe` | Recibe texto, procesa con Gemma, ejecuta comandos y genera voz |
| `POST` | `/cancel` | Cancela procesamiento en curso y detiene TTS |
| `GET` | `/status` | Estado actual del asistente |
| `POST` | `/reset` | Reinicia el historial de conversación |
| `POST` | `/audio/configure` | Reconfigura dispositivos de audio Bluetooth |

---

## Instalación

### Requisitos previos

- **Sistema:** CachyOS (Arch Linux) con Hyprland/Omarchy
- **Audio:** PipeWire + WirePlumber
- **Python:** 3.12+

### Instalación automática

```bash
./install.sh
```

Este script instala:
1. Dependencias de sistema (pipewire, bluez, playerctl, grim, slurp, ffmpeg, espeak-ng)
2. Ollama (vía yay o script oficial)
3. whisper.cpp y modelo base
4. Entorno Python con todas las dependencias
5. Keybindings de Hyprland (Super+Z / Super+X)
6. Servicio systemd de usuario

### Instalación manual

```bash
# 1. Instalar dependencias
sudo pacman -S pipewire wireplumber pipewire-pulse bluez bluez-utils playerctl grim slurp ffmpeg espeak-ng

# 2. Instalar Ollama
yay -S ollama
ollama pull ministral-3:3b

# 3. Instalar whisper.cpp
yay -S whisper.cpp
# Descargar modelo
mkdir -p ~/.cache/whisper
curl -L -o ~/.cache/whisper/ggml-base.bin \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"

# 4. Configurar Python
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Uso

### Inicio manual

```bash
./start.sh
```

Verifica que Ollama esté corriendo, descarga el modelo si es necesario, y arranca el servidor FastAPI.

### Detener

```bash
./stop.sh
```

Detiene el orchestrator, descarga el modelo de memoria y opcionalmente detiene Ollama si fue iniciado por el script.

### Uso interactivo

Una vez iniciado:
- **Super+Z** → Iniciar/detener grabación de voz
- **Super+X** → Detener todo el servicio

---

## Integración con Hyprland

El script de instalación añade keybindings a `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + Z", "AsistenteIA Listen", "/ruta/scripts/handy-toggle.sh")
o.bind("SUPER + X", "AsistenteIA Stop", "/ruta/scripts/stop-assistant.sh")
```

`handy-toggle.sh` implementa un patrón de **toggle**:
- Primera pulsación: inicia grabación
- Segunda pulsación: detiene grabación y procesa
- Si el asistente está respondiendo: cancela y empieza a grabar

---

## Servicio systemd

### Instalar como servicio

```bash
./installservice.sh
```

### Comandos

```bash
systemctl --user start asistenteia        # Iniciar
systemctl --user stop asistenteia         # Detener
systemctl --user restart asistenteia      # Reiniciar
systemctl --user status asistenteia       # Estado
journalctl --user -u asistenteia -f       # Logs en vivo
```

El servicio se inicia automáticamente al iniciar sesión (`WantedBy=default.target`).

---

## Testing

El proyecto incluye tests unitarios y de integración con pytest:

```bash
source venv/bin/activate
pytest tests/ -v
```

### Módulos testeados

| Test | Cobertura |
|------|-----------|
| `test_audio_manager.py` | Parseo de wpctl, detección BT, configuración |
| `test_command_executor.py` | Seguridad, ejecución, parseo de JSON |
| `test_context_injector.py` | Recopilación de hardware, construcción de prompt |
| `test_main.py` | Endpoints FastAPI, flujo completo |
| `test_ollama_client.py` | Conexión, generación, errores HTTP |
| `test_tts_engine.py` | Síntesis, reproducción BT, limpieza |

---

## Estructura del proyecto

```
asistenteia/
├── config/
│   ├── omarchy_commands.md    # Manual de comandos disponibles para el LLM
│   └── system_prompt.txt      # Prompt base del asistente
├── scripts/
│   ├── handy-toggle.sh        # Toggle de escucha (Super+Z)
│   ├── start-assistant.sh     # Inicio del orchestrator
│   ├── stop-assistant.sh      # Parada completa
│   └── test-mic.py            # Script de prueba de micrófono
├── services/
│   └── asistenteia.service    # Unidad systemd de usuario
├── src/
│   ├── main.py                # Orchestrator FastAPI
│   ├── ollama_client.py       # Cliente Ollama
│   ├── audio_manager.py       # Gestión audio Bluetooth
│   ├── tts_engine.py          # Motor TTS (Kokoro/gTTS)
│   ├── command_executor.py    # Ejecutor seguro de comandos
│   ├── context_injector.py    # Inyección de contexto del sistema
│   └── vision_tool.py         # Captura de pantalla para visión
├── tests/
│   ├── test_audio_manager.py
│   ├── test_command_executor.py
│   ├── test_context_injector.py
│   ├── test_main.py
│   ├── test_ollama_client.py
│   └── test_tts_engine.py
├── install.sh                 # Script de instalación completo
├── installservice.sh          # Instalación como servicio systemd
├── start.sh                   # Inicio manual
├── startservice.sh            # Inicio vía servicio
├── stop.sh                    # Parada manual
├── stopservice.sh             # Parada vía servicio
└── requirements.txt           # Dependencias Python
```

---

## Notas de desarrollo

- **Todo funciona en local**: Ollama, Kokoro TTS y whisper.cpp no requieren conexión a internet (excepto gTTS como fallback)
- **Seguridad**: Los comandos se validan contra una lista blanca antes de ejecutarse
- **Multimodal**: El asistente puede "ver" tu pantalla capturándola y enviándola al modelo
- **Bluetooth-first**: Diseñado para funcionar con auriculares Bluetooth (micrófono + altavoz)
- **Contexto dinámico**: Cada consulta incluye información actual del hardware y estado del sistema
