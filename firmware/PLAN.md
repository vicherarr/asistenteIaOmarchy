# Firmware `luka-speaker` — Plan de arquitectura

Firmware en **Rust** para el **Waveshare ESP32-S3-AUDIO-Board**, que convierte la placa en
un satélite de voz de Luka: escucha, envía el audio por WiFi al asistente de este
proyecto y reproduce su respuesta por el altavoz.

> **Estado: PLAN. No se ha escrito código todavía.** Este documento existe para acordar
> arquitectura y alcance antes de codificar. Al final hay una sección de **decisiones
> pendientes** que necesito que confirmes.

---

## 1. Resumen ejecutivo

| | |
|---|---|
| **Placa** | Waveshare ESP32-S3-AUDIO-Board (ESP32-S3R8: 8 MB PSRAM, 16 MB flash) |
| **Detectada** | `/dev/ttyACM0` — USB-JTAG/serial nativo (`303a:1001`), permisos OK |
| **Lenguaje** | Rust, track **`std` sobre ESP-IDF** (`esp-idf-svc` / `esp-idf-hal`) |
| **Toolchain** | Ya instalado: `espup 0.17.1`, `espflash 4.5.0`, toolchain `esp` (Xtensa) |
| **Wake word "Luka"** | **Viable** (microWakeWord, modelo propio entrenado). No trivial pero acotado. **Camino final se decide en la Fase 3**; las fases previas no lo hipotecan. Ver §3 |
| **Interfaz de estado** | Anillo de 7 LEDs RGB con código de colores + animaciones + parpadeos de diagnóstico. Ver §5.5 |
| **Transporte** | WebSocket binario en LAN, PCM16 16 kHz mono, auth con el `API_TOKEN` existente |
| **Requiere cambios en el Python** | Sí, mínimos y aditivos. Ver §7 |

**Veredicto corto sobre la wake word:** sí se puede hacer "Luka" en el dispositivo, y no
hace falta el proceso comercial de Espressif. La complicación real no es el modelo, es que
**obliga a meter TensorFlow Lite Micro (C++) en el proyecto Rust** vía FFI. Por eso el plan
lo deja para la **Fase 3**, después de tener un producto que ya funciona con botón.

---

## 2. Hardware

### 2.1 Confirmado

- **ESP32-S3R8**: Xtensa LX7 dual-core @240 MHz, 512 KB SRAM, **8 MB PSRAM** (octal), **16 MB flash**.
- **ES8311** — codec DAC → amplificador → altavoz.
- **ES7210** — ADC de 4 canales → array de **2 micrófonos** (+ canales libres para
  referencia de eco).
- **TCA9555** — expansor GPIO I²C (botones y enable del amplificador cuelgan de aquí).
- **PCF85063** — RTC.
- **7 LEDs RGB** direccionables en anillo.
- WiFi 4 + BLE 5, USB-C nativo, batería Li-ion 3.7 V con carga, microSD, cabecera LCD SPI,
  cabecera cámara DVP.

### 2.2 Mapa de pines — **PENDIENTE DE VERIFICAR EN LA PLACA**

La documentación oficial de Waveshare **no publica la tabla de GPIOs**. Estos valores
vienen de un port de ESPHome de terceros para esta misma placa y son el mejor punto de
partida, pero **no los doy por buenos**: la primera tarea de la Fase 0 es confirmarlos
contra el hardware real (escaneo I²C + prueba de tono).

| Función | GPIO / bus | Confianza |
|---|---|---|
| I²C SDA | `GPIO11` | media |
| I²C SCL | `GPIO10` | media |
| I2S MCLK | `GPIO12` | media |
| I2S BCLK | `GPIO13` | media |
| I2S WS / LRCK | `GPIO14` | media |
| I2S DIN (ES7210 → ESP, micros) | `GPIO15` | media |
| I2S DOUT (ESP → ES8311, altavoz) | `GPIO16` | media |
| LEDs RGB (WS2812) | `GPIO38` | media |
| Enable del amplificador | TCA9555 P8 | media |
| Botones ×3 | TCA9555 P9/P10/P11 | media |
| microSD / LCD / cámara | sin documentar | — |

