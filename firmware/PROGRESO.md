# Progreso — firmware `luka-speaker`

Estado breve de lo realizado. El diseño completo está en [`PLAN.md`](PLAN.md).

## ✅ Fase 0 — CERRADA (2026-08-05)

Los 4 spikes pasan. **Todo el mapa de pines está verificado contra el hardware real**
(`PINOUT_VERIFIED = true`), y la cadena de audio funciona de extremo a extremo.

| Spike | Resultado |
|---|---|
| `spike_i2c_scan` | SDA=11, SCL=10 y **los 4 chips responden** |
| `spike_rgb` | 7 LEDs en GPIO38, gamma suave. **Halló R/G intercambiados** |
| `spike_wifi` | IP `192.168.1.138`, RSSI -64 dBm, **TCP al asistente OK** |
| `spike_i2s_loopback` | **Se oyen los tonos y la voz grabada.** Micro y altavoz OK |

---

## Mapa de pines confirmado

| Función | Pin | Cómo se confirmó |
|---|---|---|
| I²C SDA / SCL | GPIO11 / GPIO10 | responden los 4 chips esperados |
| I²S MCLK / BCLK / WS | GPIO12 / 13 / 14 | suenan los tonos de prueba |
| I²S DIN (micros) | GPIO15 | el nivel sube 4× al hablar |
| I²S DOUT (altavoz) | GPIO16 | suenan los tonos y la voz grabada |
| LEDs RGB (×7) | GPIO38 | contados a ojo |
| `PA_ENABLE` | TCA9555 **P8**, activo alto | sin él no suena nada |
| Botones ×3 | TCA9555 P9/P10/P11, **activos a nivel BAJO** | `spike_buttons` (Fase 1) |

**El mapa de pines está ahora confirmado al completo**: no queda nada en `Reported`.

**Direcciones I²C:** ES8311 `0x18`, TCA9555 `0x20`, ES7210 `0x40`, PCF85063 `0x51`.

**Hardware:** ESP32-S3 rev v0.2, MAC `a0:f2:62:e3:4b:10`, PSRAM **8 MB octal @80 MHz**
(test de memoria OK), flash 16 MB, 240 MHz, ESP-IDF **v5.2.3** (fijada).

---

## Lecciones de la Fase 0

### El bug que costó más: `bclk_div` en el ES8311
El registro `0x06` lleva **`bclk_div - 1`**, no `bclk_div`. Con el valor sin restar, el
codec inicializa sin dar error, `reg00` reporta encendido correctamente… y no sale
absolutamente nada por el altavoz. Se resolvió bajando el driver en C de Espressif y
copiando su aritmética de bits en vez de deducirla. Moraleja: con estos codecs, **el
resumen de la documentación no basta**; hay que leer el código de referencia.

Los registros `0x02`, `0x06` y `0x07` además son **lectura-modificación-escritura**:
escribirlos enteros pisa bits ajenos.

### ⚠️ Acople acústico
La primera versión del spike de audio reproducía el micro por el altavoz **en tiempo
real**. En esta placa están a centímetros: se realimenta y suelta un pitido ensordecedor
en segundos. Hubo que desconectar la placa a la fuerza.

**Rediseño:** ciclo **grabar → pitido → reproducir**, nunca simultáneos. El amplificador
arranca apagado, solo se abre para reproducir y se apaga siempre al salir, incluso ante
error. El acople pasa de "improbable" a **imposible por construcción**.

> **Al reconectar la placa:** ejecuta lo último que se le grabó. Si eso hacía ruido,
> conéctala con **BOOT pulsado** para que entre en modo descarga y no arranque nada.

### El monitor serie y el USB-JTAG
`espflash monitor` a secas se pierde el arranque, porque el puerto se re-enumera al
resetear. Hay que usar `espflash flash --monitor --non-interactive`.

### Diagnósticos mal calibrados
Dos métricas mías no servían y hubo que rehacerlas: el nivel de audio reportaba solo el
**pico** (una muestra saturada lo pegaba al 100 %) y la barra era **lineal** contra el
fondo de escala (el habla ronda -45 dBFS y salía siempre vacía). Ahora: RMS + barra
logarítmica en dBFS.

---

## Componentes construidos

- **`crates/luka-config`** — `cfg.toml` (gitignoreado) → `build.rs` lo parsea en el host →
  `const` vía `env!()`. Cero parsing en el dispositivo; un valor mal puesto rompe el build,
  no la placa. `summary()` loguea sin filtrar la clave ni el token.
- **`crates/luka-board`** — fuente única de verdad: pines, direcciones, líneas del
  expansor, constantes de audio. Cada dato con su `Confidence`. Incluye
  `leds::to_wire()`, el único punto que sabe del cruce R/G.
- **`crates/spikes`** — los 4 programas de la Fase 0, más `spike_buttons` (Fase 1).
- **`docs/codec-registers.md`** — secuencias de init de ES8311 y ES7210 extraídas de los
  drivers en C, con la aritmética de los divisores de reloj.

**Tests en host: 10/10.** Sin pines duplicados, ninguno pisa la PSRAM octal (GPIO26-32),
direcciones I²C válidas y únicas, tramas de audio cuadradas, `to_wire` correcto,
`cfg.toml` sin placeholders.

**Secretos:** `git ls-files | grep` sobre todo lo versionable sale limpio — ni la clave
WiFi, ni el `API_TOKEN`, ni el SSID.

---

## Hallazgos que afectan al plan

1. **`HOST=0.0.0.0` ya estaba puesto.** El punto §7.1 del plan no hace falta.
2. **El servidor habla HTTPS con certificado autofirmado** (`SSL_KEYFILE`/`SSL_CERTFILE`).
   Contradice el supuesto de §6 («v1 sin TLS»). **Decisión pendiente**: empotrar el cert y
   fijarlo (*pinning*), saltarse la verificación, o un puerto sin TLS para el dispositivo.
3. **El nivel de micro es bajo:** el habla da RMS ~185 en crudo (~-45 dBFS). Funcionará
   porque `stt_engine` normaliza con `loudnorm`, pero conviene subir la ganancia del
   ES7210 (ahora ~30 dB) para mejorar la relación señal/ruido antes de mandar audio por
   la red.

---

## ✅ Fase 1 — CRITERIO DE SALIDA ALCANZADO (2026-08-05)

Botón → grabar → WebSocket → Luka responde → suena por el altavoz. LEDs de estado. Sin ML.

**Funciona de extremo a extremo con la placa**: se pulsa el botón, se habla, y Luka
contesta por el altavoz. Verificado con voz real. El servidor transcribió correctamente
«Hola, ¿qué tal? ¿Cómo estás?» y «¿Sabes multiplicar mil por tres mil?», y reporta
`{"connected":true,"name":"luka-speaker"}` de forma estable.

**La calidad del audio también está validada con voz real** (2026-08-05): la primera
versión sonaba entrecortada y perdía el principio de las frases, y tras arreglar los dos
cuellos de botella se oye bien. Ver "Caudal de audio" más abajo. Conforme a
[[feedback-evaluate-audio-with-real-voice]], esto se juzgó hablándole a la placa, no con
audio sintético.

Queda pulir (ver "Pendiente de la Fase 2" al final), no rehacer.

### ✅ Hecho: caudal de audio en los dos sentidos
La voz llegaba a trozos por dos motivos **distintos**, y ninguno se ve leyendo el código:

- **Subida.** El micro entrega tramas de 20 ms y se subían una a una: **50 escrituras TLS
  por segundo** de 640 bytes, cada una con su cabecera de WebSocket, su cifrado y su viaje
  por la pila de red. El enlace no daba abasto y se perdía el principio de las frases (89
  tramas descartadas en un solo turno; algún turno se quedó en "demasiado corto"). Ahora
  se agrupan de cinco en cinco: 10 escrituras por segundo de ~3,2 KB, el mismo tamaño que
  usa el servidor para la bajada. El acumulador **se vacía antes del `END`**, o el último
  trozo de voz se mezclaría con el turno siguiente.
