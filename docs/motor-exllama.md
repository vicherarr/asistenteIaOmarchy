# Motor ExLlamaV3 — Tecnologías del segundo motor de inferencia

Documento técnico del desarrollo que añadió a AsistenteIA un **segundo motor de
inferencia intercambiable** basado en ExLlamaV3, además del LiteRT (Gemma) original.
Cubre las tecnologías empleadas, cómo encajan y las decisiones de diseño clave.

> **TL;DR:** AsistenteIA puede ejecutar su LLM con **LiteRT/Gemma** (por defecto, en
> proceso) o con **ExLlamaV3/Qwen3** sobre un **sidecar TabbyAPI** (servidor
> OpenAI-compatible en GPU). Es 100 % retrocompatible, conmutable con
> `asistenteia engine`, y un solo motor está activo a la vez. El modelo `qwen3-vl`
> añade **visión** (multimodal).

---

## 1. Por qué un segundo motor

LiteRT-LM (Gemma 4) es excelente para cargar rápido y correr texto+visión+audio en un
único modelo, pero el usuario quería **maximizar el rendimiento de generación en una
GPU de consumo** (NVIDIA RTX 4060 Ti, 8 GiB). ExLlamaV3 es hoy el rey del rendimiento
single-user en GPU NVIDIA de consumo, y su cuantización **EXL3** permite meter modelos
mayores en poca VRAM. Resultados medidos en la 4060 Ti:

| Métrica | LiteRT (Gemma E4B) | ExLlamaV3 (Qwen3-8B 4bpw) |
|---|---|---|
| Carga del modelo | ~7 s | ~7 s |
| Decode | ~12–18 tok/s* | **~56 tok/s** |
| TTFT | — | 0,13 s |
| VRAM | ~5,9 GB | ~5,7 GB |

<sub>*comparado con backends CPU/llama.cpp; LiteRT en GPU es más rápido que CPU pero ExLlamaV3 lidera el decode.</sub>

Se descartaron alternativas: **llama.cpp** (más lento), **TensorRT-LLM** (build lento),
**vLLM/SGLang** (orientados a servidor / mayor consumo de VRAM).

---

## 2. Arquitectura: motor intercambiable por contrato

El núcleo del diseño es un **contrato común** que abstrae el motor, de modo que el
resto del sistema (orquestador de chat, STT, API) no dependa de ninguna implementación
concreta.

```
                 ┌──────────────────────────────────────────┐
                 │  assistant_service / stt_engine / main.py │
                 └───────────────────┬──────────────────────┘
                                     │  contrato InferenceEngine
                 ┌───────────────────┴──────────────────────┐
                 │              factory.create_engine          │  (AI_ENGINE)
                 └───────┬───────────────────────────┬────────┘
                         │                           │
                ┌────────▼────────┐          ┌───────▼────────────────┐
                │  LiteRTClient   │          │     ExLlamaEngine        │
                │  (en proceso)   │          │  (cliente httpx async)   │
                └────────┬────────┘          └───────────┬────────────┘
                         │                               │ HTTP (OpenAI API)
                ┌────────▼────────┐          ┌───────────▼────────────┐
                │   LiteRT-LM     │          │  TabbyAPI  (sidecar)    │
                │   Gemma 4       │          │  ExLlamaV3 + Qwen3       │
                └─────────────────┘          └────────────────────────┘
```

- **`src/engines/base.py`** — define el `Protocol` `InferenceEngine` (runtime-checkable)
  y `EngineCapabilities(tools, vision, audio, gpu)`. El resto del sistema actúa **por
  capacidades**, no por nombre de motor (p. ej. el STT cae a Whisper si
  `capabilities.audio is False`).
- **`src/engines/factory.py`** — `create_engine(settings)` construye el motor según
  `AI_ENGINE` (`"litert"` por defecto, `"exllama"` opcional).
