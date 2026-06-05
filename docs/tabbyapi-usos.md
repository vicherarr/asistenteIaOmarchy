# TabbyAPI más allá de AsistenteIA — guía de usos

AsistenteIA usa [**TabbyAPI**](https://github.com/theroyallab/tabbyAPI) como sidecar del
motor exllama, pero TabbyAPI es un **servidor de inferencia OpenAI-compatible** de
propósito general. Este documento explica **para qué más se puede usar** la misma
tecnología (y la propia instalación que ya tienes en `~/.asistenteia/exllama/tabbyAPI`).

> **Idea central:** TabbyAPI expone una API **idéntica a la de OpenAI**
> (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`). Cualquier app, librería
> o herramienta que hable con OpenAI funciona contra tu GPU local **cambiando solo la
> `base_url` y la `api_key`**. Es tu "OpenAI privado", local y offline.

---

## 1. Qué expone TabbyAPI (referencia rápida)

### Endpoints OpenAI (drop-in)
| Endpoint | Uso |
|---|---|
| `POST /v1/chat/completions` | Chat (con streaming SSE, tool calling, visión) |
| `POST /v1/completions` | Completado de texto "crudo" |
| `POST /v1/embeddings` | Vectores de embedding (RAG, búsqueda semántica) |
| `GET /v1/models` | Modelo(s) disponibles |

### Endpoints propios (gestión)
| Endpoint | Uso |
|---|---|
| `POST /v1/model/load` · `GET /v1/model/list` | **Cargar/cambiar de modelo** en caliente vía API |
| `POST /v1/download` | **Descargar** un modelo de HuggingFace por API |
| `POST /v1/model/embedding/load` · `/v1/model/embedding/list` | Modelo de embeddings dedicado |
| `GET /v1/model/draft/list` | Modelos *draft* (speculative decoding) |
| `GET/POST /v1/loras` · `/v1/lora/list` | **LoRAs**: cargar/listar adaptadores |
| `GET/POST /v1/templates` · `/v1/sampling/overrides` | Plantillas de chat y presets de sampling |
| `GET /v1/auth/permission` · `/health` · `/props` | Auth, salud, capacidades |

---

## 2. El superpoder: reemplazo directo de OpenAI

Como la API es la de OpenAI, **no tienes que reescribir nada**. Solo apuntas el cliente
a tu servidor local.

**curl:**
```bash
curl http://127.0.0.1:5000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen3-8B-exl3-4bpw","messages":[{"role":"user","content":"Hola"}],"stream":true}'
```

**SDK de OpenAI (Python):**
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:5000/v1", api_key="tu-token-o-vacio")
r = client.chat.completions.create(
    model="Qwen3-8B-exl3-4bpw",
    messages=[{"role": "user", "content": "Resume este texto..."}],
)
print(r.choices[0].message.content)
```

**JavaScript / Node (`openai`):**
```js
import OpenAI from "openai";
const client = new OpenAI({ baseURL: "http://127.0.0.1:5000/v1", apiKey: "x" });
```

A partir de ahí, todo lo que consuma OpenAI funciona: LangChain, LlamaIndex,
LiteLLM, Vercel AI SDK, etc.

---

## 3. Casos de uso

### 3.1. Asistente de código en el editor
Herramientas como **aider**, **Continue.dev**, **Cline/Roo** o **Zed** permiten usar un
endpoint OpenAI-compatible. Apuntas `base_url` a TabbyAPI y tienes autocompletado/chat de
código **local y gratis**, con un modelo de código (p. ej. Qwen3-Coder en EXL3).

```bash
# aider con tu servidor local
aider --openai-api-base http://127.0.0.1:5000/v1 --openai-api-key x \
      --model Qwen3-8B-exl3-4bpw
```

### 3.2. Frontend de chat completo
**Open WebUI**, **LibreChat**, **SillyTavern** o **Lobe Chat** se conectan a un endpoint
OpenAI. Tendrías una web tipo ChatGPT, con historial y multiusuario, servida por tu GPU.

### 3.3. RAG y búsqueda semántica (embeddings)
Carga un modelo de embeddings (`/v1/model/embedding/load`) y usa `/v1/embeddings` para
vectorizar documentos. Encaja directo en **LlamaIndex** / **LangChain** como
`OpenAIEmbeddings(base_url=...)`. Sirves a la vez el LLM (chat) y los embeddings (RAG).

### 3.4. Salida estructurada / JSON garantizado
TabbyAPI integra **gramáticas** (formatron/kbnf): puedes forzar que el modelo responda
con un **JSON que cumpla un schema** (o cualquier gramática), útil para extracción de
datos, pipelines y agentes fiables, sin parsear texto a mano.

### 3.5. Function/tool calling y agentes
Soporta tool calling estilo OpenAI (`tools` + `tool_choice`). Cualquier framework de
agentes (LangGraph, CrewAI, etc.) que use el formato OpenAI puede orquestar herramientas
contra tu modelo local. *(AsistenteIA hace su propio bucle agéntico, pero no es
obligatorio: el servidor también puede parsear tool calls con `tool_format`.)*

### 3.6. Visión / multimodal
Con un modelo VL (como el Qwen3-VL que ya tienes) y `model.vision: true`, `/v1/chat/completions`
acepta imágenes (`image_url`/base64). Sirve para OCR, descripción de imágenes, análisis de
capturas, etc., desde cualquier cliente OpenAI multimodal.

### 3.7. LoRA hot-swap (modelos especializados)
Carga **adaptadores LoRA** sobre el modelo base sin duplicar pesos. Puedes servir
variantes (un LoRA para redacción, otro para SQL, otro para tu dominio) y combinarlas con
factores de escala, ahorrando VRAM frente a tener varios modelos completos.

### 3.8. Acelerar con speculative decoding (draft models)
Carga un **modelo draft** pequeño junto al grande: el draft propone tokens y el grande los
verifica, acelerando el decode sin perder calidad. Configurable en `draft_model`.

### 3.9. Estirar el contexto en poca VRAM (cache quantization)
`cache_mode` permite cuantizar la **KV-cache** (FP16 → Q8/Q6/Q4). Bajar la cache a Q4/Q8
libera VRAM y deja **más ventana de contexto** con el mismo modelo — clave en GPUs de
8 GiB (relevante para el caso Qwen3-VL).

### 3.10. Cambiar/descargar modelos por API
`POST /v1/download` baja un modelo de HuggingFace y `POST /v1/model/load` lo carga **en
caliente** sin reiniciar el servidor. Útil para un panel/automatización que rota modelos
según la tarea.

### 3.11. Servir a tu red local / equipo
Pon `network.host: 0.0.0.0` y `disable_auth: false` con **tokens de API**: varias
personas o dispositivos de tu LAN comparten tu GPU como un mini servicio de inferencia
privado (con permisos admin/usuario).

---

## 4. Reutilizar la instalación que ya tienes

Tu AsistenteIA ya trae un TabbyAPI funcional en `~/.asistenteia/exllama/tabbyAPI` (con su
venv py3.11 y modelos EXL3). Para experimentar con otros usos:

```bash
cd ~/.asistenteia/exllama/tabbyAPI
./venv/bin/python main.py            # arranca con su config.yml
# o con otra config aparte:
./venv/bin/python main.py --config mi-config.yml
```

> ⚠️ **Un solo uso de la GPU a la vez.** En 8 GiB, TabbyAPI no coexiste con el LiteRT del
> asistente (ni con dos instancias). Si vas a experimentar, hazlo con el asistente parado
> (`asistenteia stop`) o usando el motor exllama del propio asistente. Para no interferir,
> usa un **puerto** y un **config** distintos a los de producción.

Modelos: puedes bajar cualquier cuantización EXL3 de HuggingFace a `models/` (con
`huggingface_hub` o `POST /v1/download`) y apuntar `model.model_name` a su carpeta.

---

## 5. Opciones de config destacadas (`config_sample.yml`)

| Sección | Para qué |
|---|---|
| `network` | host/puerto, `disable_auth`, CORS, exposición en LAN |
| `model` | `model_name`, `max_seq_len`, `cache_size`, `cache_mode` (Q4/Q6/Q8), `vision`, `tool_format`, `gpu_split` (multi-GPU) |
| `draft_model` | speculative decoding (modelo draft) |
| `lora` | adaptadores LoRA y sus escalas |
| `embeddings` | modelo de embeddings dedicado |
| `sampling` | presets de sampling por defecto |
| `developer` | flags avanzados |

Sobre **`gpu_split`**: con varias GPUs puedes repartir el modelo entre ellas (manual o
`gpu_split_auto`) para correr modelos más grandes.

---

## 6. Cosas a tener en cuenta (limitaciones)

- **Un modelo principal cargado a la vez** por instancia (más su draft/embeddings/LoRAs).
  Para servir varios modelos a la vez, levanta varias instancias (más VRAM) o usa el
  hot-swap por API.
- **VRAM manda**: el modelo + KV-cache deben caber. Ajusta bpw, `max_seq_len` y
  `cache_mode` al hardware.
- **Single-user friendly**: ExLlamaV3 brilla en latencia single-user; para alta
  concurrencia masiva, soluciones tipo vLLM/SGLang escalan mejor (a costa de VRAM).
- **NVIDIA/AMD**: pensado sobre todo para CUDA (hay rama ROCm). Sin GPU compatible, no es
  su terreno.

---

## 7. Enlaces

- TabbyAPI: <https://github.com/theroyallab/tabbyAPI>
- Wiki / Getting Started: <https://github.com/theroyallab/tabbyAPI/wiki>
- Documentación de la API: <https://theroyallab.github.io/tabbyAPI>
- ExLlamaV3: <https://github.com/turboderp-org/exllamav3>
- Modelos EXL3 (HuggingFace): busca el tag `exl3`

---

*Complemento de [`motor-exllama.md`](motor-exllama.md), que cubre cómo AsistenteIA usa
TabbyAPI internamente. Este documento se centra en reutilizar la tecnología para otros
fines.*