**Direcciones I²C esperadas:** ES8311 `0x18`, TCA9555 `0x20`, ES7210 `0x40`, PCF85063 `0x51`.
Un escaneo I²C las confirma en 30 segundos y valida de paso los pines SDA/SCL.

---

## 3. Wake word "Luka" — evaluación honesta

Es la pregunta que pediste evaluar antes de nada. Hay tres caminos reales.

### Opción A — microWakeWord en el dispositivo ⭐ recomendada como objetivo

Modelo TFLite INT8 en *streaming*, ~40 kB, el mismo enfoque que usan los satélites de voz
de ESPHome / Home Assistant en ESP32-S3. El entrenamiento de una palabra nueva **está
resuelto y automatizado**: se genera el corpus con TTS sintético (cientos/miles de
variantes de "Luka" con distintas voces, velocidades, ruidos y reverberaciones), se
combina con los datasets negativos publicados y sale un `.tflite` listo para flashear.

- **Coste de entrenamiento:** ~1–3 h, casi todo desatendido.
- **Coste de integración (lo caro):** TFLite Micro es C++. Hay que añadir
  `esp-tflite-micro` como componente de ESP-IDF, escribir un *shim* en C y envolverlo en
  Rust con `bindgen`. Son ~200–300 líneas de *glue* y es el único punto del proyecto con
  `unsafe`. Es un camino trillado, pero es **el trozo de más riesgo de todo el firmware**.
- **Riesgo funcional:** "Luka" son dos sílabas y poca energía → tasa de falsos positivos
  más alta que una palabra larga. Es un problema real, no teórico: en este mismo proyecto
  el detector Sherpa del PC está con `WAKE_WORD_THRESHOLD=0.10`, un umbral muy permisivo,
  lo que sugiere que ya te ha costado que dispare. **Mitigación:** entrenar
  **"Oye Luka"** o **"Hola Luka"** en vez de "Luka" a secas — mucho más robusto y sigue
  sonando natural. Se puede entrenar más de un modelo y comparar.

### Opción B — Wake word en el PC, reutilizando el Sherpa-ONNX que ya tienes

El ESP32 hace solo un VAD barato (energía / cruces por cero) y transmite al PC únicamente
cuando hay voz; el PC corre el modelo **"LUKA" que ya está en `models/sherpa-kws/`** y
decide. Comportamiento **idéntico** al del asistente actual, cero ML en el dispositivo,
cero `unsafe`, cero C++.

- **A favor:** riesgo prácticamente nulo, firmware mucho más simple, un solo modelo de wake
  word que mantener en todo el sistema.
- **En contra:** tráfico WiFi casi continuo mientras haya conversación en la sala, la placa
  no es autónoma (sin PC no hace nada), y algo más de latencia.

### Opción C — Sin wake word: pulsar botón (push-to-talk)

Trivial. La placa tiene 3 botones. Es lo que la Fase 1 entrega de todas formas.

### Decisión tomada

**Construir en este orden: C → (A o B).** La Fase 1 entrega un satélite funcional con botón
(sin ML, sin riesgo). **La elección entre A y B se pospone a la Fase 3**, con el hardware ya
funcionando y sabiendo cómo se comporta el audio real de estos micros — que es justo la
información que hoy falta para decidir bien.

Consecuencia de diseño: `luka-audio` se escribe desde ya con **VAD y ring de pre-roll de
1 s**, porque los necesitan *las dos* opciones. Lo único que difiere es quién consume esas
tramas — un modelo local (A) o el enlace de red (B) —, y eso queda tras un `trait Detector`
con dos implementaciones. Ninguna de las dos rutas queda hipotecada.

Mi apuesta personal, para cuando llegue el momento: **A con "Oye Luka"**, porque hace la
placa autónoma y no satura la WiFi.