- **`src/engines/litert_engine`** (el `LiteRTClient` existente) — se conforma al contrato
  con cambios **solo aditivos** (`name`, `is_ready`, `backend_label()`, `capabilities`,
  `streams_clean_text`). Cero cambio de comportamiento.
- **`src/engines/exllama_engine.py`** — la implementación nueva.

Propiedades del contrato relevantes:

| Miembro | LiteRT | ExLlama | Para qué |
|---|---|---|---|
| `name` | `"LiteRT"` | `"ExLlama"` | Mostrar el motor en `/status` y la UI |
| `capabilities.vision` | `True` | según modelo | Habilitar el flujo de visión |
| `capabilities.audio` | `True` | `False` | STT nativo vs. fallback a Whisper |
| `backend_label()` | `GPU`/`CPU` | `GPU (ExLlama)` | Etiqueta de hardware |
| `streams_clean_text` | `False` | `True` | Si el stream ya es texto final limpio |

---

## 3. ExLlamaV3 y la cuantización EXL3

- **[ExLlamaV3](https://github.com/turboderp-org/exllamav3)** (v0.0.39) — motor de
  inferencia de LLMs cuantizados optimizado para GPUs NVIDIA. Kernels CUDA propios para
  máxima velocidad de decode single-user.
- **EXL3** — formato de cuantización de ExLlamaV3. Permite elegir el ratio de bits por
  peso (**bpw**): a menor bpw, menos VRAM y algo menos de calidad. Para 8 GiB se usan
  modelos de 8B a ~3.5–4.0 bpw.
- **flash-attn 2.8.3** y **xformers** — atención eficiente; vienen como wheels
  precompilados (no requieren compilar ni el toolkit de CUDA).
- **torch 2.9 + CUDA 12.8 (cu128)** — runtime; wheels precompilados.

> Todo el stack de ExLlama vive en un **venv propio y aislado** (Python 3.11), separado
> del venv principal del asistente, para no mezclar las pesadas dependencias de
> torch/CUDA/flash-attn con las del resto del proyecto.

---

## 4. TabbyAPI — el sidecar OpenAI-compatible

[**TabbyAPI**](https://github.com/theroyallab/tabbyAPI) es un servidor HTTP
OpenAI-compatible construido sobre ExLlamaV2/V3. AsistenteIA lo usa como **sidecar**
(proceso aparte) en vez de cargar ExLlamaV3 en el propio proceso. Razones:

1. **Aislamiento de fallos** — el código LiteRT hace `os._exit(1)` si el motor se cuelga;
   un proceso separado evita que un fallo del LLM tumbe el asistente.
2. **Aislamiento de dependencias** — torch/CUDA/flash-attn en su venv, no en el del
   asistente.
3. **Contrato estándar** — la API OpenAI (`/v1/chat/completions`) es estable y conocida;
   `ExLlamaEngine` habla con ella vía **`httpx`** (async, streaming SSE).

`ExLlamaEngine` se conecta por HTTP; no comparte memoria ni proceso con TabbyAPI.

### Hallazgo clave: NO usar `tool_format`

TabbyAPI puede parsear tool calls del modelo a formato OpenAI con `tool_format`, pero
en streaming **sin** `tool_format` emite las tool calls como `delta.tool_calls`
(formato OpenAI estándar), que es lo que `ExLlamaEngine` consume. Por eso la config de
TabbyAPI generada por AsistenteIA **omite `tool_format`** a propósito; el parser de
`<tool_call>` en texto queda solo como fallback.

---

## 5. Los modelos: Qwen3 y Qwen3-VL

Se eligió la familia **Qwen3** por su tool-calling/agéntico líder en open source (estilo
Hermes). Catálogo integrado (`asistenteia engine model`):

| Clave | Modelo | bpw | Visión | Contexto | VRAM (8 GiB) |
|---|---|---|---|---|---|
| `qwen3-8b` | `turboderp/Qwen3-8B-exl3` | 4.0 | ❌ | 8192 | ~5,7 GB |
| `qwen3-vl` | `ArtusDev/Qwen_Qwen3-VL-8B-Instruct-EXL3` | 3.5 | ✅ | 6144 | ~6,3 GB |

- **Qwen3-8B** — texto + tools, rápido. Modelo base.
- **Qwen3-VL-8B-Instruct** — multimodal (texto + tools + **visión**); conserva la visión
  que el asistente usa hoy con LiteRT (`analyze_screen`, etc.).

### `/no_think`

Qwen3 tiene "thinking" (`<think>…</think>`) activado por defecto. Para un asistente de
voz se desactiva (latencia/tokens) añadiendo `/no_think` al system prompt, pero es
**configurable** (`EXLLAMA_THINKING`, default `False`). `ExLlamaEngine` filtra los
bloques `<think>` del stream para que nunca lleguen al TTS.

---

## 6. El bucle agéntico de tool-calling (`ExLlamaEngine`)

A diferencia de LiteRT (que tiene tool-calling nativo en proceso), aquí el **bucle
agéntico es propio**. `src/engines/exllama_engine.py` implementa:

1. **Conversor `callable → JSON-schema OpenAI`** (`callable_to_schema`): a partir de la
   firma (`inspect.signature`) y el docstring de cada tool Python genera la `function
   spec` que espera la API OpenAI.
2. **Filtro de stream incremental** (`_StreamFilter`): separa el texto natural
   (emitible al TTS) de los bloques `<think>` (descartados) y `<tool_call>` (capturados),
   manejando marcadores partidos entre chunks.
3. **Bucle agéntico** (`chat_stream`): por cada ronda lee el stream; si hay tool calls
   (preferente: `delta.tool_calls` estructuradas; fallback: `<tool_call>` en texto),
   ejecuta la herramienta Python, reinyecta el resultado como mensaje `role: tool` y
   reitera, hasta la respuesta final (tope `EXLLAMA_MAX_TOOL_ROUNDS`).
4. **Ejecución de tools** (`_exec_tool`): `await` directo si la tool es async, o
   `asyncio.to_thread` si es síncrona.

```
usuario → [TabbyAPI] → ¿tool_calls?
                          │ sí → ejecutar tool(s) Python → reinyectar resultado ─┐
                          │                                                      │
                          └──────────────────── repetir ◄───────────────────────┘
                          │ no → respuesta final (texto limpio al TTS)
```

---

## 7. Visión multimodal (Qwen3-VL)

- exllamav3 0.0.39 soporta la arquitectura **Qwen3-VL** (`architecture/qwen3_vl.py`,
  con vision tower). TabbyAPI la carga con `model.vision: true` en su config.
- `ExLlamaEngine` envía imágenes como `image_url` con la imagen en **base64** dentro del
  contenido del mensaje (formato OpenAI multimodal), solo si `EXLLAMA_VISION=True`.
- El flujo de visión del asistente (2 pasadas: la tool `analyze_screen` registra la
  captura → segunda llamada con la imagen adjunta) funciona igual que con LiteRT, porque
  `capabilities.vision` pasa a `True` con el modelo VL.

---

## 8. Ciclo de vida del sidecar (lifecycle)

El sidecar debe levantarse y pararse **junto al asistente** (como LiteRT se carga solo),
con **un único motor activo a la vez** (en 8 GiB, LiteRT y TabbyAPI no coexisten).

Dos mecanismos según el modo de despliegue:

- **Con servicio systemd (producción):** una **unit companion `asistenteia-tabby.service`**
  (`scripts/tabby-run.sh` la ejecuta en foreground). El unit principal la declara
  `Wants=` + `After=`, y la companion es `PartOf=` del asistente. Así systemd arranca
  TabbyAPI **antes** del asistente (también al boot/login) y, al parar/reiniciar, mata el
  **cgroup** entero (padre + hijo que retiene la VRAM) sin huérfanos. La companion solo
  arranca si `AI_ENGINE=exllama` (con litert sale 0, sin consumir VRAM).
- **Sin servicio (bajo demanda):** los scripts shell (`ai_tabby_start`/`ai_tabby_stop`)
  lo arrancan con `setsid` (grupo propio) y lo paran con `kill -- -PGID`.

---

## 9. STT por capacidades

ExLlamaV3/Qwen3 no procesa audio (`capabilities.audio = False`). El STT (`stt_engine.py`)
decide el backend **por capacidad del motor**, no por configuración manual: si el motor
activo no soporta audio, cae a **faster-whisper** automáticamente en vez de intentar
transcribir con un motor que no puede.

---

## 10. Decisiones técnicas y "gotchas"

| Tema | Detalle |
|---|---|
| **`tool_format` off** | Sin él, TabbyAPI emite `delta.tool_calls` estándar (ver §4). |
| **Stream limpio vs. fugado** | LiteRT fuga tool calls como texto y necesita limpieza + heurística de fallback; ExLlama entrega texto final limpio. La propiedad `streams_clean_text` evita aplicar el fallback de LiteRT a ExLlama (que confundía respuestas cortas válidas como "42" con residuos). |
| **VRAM vs. contexto (8 GiB)** | Qwen3-VL a 3.5bpw solo cabe a `max_seq_len=6144`: `8192` da *"Insufficient VRAM in split"*; `4096` es poco (el system prompt + imagen abortan con *"Job requires N pages"*). |
| **venv aislado** | Las deps de ExLlama (torch cu128, flash-attn…) viven en su propio venv py3.11, no en el del asistente. |
| **Wheels precompilados** | La instalación no compila nada (ni necesita el CUDA toolkit): usa wheels de torch/exllamav3/flash-attn. |

---

## 11. Instalación y uso (CLI)

```bash
asistenteia engine                  # motor actual + disponibles (estado del sidecar)
asistenteia engine install [m]      # instala el backend exllama (TabbyAPI + venv + modelo)
asistenteia engine exllama          # conmuta a ExLlamaV3 y reinicia
asistenteia engine litert           # vuelve a LiteRT (para el sidecar, libera VRAM)
asistenteia engine model [list|qwen3-8b|qwen3-vl]   # gestiona el modelo exllama
asistenteia engine model qwen3-vl   # descarga/activa Qwen3-VL (visión) y reinicia
asistenteia engine start|stop       # control manual del sidecar TabbyAPI
```

El instalador clona TabbyAPI, crea su venv (py3.10–3.13, prefiere 3.11), instala
`.[cu12]` (NVIDIA) o `.[amd]`, escribe `config.yml` y descarga el modelo EXL3. La
instalación es opt-in y aislada (~15 GB); nunca se versiona (`.gitignore: exllama/`).

---

## 12. Resumen de tecnologías

| Capa | Tecnología |
|---|---|
| Motor de inferencia | **ExLlamaV3** 0.0.39 (kernels CUDA, cuantización EXL3) |
| Servidor / sidecar | **TabbyAPI** (OpenAI-compatible) |
| Cliente HTTP | **httpx** (async, streaming SSE) |
| Modelos | **Qwen3-8B-exl3** (texto), **Qwen3-VL-8B-Instruct-EXL3** (visión) |
| Runtime GPU | **torch 2.9 + CUDA 12.8**, **flash-attn 2.8.3**, **xformers** |
| Descarga de modelos | **huggingface_hub** (`snapshot_download`) |
| Ciclo de vida | **systemd** (unit companion, cgroups) + scripts shell (`setsid`/PID) |
| Aislamiento | **venv** Python 3.11 dedicado |
| Contrato/diseño | `Protocol` `InferenceEngine` + `EngineCapabilities` (Python typing) |

---

*Generado durante el desarrollo del motor intercambiable LiteRT ⇄ ExLlamaV3 (Fases 0–4
+ integración de Qwen3-VL). Ver también el README principal, sección «Arquitectura».*