- **Bajada.** El búfer de reproducción era de 2 s, dando por supuesto que el audio llega en
  tiempo real. **No es así:** el servidor suelta la voz tan rápido como la sintetiza, así
  que una respuesta de 15 s llega en un par de segundos mientras el altavoz solo la
  consume a 16 kHz. Ahora son 60 s, que caben porque `CONFIG_SPIRAM_USE_MALLOC` manda lo
  grande a la PSRAM. Y al desbordar se descarta **lo nuevo, no lo viejo**: tirar audio ya
  encolado abre un hueco en mitad de una frase que está sonando.

### ✅ Hecho: TLS con certificado fijado (*pinning*)
- **`scripts/sync-cert.sh`** copia el certificado del asistente a `firmware/certs/`.
  Lo saca del **deploy** (`~/.asistenteia`), no del repo de desarrollo: **tienen
  certificados distintos** (huellas `7F:E3:…` vs `96:28:…`) y fijar el equivocado
  produce un fallo de handshake que no apunta a su causa. El script avisa si el
  asistente no corre desde donde se espera.
- `luka-config` lo empotra en el binario vía `include_str!`, terminado en NUL como exige
  la capa TLS del ESP-IDF, con test que valida el formato.
- `firmware/certs/` va al `.gitignore`: es específico de cada despliegue.
- El certificado caduca en **2036**, así que el pin no expira pronto. Es RSA-4096: el
  handshake no es gratis en el ESP32-S3, pero se paga una vez por conexión (el WebSocket
  es persistente), no por turno de voz.

### ✅ Hecho: el lado Python (commit `63b43a1`, en `master` local)
Ya existe el endpoint que faltaba. Ver `src/device_protocol.py` y `src/device_gateway.py`.

- `/device/ws`: WebSocket con **autenticación propia** — las `Depends(verify_token)` de
  las rutas HTTP **no** se aplican al *upgrade* de WebSocket. Token por cabecera
  `X-API-Token` o por query `?token=`; si no cuadra, cierre con **1008**.
- `/device/status` y `/device/audio-sink` (`pc` | `device` | `both`, en caliente).
- Retrocompatible: sin dispositivo, `audio_target = "pc"` y `audio_sink = None`, así que
  el camino de audio es idéntico al de antes. Al desconectar se restaura `"pc"` pase lo
  que pase, para no dejar al asistente mudo.
- **Formato del enlace:** PCM16LE 16 kHz mono en ambos sentidos. El remuestreo desde los
  24 kHz de Kokoro se hace **en el PC** (`float_to_link_pcm`).
- **`DOWNLINK_CHUNK_SAMPLES = 1600`** → tramas `TTS_AUDIO` de **3201 bytes**. El
  `buffer_size` del cliente WebSocket del firmware tiene que ser mayor que eso o el
  ESP-IDF partirá las tramas y habrá que reensamblarlas. Está acoplado a propósito;
  si se cambia un lado hay que cambiar el otro.

> Gotcha del test del endpoint: el fixture **no** entra en el contexto del `TestClient`,
> porque hacerlo dispara el `lifespan`, que crea un `AppState` real y pisa el mock
> (`src/main.py:301`). De paso el fichero baja de 77 s a 0,6 s.

### ✅ Hecho: `spike_buttons` — el mapa de pines queda cerrado
Vigila **las 16 líneas** del expansor en vez de dar por buenas las tres del BSP, para que
si estuvieran en otro sitio saliera ahí y no en medio del firmware.

Resultado: **P9, P10 y P11**, los tres **activos a nivel bajo** (pull-up), sin rebotes
apreciables con sondeo a 20 ms. El BSP acertaba; `expander::CONFIDENCE` pasa a `Verified`
y se añade `BUTTONS_ACTIVE_LOW`.

El spike no toca el audio en absoluto y fuerza el amplificador a apagado al arrancar: por
construcción no puede acoplar.

### ✅ Hecho: los tres crates puros de la Fase 1
Todo lo que se puede probar sin placa, probado sin placa. **56 tests nuevos en el host.**

- **`crates/luka-proto`** (20 tests) — códec del protocolo binario, gemelo de
  `src/device_protocol.py`. `no_std`, **sin dependencias**, sin asignaciones.
  - `Buf<N>` de capacidad fija: si algo no cabe, `as_frame()` devuelve `None` en vez de
    una trama truncada (un JSON a medias lo rechaza el servidor sin explicar por qué).
  - `json_str_field` **no es un parser de JSON**: busca un campo y devuelve su valor sin
    desescapar. Basta porque los únicos campos que consulta el firmware (`state`, `code`)
    son identificadores fijos. Si el valor lleva una barra invertida devuelve `None`:
    antes no entregar nada que entregar una cadena mal desescapada. El texto libre
    (`TRANSCRIPT`, `REPLY`) se loguea en crudo.
- **`crates/luka-state`** (21 tests) — la máquina de estados como **función pura**
  `next(state, event, now_ms) -> (State, Actions)`. El tiempo entra como parámetro: los
  *timeouts* de 15/30/60 s se prueban en microsegundos y de forma determinista.
  - Invariante que persigue el diseño: **nada se queda colgado para siempre**.
  - Invariante de seguridad, con test exhaustivo sobre las 9×13 combinaciones:
    **ninguna transición abre el micro sin haber callado antes el altavoz.**
  - `Fault` tiene el número de parpadeos en el discriminante, así que la tabla de
    diagnóstico del plan y el código no pueden divergir.
- **`crates/luka-ui`** (15 tests) — el anillo como función pura
  `frame(state, t_ms, level) -> [Rgb; 7]`, más `finish()` que aplica brillo global y gamma.
  - Tabla gamma en `const`: aproxima γ≈2,2 mezclando γ=2 y γ=3 (4:1), porque en `const fn`
    no hay coma flotante.
  - Los tests comprueban propiedades, no píxeles: que ningún estado deje el anillo muerto,
    que los estados de espera recorran los 7 LEDs, que el vúmetro sea monótono, y que cada
    fallo parpadee **exactamente** su número contando flancos.

**Dos bugs reales los cazaron los tests exhaustivos, no la revisión:** desbordamiento al
sumar el *backoff* con `now_ms` cerca del máximo del `u64` (ahora `saturating_add`), y
desbordamiento en el cálculo del LED cabeza con *uptime* largo (ahora se reduce el reloj
módulo 1000 s **antes** de multiplicar; ese módulo se eligió porque deja un número entero
de vueltas para cualquier velocidad, así que no produce salto visible).

### 🔨 En curso: `crates/luka-firmware` — el binario
**Escrito:** `Cargo.toml`, `build.rs` y **`src/board.rs`** (I²C compartido, expansor con
amplificador y botones, init de ES7210 y ES8311 portados de `spike_i2s_loopback`).
Cambio respecto al spike: la ganancia de los micros sube de `0x1A` (~30 dB) a `0x1D`
(~34,5 dB), porque la Fase 0 midió el habla a ~-45 dBFS y conviene mejorar la relación
señal/ruido **antes** de que el audio salga por la red, que es donde ya no se arregla.

**Pendiente de escribir (por este orden):**
1. **`src/audio.rs`** — hilo dueño del I²S. **Half-duplex a propósito**: captura *o*
   reproduce, nunca las dos cosas, que es lo que hace imposible el acople. Convierte
   estéreo↔mono y calcula el RMS que alimenta el vúmetro.
2. **`src/net.rs`** — WiFi + cliente WebSocket.
3. **`src/ring.rs`** — hilo de LEDs a 50 fps, prioridad baja, núcleo 0.
4. **`src/main.rs`** — supervisor: recibe eventos por canal, ejecuta `luka_state::next` y
   despacha las `Action`.