Descartado explícitamente: **ESP-SR / WakeNet de Espressif**. Sus modelos pre-entrenados no
incluyen "Luka" y una palabra a medida exige un corpus de más de 500 personas (incluidos
100 niños), 15 repeticiones cada una. Inviable aquí. (Su AFE — cancelación de eco y
supresión de ruido — sí lo podemos aprovechar por separado; ver Fase 4.)

---

## 4. Decisión de track: `std` (ESP-IDF) en vez de `no_std` (esp-hal)

| Criterio | `no_std` + `esp-hal` | `std` + `esp-idf-svc` ⭐ |
|---|---|---|
| WiFi + TCP/WebSocket | `esp-wifi` (aún tras *feature* `unstable`) + `embassy-net` a mano | Pila del IDF, `std::net`, probada en producción |
| I2S full-duplex 16 kHz | Driver joven, ES7210/TDM a pelo | Driver I2S maduro del IDF |
| TFLite Micro (wake word) | Prácticamente inviable | `esp-tflite-micro` es un componente IDF estándar |
| AEC / supresión de ruido (ESP-SR) | No disponible | Disponible |
| Concurrencia | Embassy async | Hilos FreeRTOS + canales |
| Elegancia / tamaño binario | Mejor | Peor |
| Tiempo de build en frío | Rápido | Lento (bootstrapea ESP-IDF una vez) |

`no_std` es más bonito, pero este dispositivo es esencialmente **un streamer de audio en
red con un modelo de ML**: casi todo lo que necesita vive en el mundo ESP-IDF. Ir por
`no_std` significaría reimplementar a mano precisamente las cuatro piezas que más valor
aportan. Vamos con `std`.

> `esp-idf-sys` se descarga y compila ESP-IDF solo la primera vez. Conviene instalar
> `ccache` (`sudo pacman -S ccache`) para que las recompilaciones no duelan.

---

## 5. Arquitectura del firmware

### 5.1 Estructura del workspace

```
firmware/
  Cargo.toml            — workspace
  rust-toolchain.toml   — canal `esp` fijado
  .cargo/config.toml    — target xtensa-esp32s3-espidf, runner espflash
  sdkconfig.defaults    — PSRAM octal, tamaño de flash, stacks, log level
  cfg.toml              — SECRETOS (WiFi, token). GITIGNORED
  cfg.toml.example      — plantilla versionada
  PLAN.md               — este documento
  crates/
    luka-proto/         — [host-testable] códec del protocolo, tipos de mensaje
    luka-board/         — BSP: pines, ES8311, ES7210, TCA9555 (traits embedded-hal)
    luka-audio/         — ring buffers, VAD, remuestreo, formatos
    luka-wakeword/      — envoltorio FFI de TFLite Micro (Fase 3, único módulo `unsafe`)
    luka-firmware/      — binario: FSM, hilos, WiFi, WebSocket, LEDs
  xtask/                — `cargo xtask flash|monitor|spike <n>`
```

**Por qué en crates separados:** `luka-proto`, `luka-audio` y los drivers de
`luka-board` son lógica pura sobre *traits* `embedded-hal`. Eso permite ejecutar
`cargo test` **en el PC**, sin placa, con `embedded-hal-mock` — que en embebido es la
diferencia entre iterar en segundos o en minutos-con-flasheo.

### 5.2 Modelo de concurrencia: hilos + canales acotados

Nada de async. En ESP-IDF `std`, `std::thread` son tareas FreeRTOS de verdad, con
prioridad y afinidad de núcleo. El diseño es un pipeline clásico:

