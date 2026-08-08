# Motor OpenRouter: el LLM en la nube

Tercer motor de inferencia, junto a `litert` (Gemma en proceso) y `exllama` (sidecar
TabbyAPI). Aquí no hay pesos ni VRAM: se le habla por HTTP a la API de
[OpenRouter](https://openrouter.ai), que es OpenAI-compatible, y el modelo corre en el
proveedor.

Qué se gana y qué se pierde:

| | Local (litert / exllama) | OpenRouter |
|---|---|---|
| Modelo | lo que quepa en 8 GiB | 30B+ sin tocar el hardware |
| Contexto | 4k–8k | 256k |
| Latencia | sin red | depende de la red |
| Privacidad | todo en casa | el prompt sale de la máquina |
| Disponibilidad | siempre | sujeta a la cuota del tier gratuito |

**Solo se ofrecen modelos gratis** (precio 0 en prompt y en completion) **y con tool
calling**: sin herramientas Luka no puede abrir la terminal, ni poner música, ni leer el
correo; sería un chatbot. Los de pago no aparecen en el selector.

## Puesta en marcha

```bash
asistenteia engine openrouter key <TU_API_KEY>   # la sacas de https://openrouter.ai/keys
asistenteia engine openrouter                    # cambia de motor y reinicia
```

La key se guarda **solo** en el `.env` de la instalación (que está en `.gitignore` y se
deja en modo 600). No viaja por git: si usas el repo de desarrollo y la instalación de
`~/.asistenteia`, hay que ponerla en cada una.

Volver atrás es simétrico y no deja rastro: `asistenteia engine litert`.

## Modelos

```bash
asistenteia engine openrouter model          # lista y pregunta
asistenteia engine openrouter model <id>     # fija uno concreto
```

El catálogo se consulta **en vivo** a `https://openrouter.ai/api/v1/models`
(`scripts/openrouter-models.py`): la lista de modelos gratis cambia cada pocas semanas y
hardcodearla sería garantizar que envejece mal. Se marca cuáles aceptan imágenes, y al
elegir uno la CLI ajusta sola `OPENROUTER_VISION`.

**Por defecto: `google/gemma-4-31b-it:free`.** De los modelos gratis con herramientas es
el más capaz de los que además ven imágenes (30,7B densos, 256k de contexto, function
calling nativo). Que sea Gemma no es casualidad: `config/system_prompt.txt` está afinado
a base de medir con Gemma y es frágil a los cambios, así que el motor de la nube le habla
al mismo modelo que el local.

Si eliges un modelo de solo texto, la CLI avisa: pierdes `analyze_camera` y
`analyze_screen`.

## El pool compartido y los 429

Los modelos `:free` se sirven desde un **pool compartido** entre todos los usuarios de
OpenRouter. Se satura a ratos y devuelve `429` aunque tu cuota personal esté intacta
(`limit_source: upstream_provider_shared_pool`). Medido durante el desarrollo: con el
pool de Gemma 31B saturado, la misma petición fallaba una y otra vez.

Por eso el motor manda la lista `models` de OpenRouter con modelos de reserva
(`OPENROUTER_FALLBACK_MODELS`): si el principal no puede atender, enruta solo al
siguiente. En la prueba anterior contestó `gemma-4-26b-a4b:free` sin que se notara. Los
dos de reserva por defecto también aceptan imágenes, para no perder la visión al caer.

Si aun así fallan todos, el usuario oye un mensaje concreto ("se ha agotado la cuota
diaria de los modelos gratuitos") en vez de un error genérico.

## Cómo está hecho

`src/engines/openrouter_engine.py` **hereda de `ExLlamaEngine`**, porque ese módulo ya es
el cliente OpenAI-compatible del proyecto: el streaming SSE, la conversión de tools a
function specs, la acumulación de `delta.tool_calls` y el bucle agéntico
(modelo → tool → modelo) son idénticos. Lo único que cambia es el transporte, así que
solo se redefinen los puntos de extensión que se añadieron en el padre:

| Hook | Qué cambia |
|---|---|
| `chat_url` | la base ya incluye `/v1` |
| `_engine_label` | el nombre en los mensajes de error |
| `_system_suffix` | sin `/no_think` (eso es de Qwen) |
| `_image_mime` | MIME real por *magic bytes* |
| `_extra_payload` | la lista `models` de reserva |
| `_http_error_message` | 401 / 429 / 404 / 5xx con mensajes distintos |

Dos detalles que importan:

- **No se pinga al arrancar.** El motor local hace un `httpx.get` síncrono en el
  constructor; contra un endpoint remoto eso sería latencia y un punto de fallo en el
  arranque de la app. Aquí basta con tener key; el fallo real se detecta al inferir.
- **El MIME de la imagen se calcula de verdad.** El padre etiquetaba siempre
  `data:image/png`, y las imágenes de este proyecto son **JPEG**: la foto de la cámara
  del ESP32 (`device_cam_*.jpg`) y las capturas redimensionadas. TabbyAPI lo toleraba;
  un proveedor en la nube puede rechazar un data-URI mal etiquetado, y eso rompería justo
  la visión de cámara.

El razonamiento de los modelos que piensan llega en `delta.reasoning`, un campo que el
bucle no lee: se descarta solo, que es lo que queremos en un asistente de voz.

## Configuración (`.env`)

| Clave | Defecto | Para qué |
|---|---|---|
| `OPENROUTER_MODEL` | `google/gemma-4-31b-it:free` | modelo principal |
| `OPENROUTER_FALLBACK_MODELS` | 26B A4B + Nemotron 12B VL | reserva ante 429 |
| `OPENROUTER_API_KEY` | vacío | tu key (nunca al repo) |
| `OPENROUTER_VISION` | `True` | lo ajusta la CLI según el modelo |
| `OPENROUTER_MAX_TOKENS` | 1024 | tope de respuesta |
| `OPENROUTER_TEMPERATURE` | 0.6 | |
| `OPENROUTER_MAX_TOOL_ROUNDS` | 8 | tope del bucle agéntico |
| `OPENROUTER_TIMEOUT` | 120 s | |