**Modelo de concurrencia acordado** (plan §5.2): hilos y canales acotados, nada de async.
Un solo hilo posee el I²S. El bus I²C se comparte con `Arc<Mutex<..>>` entre el hilo de
audio (que abre y cierra el amplificador) y el de botones (sondeo cada 20 ms): por él solo
pasan operaciones cortas y esporádicas, y la ruta de tiempo real es el I²S, que no se
comparte con nadie.

### ⚠️ El cliente WebSocket exige un componente gestionado del ESP-IDF

`esp_idf_svc::ws::client` **no forma parte del ESP-IDF base**. Está detrás de un `cfg`
(`esp_idf_comp_espressif__esp_websocket_client_enabled`), así que sin el componente el
módulo sencillamente no existe y el error que sale es `unresolved import` — que **no
menciona en ningún sitio que falte un componente**. Se declara en el `Cargo.toml` del
binario:

```toml
[package.metadata.esp-idf-sys]
extra_components = [
    { remote_component = { name = "espressif/esp_websocket_client", version = "^1.2" } },
]
```

- Lo descarga el gestor de componentes de Espressif, así que **el primer build necesita red**.
- Cambiar esa metadata **no invalida el build de `esp-idf-sys`**: hay que forzarlo con
  `cargo clean -p esp-idf-sys`, y eso reconstruye el ESP-IDF entero (varios minutos).
- **Y no basta con declararlo.** Este es un workspace *virtual* (el `Cargo.toml` de
  `firmware/` no tiene `[package]`), así que `metadata.root_package()` devuelve `None` y
  esp-idf-sys **ignora en silencio** todos los `[package.metadata.esp-idf-sys]`: solo
  suelta un aviso que se pierde entre el ruido del build. Hay que nombrar el crate raíz
  a mano en `.cargo/config.toml`:

  ```toml
  [env]
  ESP_IDF_SYS_ROOT_CRATE = "luka-firmware"
  ```

  Sin esa línea, el `extra_components` de arriba no hace absolutamente nada y el síntoma
  es idéntico a no haberlo puesto.

### API del cliente WebSocket, ya comprobada en el crate
`esp_idf_svc::ws::client::{EspWebSocketClient, EspWebSocketClientConfig}` (esp-idf-svc
0.52.1). Lo relevante, mirado en el fuente y no supuesto:
- `EspWebSocketClientConfig` tiene `server_cert: Option<X509<'static>>` y
  `skip_cert_common_name_check: bool` → es justo lo que hace falta para el *pinning*
  (el cert del asistente es autofirmado con `CN=localhost`, así que el nombre no se
  puede validar contra una IP de la LAN).
- Cabeceras arbitrarias por `headers: Option<&'a str>` → ahí va `X-API-Token: …\r\n`.
- `EspWebSocketClient::new(uri, &config, timeout, callback)` exige callback `'static`:
  se le pasa un `Sender` propio movido dentro.
- `send(FrameType::Binary(false), datos)`. **Enviar fragmentado hace `panic!`**, igual que
  cerrar a mano (hay que soltar el cliente).
- Los eventos llegan como `WebSocketEventType::{Connected, Disconnected, Binary, Text, …}`.

### Los tres fallos que solo aparecieron con la placa delante

Ninguno lo habría cazado el compilador ni los tests del host. Están documentados en el
código, en el sitio donde muerden.

1. **El anillo salía negro.** `luka_ui::finish` aplicaba el brillo global **antes** de la
   gamma. Parece lo natural ("la mitad de brillo percibido"), pero compone dos
   atenuaciones sobre un `u8`: con el `led_brightness = 48` real, hasta el rojo a plena
   saturación salía a **7/255** y `Booting` a **(0,0,0)** exacto. Ahora la gamma va primero
   y el brillo escala linealmente el PWM: es un **techo de potencia**, no un atenuador
   perceptual. El test que había solo probaba blanco a tope, el único caso que sobrevivía.