```
        ┌────────────────────────────────────────────────────────────┐
        │  audio_io  (core 1, prioridad alta) — ÚNICO dueño del I2S   │
        │  bucle full-duplex: lee 20 ms de los micros, escribe 20 ms  │
        │  al altavoz. Nunca bloquea, nunca asigna memoria.           │
        └───────┬────────────────────────────────────┬───────────────┘
         frames │ (mpsc acotado)      (mpsc acotado) │ pcm de salida
                ▼                                    ▲
        ┌───────────────────┐              ┌─────────┴─────────┐
        │ detect (core 1)   │              │  net (core 0)     │
        │ VAD + pre-roll 1s │─── voz ─────▶│  WebSocket:       │
        │ + wake word (F3)  │              │  sube PCM,        │
        └───────┬───────────┘              │  baja TTS         │
                │ eventos                  └─────────┬─────────┘
                ▼                                    │ eventos
        ┌────────────────────────────────────────────▼───────────────┐
        │  supervisor (core 0) — máquina de estados + watchdog        │
        └────────────────────────┬───────────────────────────────────┘
                                 ▼ estado
                        ┌────────────────────┐
                        │  ui (core 0, baja) │  7 LEDs RGB vía RMT
                        │  + botones (TCA)   │
                        └────────────────────┘
```

**Detalle crítico:** el I2S de la placa es *full-duplex sobre el mismo periférico* (BCLK y
WS compartidos entre ES7210 y ES8311). Por eso **un solo hilo posee el driver** y hace
lectura y escritura en el mismo bucle; el resto le habla por canales. Repartir el I2S entre
dos hilos es la vía rápida a *glitches* y a un `Mutex` en la ruta de tiempo real.

**Presupuesto de memoria:** buffers DMA en SRAM interna (obligatorio); el ring de pre-roll
(1 s = 32 KB) y el *jitter buffer* de TTS (2–4 s) en PSRAM, que sobra (8 MB).

### 5.3 Máquina de estados

```rust
enum State {
    Booting,
    WifiConnecting { since: Instant },
    Disconnected  { retry_in: Duration },   // backoff exponencial con jitter
    Idle,                                   // esperando botón / wake word
    Listening     { started: Instant },     // capturando y subiendo
    Thinking,                               // audio enviado, esperando a Luka
    Speaking,                               // reproduciendo TTS
    Error(FaultKind),
}
```

Transiciones explícitas en una función pura `fn next(state, event) -> (State, Vec<Action>)`,
**testeable en el host**. Cada estado tiene su color/animación de LED y su *timeout*: nada
puede quedarse colgado para siempre (`Listening` corta a los 15 s, `Thinking` a los 30 s).

### 5.4 Audio

| Parámetro | Valor | Motivo |
|---|---|---|
| Frecuencia de muestreo | 16 kHz mono | Lo que quiere Whisper; el `stt_engine` ya normaliza a 16 k |
| Formato | PCM16 LE | Sin códec en v1; ~256 kbps, irrelevante en LAN |
| Tamaño de trama | 20 ms (320 muestras / 640 B) | Compromiso latencia ↔ *overhead* |
| Pre-roll | 1 s | Que no se coma el principio de la frase tras la wake word |
| Salida TTS | PCM16 16 kHz mono | Simetría; el servidor remuestrea desde los 24 kHz de Kokoro |

**Half-duplex en v1:** mientras Luka habla, el micro se ignora. Interrumpirla hablando
(*barge-in*) exige cancelación de eco acústico y se pospone a la Fase 4. Es exactamente lo
que hace ya el asistente en el PC (pausa los reproductores mientras graba).

### 5.5 Lenguaje visual: los 7 LEDs RGB

Es el **único canal de diagnóstico del dispositivo**: no hay pantalla en v1 y mirar el log
por serie exige tenerlo enchufado al PC. Así que el anillo no es decoración, es la interfaz
de estado, y se diseña como tal.

> Nota: son **7 LEDs direccionables en anillo** ("surround RGB"), no una matriz XY. El
> diseño de abajo funciona igual si en la placa resultan ser más o estar en otra
> disposición; `spike-rgb` (Fase 0) confirma cantidad y orden físico.

#### Principio de diseño

**El movimiento distingue, el color confirma.** Cada estado tiene una *animación* propia
(giro, respiración, vúmetro, parpadeo), no solo un color. Así el estado se lee de un
vistazo, de reojo, desde lejos y sin depender de distinguir bien cian de verde — que a 2
metros y con el difusor puesto no es tan obvio como parece en una tabla.

#### Tabla de estados

| Estado | Color | Animación | Lectura |
|---|---|---|---|
| `Booting` | blanco 20 % | Barrido rápido 1 vuelta | "arrancando" |
| `WifiConnecting` | azul | 1 LED girando, 1 vuelta/s | "buscando la red" |
| `Disconnected` | ámbar | 1 LED girando, lento (0,3 v/s) | "sin servidor, reintentando" |
| `Idle` | apagado | 1 LED "faro" al 3 %, respiración de 6 s | "vivo, esperando" — no molesta de noche |
| `WakeDetected` | blanco | Flash de anillo completo, 120 ms | "te he oído" (acuse instantáneo) |
| `Listening` | cian | **Vúmetro**: N LEDs según nivel de voz | "te estoy oyendo, y cuánto" |
| `Thinking` | violeta | Cometa de 3 LEDs, 1,5 vueltas/s | "procesando" |
| `Speaking` | verde | Respiración según amplitud del TTS | "hablando" |
| `Muted` | rojo 15 % | 1 LED fijo, sin animación | "micro cortado" |
| `Updating` | azul | Barra de progreso (n/7 LEDs) | "OTA al 43 %" |
| `LowBattery` | naranja | Doble parpadeo cada 5 s, superpuesto | aviso sin tapar el estado |
| `Error(k)` | rojo | **k parpadeos**, pausa de 1,5 s, repite | diagnóstico sin cable |

El **vúmetro de `Listening`** es lo que más se agradece en uso real: ves si el micro te
capta y si estás demasiado lejos, sin adivinar. Se alimenta del RMS de la trama de audio
que ya calcula el VAD, así que sale gratis.

#### Códigos de error (parpadeos rojos)

Diagnóstico sin conectar nada, en el espíritu de los códigos de arranque de una BIOS:

| Parpadeos | Fallo | Primera cosa que mirar |
|---|---|---|
| 1 | WiFi: no asocia | SSID/PSK, ¿está el router en 2,4 GHz? |
| 2 | Servidor inalcanzable | ¿corre `asistenteia`? ¿`HOST` a la LAN? ¿IP correcta? |
| 3 | Auth rechazada | `API_TOKEN` de `cfg.toml` ≠ el del `.env` del PC |
| 4 | Audio: I²C/codec | Pines o secuencia de init del ES8311/ES7210 |
| 5 | Wake word / modelo | Fase 3: modelo ausente o corrupto |
| 6 | Reinicio por pánico | Se muestra 3 s al arrancar; la causa está en NVS |

#### Implementación

- **`luka-ui`**: motor de animación como **función pura `fn frame(state, t, level) -> [Rgb8; 7]`**,
  con `cargo test` en el host — se pueden verificar patrones y transiciones sin placa, e
  incluso volcarlos a un GIF para revisarlos de un vistazo.
- **Corrección gamma obligatoria.** Los WS2812 son lineales en PWM pero el ojo no: sin
  gamma, el 3 % del `Idle` se ve como un 25 % y las respiraciones salen a saltos. Tabla LUT
  de 256 entradas precalculada en `const`.
- **Brillo global configurable** en `cfg.toml` + modo nocturno (techo de brillo por horas,
  vía el RTC PCF85063 que trae la placa).
- Salida por el periférico **RMT** con DMA — el *bit-banging* de WS2812 a mano competiría
  con el hilo de audio por los ciclos.
- Hilo `ui` a **50 fps**, baja prioridad, núcleo 0: nunca interfiere con el audio, y si se
  retrasa un frame no pasa absolutamente nada.
- Los avisos (`LowBattery`) se **superponen** al estado base en vez de sustituirlo: nunca
  pierdes de vista lo que la placa está haciendo por un aviso secundario.