2. **Soltar el cliente WebSocket aborta el dispositivo.** Su `Drop` llama a
   `esp_websocket_client_close`, que intenta mandar la trama de cierre; sin conexión
   abierta el componente devuelve `ESP_FAIL` y esp-idf-svc hace `.unwrap()` sobre eso.
   La reconexión automática **no** lo evita (el problema es "no conectado", no "no
   arrancado"). Solución: el cliente se crea una vez y no se suelta nunca; se para y se
   arranca su tarea con `esp_websocket_client_stop`/`start`, que sí es seguro.
3. **El "error" que no era un error.** esp-idf-svc 0.52 traduce con
   `_ => Err(ESP_ERR_INVALID_ARG)` **cualquier id de evento que no conozca**, y el
   componente que se compila (esp_websocket_client **1.8.0**) es más nuevo: emite `BEGIN`,
   `FINISH` y `HEADER_RECEIVED`. O sea que **el primer evento de toda conexión llega como
   "error"**. Tratarlo como caída hacía que el firmware se declarase caído a sí mismo y
   reconectase en bucle cada segundo, con un síntoma que apuntaba a TLS o al token.
   Ahora un `Err` del callback **solo se registra**: las caídas de verdad llegan como
   `Disconnected` o `Closed`.

De paso se cerró un hueco que esto destapó: `ServerConnecting` no tenía plazo, así que un
fallo que no llegara a producir evento de desconexión dejaba el dispositivo girando en
azul para siempre. Ahora sale a los 15 s con backoff.

### Fuera de alcance por decisión del usuario
- **OTA por WiFi: no se hace.** El dispositivo es para uso propio y el cable no molesta.
  Era el trozo más grande y el único capaz de dejar la placa inutilizable.
- **mDNS: no se hace.** Resolvería que la placa encuentre el asistente sola, pero el
  problema real (si el PC cambia de IP hay que regrabar) se arregla mejor con una
  **reserva DHCP en el router**: cero código y cero riesgo. Y no lo resolvería del todo,
  porque el certificado va fijado en el binario: mover el asistente a otra máquina obliga
  a regrabar de todas formas.
- **Telemetría de batería: no se hace.** El dispositivo vive enchufado. Además no sería
  barato: el escaneo I²C de la Fase 0 encontró solo cuatro chips y **no hay medidor de
  carga**, así que habría que leer un divisor por ADC en un pin que no está en el mapa
  verificado — otro spike, no una llamada.

### La lección del anillo: comprobar antes de la última transformación es no comprobar nada

Tres fallos seguidos en el mismo sitio, todos vistos con la placa delante y **ninguno**
detectado por los tests que ya existían:

1. Brillo aplicado **antes** de la gamma → con `led_brightness = 48` todo salía a 0-7/255.
2. `finish` convertía "poca luz" en **negro exacto**: la gamma entera manda a cero todo lo
   que baje de 23, y el faro del reposo pedía entre 4 y 14. **Llevaba apagado desde el
   primer día.** Ahora hay un suelo de 1, que es lo mínimo que el LED sabe encender.
3. Con el suelo se encendía pero **no respiraba**: los once niveles del faro caían todos
   en el mismo escalón de PWM. El rango pasa a 24..92, que la gamma sí separa.

El patrón común: los tests miraban `frame()` a secas, o sea el valor lógico **antes** de
la gamma y del brillo. Daban verde mientras en la placa no se veía nada. Ahora comprueban
lo que sale al bus. Si algún día se añade otra etapa al final de la cadena, los tests
tienen que moverse con ella.

### Pendiente de la Fase 2 (pulido, no rehacer)
- ~~**Reconexión:** la cadencia real la lleva el cliente del ESP-IDF, no el backoff de la
  máquina de estados, por lo del punto 2. Funciona, pero la política está en dos sitios.~~ **Hecho** (con `disable_auto_reconnect: true` la FSM recupera el control).
- ~~**La respuesta llega con el `<think>` del modelo dentro.** Se ve en el log del
  dispositivo (`← {"text":"<think>…"}`). Al altavoz no le afecta, pero si algún día hay
  pantalla habrá que limpiarlo en el servidor.~~ **Hecho** (se eliminan las etiquetas con regex en `device_gateway.py`).
- Verificar cuánto aguanta el enlace en horas, y el consumo.
- ~~El anillo de LEDs no se ha comprobado a ojo.~~ **Hecho, y encontró dos fallos más**
  (ver abajo). Vúmetro cian y faro del reposo confirmados en la placa.

## 🔨 Fase 3 — Wake word "Luka" (en curso, 2026-08-05)

**Decisión tomada:** opción A del plan, modelo en el dispositivo. Y la palabra es
**"Luka" a secas**, no "Oye Luka": es la que se va a decir treinta veces al día y la
elige quien la dice. El coste de esa elección es real —dos sílabas cortas disparan más
de la cuenta— y se paga en el corpus y en el umbral, no cambiando la palabra.

### Lo que ya está construido

| Pieza | Estado |
|---|---|
| Entorno de entrenamiento aislado (2 venvs, GPU) | ✅ `firmware/wakeword/preparar_entorno.sh` |
| Corpus: 4.000 "Luka" + 3.600 palabras vecinas | ✅ 8 voces españolas de Piper |
| Datasets negativos y fondos (ruido, música, impulsos) | ✅ ~10 GB fuera del repo |
| Componente C `luka_ww`: frontend + TFLite Micro | ✅ **compila para el S3** |
| Crate `luka-wakeword` (FFI + política de decisión) | ✅ 8 tests en el host |
| `Event::WakeDetected` / `SilenceDetected` en la FSM | ✅ 27 tests |
| Hilo `detect`: pre-roll, silencio, modos | ✅ compila y enlaza |
| Modo calibración del anillo | ✅ `luka_ui::calibration` |
| Modelo entrenado | ✅ `luka.tflite` empotrado |
| Prueba de campo | ⏳ requiere al usuario |

### El riesgo grande resultó no serlo

El plan daba el FFI de TFLite Micro como "el trozo de más riesgo de todo el firmware":
~300 líneas de *glue* con `bindgen`. No hizo falta nada de eso. ESPHome empaqueta las
tres piezas como **componentes gestionados del ESP-IDF** y se declaran igual que el
cliente WebSocket, en `luka-firmware/Cargo.toml`:

- `espressif/esp-tflite-micro` — el intérprete.
- `espressif/esp-nn` — sus kernels para el S3. Sin él la inferencia no llega a tiempo.
- `esphome/esp-micro-speech-features` — el frontend de espectrograma.

Encima queda un shim en C de ~250 líneas (`components/luka_ww/`) y cinco `extern "C"` en
Rust. Sin `bindgen`, y el `unsafe` cabe en una pantalla.

**Lo que sí es delicado:** las constantes del frontend (40 bandas, ventana de 30 ms,
suavizado del ruido, desplazamiento del logaritmo) tienen que coincidir **exactamente**
con las del entrenamiento. Si se cambia una, el modelo recibe características que no
vio nunca y **no dispara jamás, sin ningún error en el log**. Están replicadas literales
en `luka_ww.cc` con un comentario que lo advierte.

### El generador de voces de microWakeWord es solo inglés

Es el hallazgo que más condiciona la fase. Los positivos del flujo estándar salen de un
checkpoint LibriTTS-R con **904 hablantes**; no hay equivalente en español. Con 8 voces
españolas, la variedad tiene que salir de la augmentación (reverberación de salas
reales, ruido entre -5 y +10 dB de SNR, tono, EQ). Detalle en
[`wakeword/README.md`](wakeword/README.md).

### Lo que no tiene arreglo por entrenamiento

En español **"Luca" se pronuncia exactamente igual que "Luka"**. Si alguien en la sala
se llama así, la placa despertará. "Lucas" y "Lucía" sí se distinguen (hay sonido
después) y están entre los negativos adversarios.

### Sin botón que soltar, el turno lo cierra el silencio

La wake word abre el turno, pero no hay nada que soltar para cerrarlo. Se añadió
`SilenceDetected` (1,2 s por debajo del nivel de voz) y una marca `hands_free` en el
estado `Listening`: **el silencio solo cierra los turnos que abrió la palabra**. Sin esa
distinción, callarte un momento mientras piensas qué decir te cortaría también los
turnos de botón, que es justo lo contrario de lo que quiere quien tiene el dedo puesto.

### El pre-roll no es un lujo

Cuando el detector dice "Luka", la palabra **ya se ha dicho**: el modelo necesita oírla
entera. Grabar a partir de ese instante manda al servidor una frase que empieza por la
mitad. El hilo `detect` guarda el último segundo en un anillo y lo vuelca al despertar.

### Trampas del entrenamiento (todas costaron una vuelta)

- **Las libs de CUDA viven dentro de los venvs.** Sin `LD_LIBRARY_PATH` ni PyTorch ni
  TensorFlow las encuentran y entrenan en CPU **sin avisar**.
- **La GPU la comparte el propio asistente.** TabbyAPI tiene ~5 GB de los 8, y
  TensorFlow por defecto reserva de golpe casi toda la memoria libre; la evaluación del
  set ambiente copia ~1 GB y revienta con `Dst tensor is not initialized`, que no
  menciona la memoria por ningún lado. Se arregla con `TF_FORCE_GPU_ALLOW_GROWTH`.
- **`datasets` ≥4 exige `torchcodec`**, que arrastra PyTorch entero al venv de
  TensorFlow. Anclado a la serie 3.
- **HuggingFace devuelve 200 con 15 bytes de JSON** cuando una ruta ya no existe (le
  pasó a AudioSet). El script comprueba tamaños.
- **Un directorio creado y vacío no es un paso hecho.** La comprobación de "ya está" de
  las features miraba la carpeta, no los datos, y el corpus positivo se saltó entero en
  silencio.

## Puesta en marcha del lado Python (leer antes de probar la placa)

El código ya está en `origin/master`, así que `asistenteia update` se lo lleva al deploy
(`~/.asistenteia`). Pero **estar en el disco no basta** para que la placa pueda conectar:

1. **`pip install websockets`** (ya está en `requirements.txt`). Uvicorn necesita una
   implementación de WebSocket para `/device/ws`; sin ella el endpoint **no llega ni a
   negociar**, y el error que se ve desde el dispositivo no menciona la dependencia.
2. **Reiniciar `asistenteia.service`**, que lo hace **el usuario**, no el agente. Hasta el
   reinicio, el proceso en marcha sigue sin tener el endpoint por mucho que el fichero
   esté actualizado.
3. Comprobar desde el PC antes de culpar al firmware:
   ```bash
   curl -k -H "X-API-Token: $API_TOKEN" https://localhost:8765/device/status
   # -> {"device":{"connected":false},"audio_target":"pc"}
   ```
   Si eso no contesta, el problema no está en la placa.

### ⚠️ El certificado fijado se rompe solo si se regeneran los certificados

El firmware **solo acepta el certificado exacto** que lleva empotrado. Es una garantía más
fuerte que la validación normal, pero tiene el precio de que el pin caduca cuando el
certificado cambia:

- Si el deploy regenera `SSL_CERTFILE` (al actualizar, al reinstalar, o a mano), la placa
  deja de conectar de golpe. El síntoma es un **fallo de handshake TLS que no dice en
  ningún sitio que la causa sea el certificado**.
- Arreglo: volver a ejecutar **`firmware/scripts/sync-cert.sh`** y **reflashear**. No hay
  atajo: el certificado va dentro del binario.
- El certificado actual caduca en **2036**, así que por caducidad no va a pasar.
- Recordatorio de por qué el script mira `~/.asistenteia` y no el repo de desarrollo:
  **tienen certificados distintos** (`7F:E3:…` vs `96:28:…`) y fijar el equivocado produce
  exactamente ese mismo fallo de handshake mudo.

**Decisión pendiente:** la wake word (Fase 3, según lo acordado).

**Pendiente menor:** `ccache` sin instalar (`sudo pacman -S ccache`);
`ws2812-esp32-rmt-driver` usa la API RMT legacy (aviso de deprecación, cosa del crate).

---

## Cómo ejecutar

```bash
# Tests en host — DESDE LA RAÍZ DEL REPO (esquiva la config de Xtensa)
for c in luka-board luka-config luka-proto luka-state luka-ui; do
    cargo test --manifest-path firmware/crates/$c/Cargo.toml
done

# Spikes — desde firmware/, con la placa en /dev/ttyACM0
cd firmware
cargo build -p spikes --bin spike_buttons
espflash flash --chip esp32s3 --port /dev/ttyACM0 --monitor --non-interactive \
    target/xtensa-esp32s3-espidf/debug/spike_buttons
```

**Gotchas al grabar:**
- `espflash monitor` a secas se pierde el arranque; usa `flash --monitor --non-interactive`.
- El monitor **retiene `/dev/ttyACM0`**: hay que matarlo antes de volver a grabar o sale
  `Device or resource busy`, que no dice en ningún momento que la culpa sea del monitor.
- Los crates `no_std` con tests que usen `Vec` necesitan `extern crate std;` dentro de
  `mod tests` (el crate es `no_std`, pero los tests corren en el PC).

## Estado del control de versiones

Rama `master`, **ya en `origin/master`** (2026-08-05). Tres commits:
- `bf2d950` — firmware: Fase 0 cerrada + TLS con cert fijado.
- `63b43a1` — lado Python: `/device/ws` y salida de audio configurable.
- `04ab322` — Fase 1: crates puros, botones verificados, esqueleto del binario.

Árbol limpio; no queda nada sin commitear.

Recuerda: **el deploy sale de `origin/master`**, así que nada llega a `~/.asistenteia`
hasta que se empuje. El reinicio del servicio lo hace el usuario, no el agente. Ver
"Puesta en marcha del lado Python" más arriba.

**Antes de cada push**, comprobar que no se cuela nada de `cfg.toml` (el repo es público):

```bash
git diff origin/master..master | grep -F "$(grep -oP '(?<=^password = ")[^"]+' firmware/cfg.toml)"
git diff --name-only origin/master..master | grep -E 'cfg\.toml$|certs/|\.env'
```

Ambos deben salir vacíos. Ojo: no basta con mirar la contraseña — **el SSID tampoco debe
aparecer** en nada versionado.

---

## ✅ Fase 4 — Conversación encadenada (2026-08-06)

Se pidieron tres cosas: interrumpir a Luka mientras habla, contestarle sin repetir la
palabra, y que narre las tareas largas. **Se entregó la segunda**; la primera se probó,
se midió y se apagó; la tercera no se ha empezado.

### Lo que funciona

| | Estado |
|---|---|
| Oír de lejos (umbral 150, PGA al tope) | ✅ verificado con voz real |
| El turno se cierra al callarte (~0,6 s) | ✅ |
| Encadenar una segunda pregunta sin decir "Luka" | ✅ ventana de 3,5 s |
| El anillo avisa de que el micro sigue atento | ✅ cian tenue, respirando |
| Interrumpir por voz mientras habla (barge-in) | ⛔ **apagado**, `barge_in = 0` |
| Narrar tareas agénticas largas | ⏳ sin empezar |

**Tests en host: 96.**

### Los cuatro fallos del día, y qué los hacía caros

Ninguno daba un error. Los cuatro se manifestaban como "la placa va rara", y los cuatro
se cerraron con **una traza que convirtió una impresión en un número**. Es el patrón que
conviene repetir.

**1. El umbral de silencio absoluto.** `SILENCIO_NIVEL = 40` era un nivel fijo. Al subir
el PGA del ES7210 al tope, el suelo de ruido de una sala normal (medido: **40-53**) se
puso por encima, y la condición dejó de cumplirse **nunca**. Ningún turno se cerraba por
callarse; todos agotaban los 15 s de `LISTENING_TIMEOUT_MS`. Desde fuera: "tarda quince
segundos en contestar". Ahora el umbral es relativo al suelo medido en reposo.

> Un umbral absoluto contra una señal cuya ganancia se toca es una bomba de relojería.

**2. El detector le robaba la CPU a la red.** Con barge-in, el hilo `detect` corría con
la prioridad por defecto de pthreads (5) — **la misma que la tarea del cliente
WebSocket**. El enlace moría a los ~25 s de cualquier respuesta larga con
`Could not lock ws-client within 1000 timeout`, que **no menciona la CPU por ningún
lado**. Se aisló apagando `barge_in` y dejando todo lo demás igual. Arreglado con
prioridad 4 y anclando el hilo al núcleo 1 (WiFi/lwIP viven en el 0).

> Añadir trabajo continuo en un estado donde antes no había ninguno no es un cambio de
> una línea, aunque la línea sea `if !playing || BARGE_IN != 0`.

**3. La sesión vieja dejaba muda a la nueva.** Al reiniciarse la placa, su sesión nueva
abría el WebSocket **antes** de que el servidor procesara el cierre de la vieja, y el
`finally` de la vieja llamaba a `detach()` sin comprobar de quién era: borraba el
`audio_sink` de la sesión viva. A partir de ahí, mudo para siempre y **sin un solo error
en ningún log** — el turno se transcribe, el LLM contesta, el TTS sintetiza y reproduce;
solo que el destino ya no existe. Lo destapó `playback: cerrando (0 muestras con señal)`.

> Cada `espflash flash` y cada `espflash monitor` **reinician la placa**. Eso disparó esta
> carrera una docena de veces mientras se buscaba otra cosa, y arruinó tres mediciones.

**4. El instrumento contaba el rebote del disparo.** La primera medición de barge-in casi
descarta la función: los picos que parecían "Luka oyéndose a sí misma" eran la cola de la
interrupción del usuario medio segundo después, porque al disparar se limpia la ventana
pero las probabilidades altas de esa misma palabra la vuelven a llenar. Leído mal, el
margen parecía de 9 puntos sobre 255; excluyendo el refractario, ~80.

> Antes de creerse una medición, comprobar que el instrumento no está midiendo su propio
> eco.

### Por qué barge-in está apagado

No es el acople de la Fase 0 y conviene no confundirlos: aquello era un **lazo cerrado**
(micro → altavoz → micro) que diverge; en barge-in lo capturado va al detector y **se
tira**. No hay realimentación posible.

Lo que sí hay es coste. Aun con la prioridad arreglada, el audio salía entrecortado:
`playback: cerrando (71048 muestras con señal)` sobre 203.680 huecos escritos. Con
`barge_in = 0` suena limpio. **El diagnóstico es firme porque se hizo el bisect**, no por
deducción.

Para retomarlo hace falta: procesar **una trama de cada dos**, **no reservar memoria** en
el hilo de audio (hoy `mono_and_level` pide un `Vec` cada 20 ms dentro del bucle de tiempo
real), y un criterio de salida explícito — si suben los subdesbordamientos, no entra.
Interrumpir es un lujo; que Luka suene bien, no.

### Lo que queda

- **Narrar tareas largas.** Bloqueado por un detalle concreto: `(Idle, TtsStarted)` y
  `(Idle, ServerSaid(Speaking))` **no existen** en la FSM, así que hoy el servidor no
  puede hablar por iniciativa propia — el audio llegaría y se tiraría sin abrir el
  amplificador. Con esa transición y la ventana de seguimiento ya hecha, el transporte
  está resuelto; falta decidir **dónde** narra el servidor (redacción de documento,
  segunda pasada de visión, y el `sleep(3.0)` de la ruta de terminal).
- **Colchón antes de reproducir.** Aparcado: con `barge_in = 0` el audio va bien. Se
  retoma si vuelven los cortes.

---

## ⛔ Barge-in — APARCADO por decisión (2026-08-06)

Se intentó dos veces y se para aquí. **No por no saber hacerlo**: oír con el altavoz
sonando funcionaba, y el riesgo de acople estaba descartado por diseño (el micro va al
detector y se tira; no hay lazo). Se para porque **cuesta más CPU de la que este chip
tiene libre** mientras además baja 32 KB/s de TLS y mueve el I²S full-duplex.

`barge_in = 0` en `cfg.toml`. El código sigue en su sitio y la palanca tiene tres
posiciones (0 apagado / 1 solo medir / 2 activo), así que retomarlo no exige revertir
nada.

**Lo que se descartó por el camino, para no repetirlo:**

- **El arena de TFLite NO está en PSRAM.** Se comprobó: `arena=25548/65536 B (interna)`.
  Esa hipótesis —que la inferencia robara ancho de banda del bus PSRAM a la cola de
  reproducción y a mbedtls— era buena, y era falsa. De paso: pide 64 kB y usa 25,5.
- **Decimar tramas ("una de cada dos") NO es una opción**, aunque aparezca como idea en
  notas anteriores. El frontend de microWakeWord tiene estado y espera señal continua;
  con la mitad del audio recibe características que el modelo no vio nunca y **no
  dispararía jamás, sin un solo error en el log**.

**Lo que quedaría por hacer si se retoma**, por orden:
1. Quitar la reserva de memoria del hilo de tiempo real (`mono_and_level` pide un `Vec`
   cada 20 ms dentro del bucle que escribe al I²S, y lo libera otro núcleo; el montón del
   ESP-IDF tiene cerrojo global y compite con lwIP/mbedtls). Fondo de búferes reutilizables.
2. Volver a medir contra una línea de base limpia.
3. Solo si no basta: separar frontend e intérprete en `luka_ww.cc` y ejecutar el
   intérprete **solo cuando el nivel del micro supera el eco de la propia Luka**, umbral
   aprendido durante el periodo de gracia. Bajaría el ciclo de trabajo a casi cero y de
   paso impediría que Luka se dispare a sí misma.

## 🔎 El TTS del PC va por debajo de tiempo real (sin arreglar)

Buscando la línea de base de barge-in salió un problema mayor y **ajeno al dispositivo**:
la reproducción sufre cortes aunque barge-in esté apagado.

Medido en la placa: `354455 con señal, 263600 inventadas en 12 cortes` en una
reproducción de 42,8 s. **16,5 segundos de silencio** que el hilo de audio tuvo que
inventar porque la cola llegó vacía.

La causa está en `src/assistant_service.py:273`:

```python
if len(text_buffer) > 80:
    pattern = re.compile(r'([.!?:])(?=\s|$)|(\n)|,(?=\s)')
```

En cuanto el buffer pasa de 80 caracteres parte **por todas las comas y dos puntos a la
vez**, no solo lo justo para soltar un trozo, y la respuesta acaba hecha astillas. Kokoro
tiene un coste fijo por llamada de ~1 s, así que:

| Fragmento | Habla | Tarda | Ritmo |
|---|---|---|---|
| 73 caracteres | ~4 s | 2 s | **2× tiempo real** |
| 18 caracteres | ~1 s | 1 s | **1× tiempo real** |

Con astillas el coste fijo **es** el fragmento. El altavoz consume a tiempo real sin
parar, así que cualquier hipo abre un hueco.

**Arreglo propuesto:** tamaño mínimo de fragmento (~120-150 caracteres) antes de
sintetizar. Precio: ~1 s más hasta que Luka empieza a hablar, que es justo lo que el corte
por comas intentaba comprar y compró demasiado caro. Un colchón en la placa sería el
segundo paso, y probablemente sobre: a 2× tiempo real la cola no se seca.

---

## ✅ El audio entrecortado — RESUELTO (2026-08-06)

La reproducción se cortaba a trozos mientras Luka hablaba, con barge-in ya apagado.

| | Antes | Después |
|---|---|---|
| Silencio inventado por cola vacía | 263.600 muestras (16,5 s) | 5.600 (0,35 s) |
| Cortes | 12 | 3 |
| Audio recibido y tirado al final | 41.520 (2,6 s) | 0 |

### Causa: no había colchón, y el servidor mandaba sobre el altavoz

`net.rs` emitía `TtsStarted` con la **primera** trama `TTS_AUDIO`, así que la
reproducción arrancaba con 20 ms de audio. A partir de ahí el altavoz consume
16.000 muestras por segundo sin descanso mientras la fuente entrega a ráfagas:
cualquier bajón vaciaba la cola y se oía un corte. Ahora se acumulan **1,5 s**
antes de arrancar (`COLCHON_SAMPLES`), o hasta que llegue `TTS_END` si la
respuesta es tan corta que no da para tanto.

Pero el colchón por sí solo **no hacía nada**, y el log lo dejó claro
(`playback: abriendo (cola 0 muestras)`): la máquina de estados se fiaba de dos
tramas de estado del servidor que describen lo que hace **el servidor**, no lo
que suena en la placa.

- `STATE_SPEAKING` se manda **antes** de sintetizar una sola muestra, y abría el
  altavoz con la cola vacía. Anulaba el colchón entero.
- `STATE_IDLE` llega pegado al `TTS_END`, cuando al dispositivo aún le quedan
  segundos por sonar. **Cortaba el final de cada respuesta**, y esto ya pasaba
  antes del colchón: 2,6 s de audio ya recibido a la basura, en silencio.

Ahora `Speaking` se entra **solo** con `TtsStarted` (que la red retiene hasta
tener colchón) y se sale **solo** con `TtsEnded`, que manda el supervisor cuando
la cola se vacía de verdad. `SPEAKING_TIMEOUT_MS` sigue de guardia.

> **La regla:** el servidor no decide cuándo se abre ni cuándo se cierra el
> altavoz. Lo decide el audio que hay en la cola.

### Lo que costó encontrarlo

Tres diagnósticos equivocados antes del bueno, todos por deducir en vez de medir:

1. «Es la inferencia de barge-in» — lo era en parte (prioridad del hilo), pero
   quedaban cortes con barge-in apagado.
2. «Es el TTS del PC, va por debajo de tiempo real» — solo con enumeraciones
   (`'Cuarto, expulsión:'`, 18 caracteres); con prosa va a ~2×.
3. «Es el contador» — el contador medía muestras no nulas, y el silencio natural
   entre palabras también son ceros.

Lo que lo cerró fue separar **huecos** (muestras inventadas por cola vacía) de
**cortes** (cuántas veces se secó). Un corte largo y cien cortos dan el mismo
total de muestras y no suenan igual.

### Pendiente relacionado, en el PC

`src/assistant_service.py:273` parte por **todas** las comas y dos puntos en
cuanto el buffer pasa de 80 caracteres. Con enumeraciones salen fragmentos de 18
caracteres y Kokoro tiene ~1 s de coste fijo por llamada, así que el ritmo cae a
1× tiempo real. Con el colchón ya no se oye, pero sigue ahí. Arreglo: tamaño
mínimo de fragmento (~120 caracteres).

---

## ⛔ Barge-in — DESCARTADO, con números (2026-08-06)

Segundo y último intento, esta vez con el colchón de reproducción ya puesto. La
primera vez se aparcó "porque le cuesta al chip"; ahora hay medida.

Misma placa, misma pregunta, servidor sano en las dos (comprobado en
`journalctl`), normalizado porque las respuestas duran distinto:

| | `barge_in = 0` | `barge_in = 1` |
|---|---|---|
| Reproducción | 55,6 s | 17,5 s |
| Silencio inventado | 5.600 | 70.080 |
| Cortes | 3 | 14 |
| **Huecos por segundo** | **101** | **4.005** (40×) |
| **Cortes por segundo** | **0,05** | **0,80** (15×) |

El **25 %** de la reproducción con barge-in fue silencio inventado. El colchón
ayudó —arrancó con 24.000 muestras encoladas y no se descartó nada al final—
pero absorbe **tropiezos**, no un **déficit sostenido**: la inferencia son 10 ms
de cada 30 y eso vacía 1,5 s de holgura sin despeinarse.

### El hallazgo que sí merece guardarse: el problema NO es acústico

En 17,5 s de reproducción, pasado el calentamiento y **con la placa como única
fuente de sonido**, no salió **ni una sola línea `eco:`**. Es decir: la voz de
Luka nunca superó confianza 60, con el umbral de barge-in en 220.

**Luka no se dispararía a sí misma.** El miedo de partida —que despertase
oyéndose decir su propio nombre— resultó infundado, y con él se cae también la
sospecha del acople: el micro va al detector y se tira, no hay lazo.

Todo el obstáculo es CPU. Si algún día sobra, barge-in funciona.

### Si alguien lo retoma

Lo único que cerraría una brecha de 40× es **no ejecutar el intérprete salvo
cuando alguien habla encima**: partir `luka_ww.cc` para alimentar el frontend
siempre (tiene estado, no se le puede dejar de dar señal) y correr el intérprete
solo por encima del nivel de eco, aprendido durante el periodo de gracia.
Acústicamente está demostrado que funcionaría. Lo que **no está medido** es si el
frontend solo es lo bastante barato.

Descartado por decisión, no por falta de camino.

**No sirve**, aunque aparezca en notas viejas: procesar una trama de cada dos.
El frontend de microWakeWord espera señal continua; con la mitad del audio
recibe características que el modelo no vio nunca y **no dispararía jamás, sin
ningún error en el log**.

El código y la palanca de tres posiciones (0/1/2) se quedan: retomarlo no exige
revertir nada, y el modo 1 es el instrumento con el que se midió esto.

---

## ✅ Cámara GC0308 — funcionando (2026-08-06)

Pedirle a Luka que mire por la cámara del dispositivo y que enseñe la foto en el
PC. Funciona de extremo a extremo.

| | |
|---|---|
| Sensor identificado y despierto | ✅ GC0308, `0x21`, ID `0x9B` |
| Interfaz DVP (D0-D7, PCLK, VSYNC, HREF) | ✅ verificado capturando |
| Captura + JPEG en la placa | ✅ **640x480** (el máximo del sensor), ~12-19 kB |
| Subida al PC (`IMAGE` 0x07) | ✅ entera en una trama |
| Petición desde el servidor (`CAPTURE` 0x88) | ✅ |
| Tools de voz | ✅ `analyze_camera`, `show_camera_photo` |

### Pinout (de un port de terceros, verificado con spike)

XCLK 43, PCLK 44, VSYNC 21, HREF 1, D0-D7 = 2, 17, 18, 39, 45, 46, 47, 48.
SCCB en el bus I²C que ya existe (11/10). Control por el **TCA9555**:
`power_down` P5, `camera_select` P6 (**activo ALTO**), `hardware_reset` P7.

### Las cuatro trampas, todas con errores que no mencionaban la causa

**1. No contestaba por SCCB.** Faltaban dos cosas a la vez: reloj en XCLK y
sacarla de reset por el expansor, que arranca con todo como entradas. Y
`camera_select` resultó ser activo a nivel ALTO, cosa que no documenta nadie:
salió de barrer las cuatro combinaciones de polaridad, cuatro sondeos de 100 ms.

**2. `esp_camera_init` fallaba con `ESP_FAIL` pelado.** El reset hay que soltarlo
**con XCLK ya corriendo**. El spike acertaba por accidente, porque generaba el
reloj para poder hablar por SCCB.

**3. Sin memoria.** El driver pide 30 kB **contiguos de interna con capacidad
DMA**, que no se sirven desde PSRAM:

    cam_dma_config: DMA buffer 30720 Byte malloc failed,
    the current largest free block: 11264 Byte

Hay que inicializar la cámara **antes que la red**. Y luego la WiFi se quedó sin
sus búferes (`Expected to init 10 rx buffer, actual is 4`), que se arregló
bajando el arena del wake word de 64 kB a 40 — pedía 64 de **interna** y usa
25,5.

**4. La cámara dejaba mudo al micrófono.** El peor de todos. Su SCCB cuelga del
mismo bus que el ES7210, y el driver abría **su propio bus** sobre esos pines. Al
abrir turno el firmware toca el I²C —cierra el amplificador por si acaso— y ahí
se corrompía el ADC de los micros.

El síntoma despistaba muchísimo: **"Luka" SÍ se transcribía** —venía del
pre-roll, grabado antes de abrir el turno— y a partir de ahí solo silencio. Se
persiguió el STT, el umbral de silencio y hasta se propuso cambiar de motor de
transcripción. Lo cerró un bisect: cámara fuera, todo lo demás igual.

> Arreglo: `pin_sccb_sda/scl = -1` y `sccb_i2c_port = 0`. "No abras un bus, usa
> el que ya hay."

### Y dos del lado servidor

**El modelo escribía mal el nombre de la tool.** `mirar_camara` salía como
`call:mirarara`, se comía el `_cam`, la llamada no parseaba y el modelo
improvisaba que no tiene ojos. Causa: nombres en español teniendo las otras
veinte en inglés. **Un nombre que rompe el patrón del catálogo es un nombre que
el modelo escribe mal.** Ahora `analyze_camera` y `show_camera_photo`.

**"Enséñamela" llega en un turno nuevo**, y lo único que el modelo conserva del
anterior es el texto que devolvió la tool. Si ese texto no dice que la foto sigue
guardada, contesta que no puede mostrarla. Las tools de este proyecto se cruzan
entre sí a propósito; las de cámara también, ahora.

### 5. VGA: el diagnóstico fácil era el equivocado

A 640x480 el driver rechazaba cada fotograma (`FB-SIZE: 599040 != 614400`) y se
apuntó que era **la ventana de salida del GC0308**. Falso. El log lo decía en
otra línea que no se miró: `EV-EOF-OVF`, o sea "llegan más rápido de lo que
puedo copiarlos".

El arreglo es bajar XCLK, no bajar resolución. En el S3 se divide de los 80 MHz
del APB, así que solo valen divisores exactos:

| XCLK | Resultado |
|---|---|
| 20 MHz | `FB-SIZE` corto, fotogramas rechazados |
| 16 MHz | tamaño correcto pero **mitad inferior corrupta** |
| **10 MHz** | limpio |

**Los 16 MHz son la lección que hay que guardar**: el log daba la captura por
buena —tamaño correcto, cabecera JPEG válida, todos los indicadores en verde— y
solo mirando la imagen se veía que la mitad de abajo era ruido de colores. Un
tamaño correcto no significa una imagen correcta.

El cuello de botella de fondo es `PSRAM DMA mode disabled`: el driver no vuelca
directo a PSRAM, usa un búfer interno de 30 kB y copia. Ahí está el hilo del que
tirar si algún día se quiere más ritmo (vídeo, o VGA a más fps).

### Pendiente

- **El color tira a cálido**: balance de blancos de fábrica del GC0308. Para
  "qué hay delante" da igual; si le preguntan colores, mentirá.
- **La tarjeta SD** sigue sin usar (SDMMC 1 bit: clk 40, cmd 42, d0 41, cs en P3
  del expansor).

## ⚠️ El micro del PC tumba el asistente

`wake_word_listener` no puede abrir la entrada de audio del ordenador
(`PaErrorCode -9999`) y portaudio acaba pisando memoria liberada dentro de ALSA:
**SIGSEGV que se lleva el proceso entero**. Hoy hubo volcados a las 13:12, 13:28,
14:02, 18:27 y 18:28.

Apagado con `WAKE_WORD_ENABLED=False`, que además tiene sentido: ese oyente
vigila el micro del PC y la entrada de voz es el satélite.

**Esto lo esquiva, no lo arregla.** Que un dispositivo de audio roto pueda tumbar
el asistente entero es un fallo de robustez: el fallo debería quedar contenido en
su hilo.

---

## ✅ Un turno lento ya no tira el enlace (2026-08-08)

Al pasar Luka a un motor en la nube, las preguntas a la cámara empezaron a morir
a mitad. La respuesta se generaba entera —quedaba escrita en el log del
servidor— y no llegaba a oírse. Tres intentos seguidos cayeron a los **33, 34 y
34 segundos**. Esa regularidad es lo que delató que era un plazo y no un fallo de
radio.

**La cadena, de arriba abajo:** `Thinking` tenía un plazo **absoluto** de 30 s
desde el `END` que nada podía prorrogar (`STATE`, `TRANSCRIPT`, `REPLY` y `PONG`
llegaban y no tenían ningún efecto). Al vencer iba a `Fault::ServerUnreachable`
sin emitir una sola acción, y 3 s después `is_recoverable()` lo mandaba a
`WifiConnecting` con `ConnectWifi`. Y `connect_wifi` **reasocia la radio aunque
la WiFi esté perfecta**, lo que mata el socket TLS de debajo del WebSocket: eso
es el `enlace perdido al enviar` + `HELLO` nuevo que se veía en el servidor, que
además cancelaba el turno al interpretarlo como desconexión.

Dicho corto: **el aparato respondía a "el servidor tarda" tirando la WiFi**. No
distinguía "va lento" de "no está", porque no tenía con qué: el servidor no
mandaba nada durante el turno.

**El arreglo, en tres piezas:**

1. `Thinking` lleva ahora dos marcas: `since_ms` (último indicio de vida, se
   renueva con cada `STATE` del servidor) y `started_ms` (no se renueva jamás).
   `THINKING_TIMEOUT_MS` pasa a significar **silencio**, no duración.
2. `THINKING_MAX_MS` (180 s) como tope duro, porque un plazo renovable por sí
   solo rompería el invariante del módulo: un servidor que dijera "sigo en ello"
   eternamente dejaría el aparato esperando eternamente. Al vencer cualquiera de
   los dos se manda `SendCancel`, que antes no se mandaba: el servidor seguía
   generando una respuesta que ya no iba a oír nadie.
3. `Fault::needs_wifi_restart()`: que el servidor no conteste **no** es motivo
   para tocar la radio. Ese caso rehace solo el WebSocket, por el mismo camino
   que una desconexión normal (`Disconnected` + `DropServer`, y el backoff de
   siempre lo recoge). De regalo, ese camino ya no reinicia `attempt` en cada
   vuelta, así que un servidor apagado deja de reintentarse cada 35 s.

Del lado del servidor, `device_gateway.py` repite `STATE_THINKING` cada 5 s
mientras dura el turno (`TURN_KEEPALIVE_SECONDS`). Seis avisos dentro del plazo:
se pueden perder cinco seguidos sin consecuencias. **No hizo falta tocar el
protocolo**: `STATE` con `thinking` ya existía en los dos extremos y `net.rs` ya
lo convertía en `ServerSaid(Reported::Thinking)`. Solo faltaba que alguien lo
dijera y que aquí sirviera para algo.

**Lo que NO se tocó, a propósito:** el servidor sigue sin decidir cuándo se abre
el altavoz. Ese invariante costó medidas (abría con la cola vacía; cortaba 3,2 s
de audio) y sigue intacto: el aviso solo renueva el *plazo*, y `Speaking` se
entra únicamente con audio de verdad.

Antes hubo un parche que metía una frase hablada al arrancar cada herramienta.
Funcionaba, pero por el motivo equivocado: colaba audio antes de los 30 s. El
aparato aguantaba porque le hablábamos, no porque el protocolo se lo dijera, y
cualquier turno lento sin herramienta seguía roto. Revertido.

44 tests en `luka-state` (3 nuevos: el plazo se prorroga, el tope duro corta
igual, y un servidor mudo no reasocia la WiFi).

---

# 📍 Dónde estamos (2026-08-06, cierre del día)

## Funciona

| | |
|---|---|
| Despertar diciendo "Luka" desde lejos | ✅ umbral 150, PGA al tope |
| Cerrar el turno al callarte (~1 s) | ✅ relativo al ruido de la sala |
| Encadenar otra pregunta sin repetir "Luka" | ✅ ventana de 3,5 s, anillo cian |
| Enlace estable en respuestas largas | ✅ prioridad y núcleo del hilo `detect` |
| Audio sin cortes | ✅ colchón de 1,5 s + el servidor no manda sobre el altavoz |
| Mirar por la cámara y describir la escena | ✅ VGA 640x480 |
| Enseñar la foto en el PC | ✅ `show_camera_photo` |

## Descartado, con medida y por decisión

**Barge-in** (interrumpir a Luka mientras habla). 40× más huecos de audio por
segundo; el 25 % de la reproducción salía como silencio inventado. **El problema
no es acústico** —la voz de Luka nunca superó confianza 60 con el umbral en 220,
así que no se dispararía a sí misma— sino de CPU. `barge_in = 0`. No retomar sin
que lo pidan.

## Pendiente, por orden de lo que más molesta

1. **Se tiran tramas de la voz del usuario.** 78-85 descartes por turno mientras
   SUBE lo que dices: trozos de tus frases que el STT nunca ve, sin ningún error.
   Sin investigar. Sospecha principal: la reserva de memoria por trama en el hilo
   de tiempo real (`mono_and_level` pide un `Vec` cada 20 ms). Arreglo: fondo de
   búferes reutilizables.
2. **El micro del PC tumba el asistente entero** con SIGSEGV en ALSA/portaudio.
   Esquivado con `WAKE_WORD_ENABLED=False`, **no arreglado**.
3. **Gemma-4 transcribe mal el habla rápida.** Despacio va perfecto. El audio
   llega limpio y sin saturar: está medido. Es del reconocimiento, no de la placa.
4. **El troceado del TTS** (`assistant_service.py:273`) parte por todas las comas
   pasados 80 caracteres; con enumeraciones Kokoro cae a 1× tiempo real. El
   colchón lo tapa, pero sigue ahí.
5. **Narrar tareas agénticas**: la tercera petición del día, sin empezar. La
   bloquea que `(Idle, TtsStarted)` no exista en la máquina de estados.
6. **El color de la cámara** (balance de blancos) y **la tarjeta SD**, sin tocar.

## Cómo se trabaja aquí (lo que hoy costó aprender)

- **Medir antes de cambiar.** Hoy hubo cuatro diagnósticos equivocados seguidos,
  todos por deducir en vez de medir. Los cuatro se cerraron con una traza que
  convertía una impresión en un número.
- **Bisecar cuando algo funcionaba antes.** Quitar UNA cosa y dejar el resto
  igual resolvió barge-in y la cámara-que-dejaba-mudo-al-micro. Las dos veces,
  después de horas buscando por otro lado. Si el usuario dice "esta mañana iba
  bien", la primera pregunta es qué se tocó desde entonces.
- **Los indicadores en verde no son la prueba.** El JPEG a 16 MHz tenía tamaño
  correcto y cabecera válida, y media imagen era ruido de colores. Hay que mirar
  la cosa, no sus metadatos.
- **Grabar y monitorizar REINICIAN la placa** y tiran el WebSocket. Para juzgar a
  oído, sin monitor abierto. Para medir, una captura en segundo plano a fichero.
- **El puerto serie cambia** entre `/dev/ttyACM0` y `ACM1` al re-enumerar.