**Fase 3:** la wake word añade `WakeDetected` (el flash de acuse, para que sepas al
instante que te ha oído y no repitas la frase) y un modo de calibración — el anillo mostrará
la *confianza* del detector como vúmetro, que es la forma práctica de ajustar el umbral sin
instrumentar nada.

---

## 6. Protocolo de red

Un solo **WebSocket** `/device/ws`, binario, con una cabecera de 1 byte por trama.

```
Dispositivo → Servidor            Servidor → Dispositivo
  0x01 HELLO   {json: id, fw, caps}   0x81 STATE      {json: listening|thinking|speaking}
  0x02 AUDIO   [pcm16le]              0x82 TRANSCRIPT {json: texto del usuario}
  0x03 END     —  (fin de turno)      0x83 REPLY      {json: texto de Luka}
  0x04 CANCEL  —                      0x84 TTS_AUDIO  [pcm16le]
  0x05 PING    —                      0x85 TTS_END    —
                                      0x86 ERROR      {json: code, message}
```

- **Auth:** `X-API-Token` en el *upgrade* (mismo token que ya usa la GUI y la API).
- **TLS: sí, `wss://` con el certificado FIJADO en el firmware** (*pinning*). El servidor
  del asistente ya corre con HTTPS (`SSL_KEYFILE`/`SSL_CERTFILE`), así que no era opcional.

  El firmware **no confía en ninguna autoridad certificadora**: lleva empotrado el
  certificado del servidor y solo acepta ese. Es una garantía **más fuerte** que la
  validación normal —no basta con presentar un certificado válido, tiene que ser *este*—
  y además es la única opción viable, porque el certificado del asistente es autofirmado
  y con `CN=localhost`: nunca pasaría una validación de nombre contra una IP de la LAN.
  De ahí que se active `skip_cert_common_name_check`: el nombre no se comprueba porque no
  aporta nada cuando ya se exige el certificado exacto.

  - Se copia con **`firmware/scripts/sync-cert.sh`**, que lo saca del **deploy**
    (`~/.asistenteia`), no del repo de desarrollo — tienen certificados distintos y fijar
    el equivocado da un fallo de handshake que no apunta a su causa.
  - `firmware/certs/` está en `.gitignore`: es específico de cada despliegue.
  - Si algún día se regeneran los certificados del asistente, hay que volver a ejecutar el
    script; hasta entonces la placa no conectaría.
  - Coste: el certificado es RSA-4096, así que el handshake no es gratis en el ESP32-S3.
    Como el WebSocket es persistente, se paga una vez por conexión y no por turno de voz.
- **Robustez:** *heartbeat* cada 10 s, reconexión con *backoff* exponencial + *jitter*,
  y descarte de las tramas de audio más viejas si el enlace se atasca (nunca crecer sin
  límite: el audio viejo no vale nada).
- **Descubrimiento del servidor:** IP fija por configuración en v1; mDNS (`_luka._tcp`)
  como mejora de la Fase 2.

---

## 7. Cambios necesarios en el lado Python

El firmware no sirve de nada si el servidor no tiene por dónde recibir audio. Hoy **no
existe ningún endpoint que acepte audio**: la GUI manda texto a `/transcribe/stream` y
`/listen/toggle` graba con el micro del propio PC. Cambios propuestos, todos **aditivos**
(si no hay dispositivo, nada cambia):

1. **`HOST=0.0.0.0`** (hoy `127.0.0.1` en `src/config.py:14`). Sin esto la placa no puede
   ni conectar. Implica exponer la API a la LAN → el `API_TOKEN` pasa de conveniencia a
   ser imprescindible.
2. **`src/device_gateway.py`** (nuevo): el endpoint WebSocket, el códec del protocolo y la
   sesión del dispositivo. Ensambla las tramas de audio en un `.wav` temporal y llama al
   `stt_engine.transcribe()` que ya existe.
3. **Enganche en `assistant_service`**: un *sink* de audio con **destino configurable en
   caliente** (`pc` | `device` | `both`). `_synth_worker` ya produce arrays numpy que
   `_play_worker` reproduce; basta con permitir un segundo consumidor que los mande al
   dispositivo. Cambio pequeño y limpio en un punto que ya está bien factorizado.
   Se expone como `POST /device/audio-sink` y se refleja en `/status` para que la GUI lo
   pueda conmutar. Por defecto `both` si hay dispositivo conectado, `pc` si no — de modo
   que sin placa el comportamiento actual no cambia ni un ápice.
4. **`/status`**: añadir el estado del dispositivo (conectado, RSSI, batería) para que la
   GUI pueda mostrarlo.

Estimación: ~300–400 líneas de Python. Lo detallaré cuando lleguemos ahí; **no toco el
Python sin avisarte**, y va en su propia rama.

---

## 8. Buenas prácticas Rust que aplicaré

- **`#![deny(unsafe_code)]` en todos los crates** salvo `luka-wakeword`, donde el FFI se
  aísla tras una API segura y documentada.
- **Errores:** tipos concretos por crate (`thiserror`), `anyhow` solo en el binario. Nada
  de `unwrap()`/`expect()` fuera de la inicialización — y en la inicialización, con mensaje
  explicando la invariante.
- **Sin pánicos en régimen permanente.** `panic = "abort"` + *task watchdog* → reinicio con
  la causa registrada en NVS y volcada por serie al arrancar.
- **Drivers sobre `embedded-hal`**, no sobre tipos del IDF → testeables en el host.
- **Newtypes y unidades explícitas**: `SampleRate(u32)`, `Millis(u32)`, `Rssi(i8)`. Nada de
  `u32` sueltos cruzando fronteras de API.
- **Sin asignaciones en la ruta de audio**: todo preasignado en el arranque; `heapless` en
  las estructuras de tiempo real.
- **Canales acotados siempre**, con política explícita de descarte. Un canal sin límite en
  un dispositivo empotrado es una fuga de memoria con pasos extra.
- **`cargo clippy -- -D warnings`, `cargo fmt --check`, `cargo test` (host)** antes de cada
  commit. `cargo deny` para licencias/CVEs.
- **Features:** `wakeword`, `display`, `camera`, `spikes` — para que el binario base no
  cargue con lo que no usa.
- **Configuración compilada**: `toml-cfg` lee `cfg.toml`. Un solo sitio para credenciales.
- **Logs**: `log` + `EspLogger`, niveles por módulo, y **nunca** el PSK ni el token en claro.

---

## 9. Fases

### Fase 0 — Spikes de hardware *(medio día)*
Programas independientes y desechables, uno por incógnita. Nada de integrar todavía.
1. `spike-i2c-scan` — confirma SDA/SCL y las 4 direcciones esperadas.
2. `spike-rgb` — 7 LEDs por RMT (feedback visual inmediato, útil para depurar el resto).
3. `spike-i2s-loopback` — ES7210 → ES8311: hablar al micro y oírse por el altavoz. Valida
   codecs, pines, relojes y el enable del amplificador de una tacada.
4. `spike-wifi` — asociación a la WiFi de `cfg.toml` + alcance TCP al PC.

**Criterio de salida:** los 4 pasan y el mapa de pines de §2.2 queda confirmado o corregido.

### Fase 1 — Satélite funcional con botón *(2–3 sesiones)*
Botón → grabar → WebSocket → Luka responde → suena por el altavoz. LEDs de estado. Sin ML.
**Criterio de salida:** pulsar el botón, decir "¿qué hora es?" y oír a Luka contestar por
el altavoz de la placa.

### Fase 2 — Robustez *(1–2 sesiones)*
Reconexión con *backoff*, watchdog, *jitter buffer*, mDNS, OTA por WiFi (para no depender
del cable), telemetría de batería/RSSI, ajuste de latencia.

### Fase 3 — Wake word *(en curso)*
**Decidido: opción A** (modelo en el dispositivo) y la palabra es **"Luka" a secas**. La
variante "Oye Luka" que este plan recomendaba queda descartada por decisión del usuario:
es la palabra que va a decir él, y la elige él. El coste —una palabra corta dispara más
de la cuenta— se paga en el corpus (negativos adversarios en español) y en el umbral, no
cambiando la palabra.

Estado y detalle en [`PROGRESO.md`](PROGRESO.md) y [`wakeword/README.md`](wakeword/README.md).
Dos correcciones a lo que este plan daba por hecho:

- **El FFI no hizo falta.** ESPHome publica el intérprete y el frontend como componentes
  gestionados del ESP-IDF (`esp-tflite-micro`, `esp-nn`, `esp-micro-speech-features`), así
  que sobre ellos solo queda un shim en C y cinco `extern "C"`. El "trozo de más riesgo
  del firmware" resultó ser el trozo más rutinario.
- **El generador de voces de microWakeWord es solo inglés** (904 hablantes, sin
  equivalente en español). El corpus se hace con las 8 voces españolas de Piper y la
  variedad la aporta la augmentación.

Además de lo previsto —acuse visual, umbral configurable y modo calibración en el
anillo— hizo falta algo que el plan no contemplaba: **el turno se cierra por silencio**,
porque con la wake word no hay botón que soltar. Solo se aplica a los turnos que abrió la
palabra (`hands_free` en `Listening`).

**Criterio de salida:** la palabra dispara la captura desde 3 metros, y una tarde de
conversación normal en la sala no produce falsos positivos molestos.

### Fase 4 — Extras *(opcional)*
AEC con canal de referencia del ES7210 (permite interrumpir a Luka hablando), pantalla SPI,
cámara DVP → `analyze_screen` desde la placa, suspensión y gestión de batería.

---

## 10. Riesgos

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Pines de §2.2 equivocados | Bloqueante | Fase 0 lo resuelve el primer día; el escaneo I²C es concluyente |
| Secuencia de init del ES8311 (se queda en *power-down* si el orden falla) | Alto | Replicar exactamente la secuencia C conocida; hay precedentes documentados en Rust |
| FFI de TFLite Micro | Alto (solo Fase 3) | Aislado en su crate; opción B como plan de repliegue sin tirar nada |
| Falsos positivos de "Luka" (palabra corta) | Medio | Entrenar "Oye Luka"; umbral ajustable; confirmación por VAD |
| Exponer la API a la LAN | Medio | `API_TOKEN` obligatorio, *bind* a la IP de la LAN y no a `0.0.0.0` si prefieres, y regla de firewall |
| Build de ESP-IDF lento la primera vez | Bajo | `ccache`; solo pasa una vez |
| PSK de tu WiFi en un repo público | Alto | **Resuelto:** `cfg.toml` gitignoreado (§11) |

---

## 11. Decisiones tomadas

| # | Decisión | Elegido |
|---|---|---|
| 1 | **Wake word** | **Se define en la Fase 3.** Fases 0–2 con botón. `luka-audio` ya lleva VAD + pre-roll y un `trait Detector` para no cerrar ninguna puerta (§3) |
| 2 | **Salida de voz** | **Ambos, configurable** en caliente: `pc` \| `device` \| `both` (§7.3) |
| 3 | **Credenciales WiFi** | **`firmware/cfg.toml`, gitignoreado**, con `cfg.toml.example` versionado. Igual de cómodo (compilas y ya) sin publicar el PSK en un repo público |
| 4 | **Ubicación** | `firmware/` en la raíz del repo |
| 5 | **TLS** | **Pinning del certificado** en el firmware (§6) |
| 6 | **Rama de trabajo** | Los cambios del lado Python van en **`master`**, no en una rama aparte |

El SSID, la clave y el `API_TOKEN` van escritos en `cfg.toml`, que está en `.gitignore`
junto con `firmware/target/`. Ni el SSID aparece en ningún archivo versionado: un nombre de
red es geolocalizable en bases de datos públicas de wardriving.

---

*Documento de planificación — creado antes de escribir una sola línea de firmware.*
