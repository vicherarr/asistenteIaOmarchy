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

### Pendiente de la Fase 2 (pulido, no rehacer)
- **Reconexión:** la cadencia real la lleva el cliente del ESP-IDF, no el backoff de la
  máquina de estados, por lo del punto 2. Funciona, pero la política está en dos sitios.
- **La respuesta llega con el `<think>` del modelo dentro.** Se ve en el log del
  dispositivo (`← {"text":"<think>…"}`). Al altavoz no le afecta, pero si algún día hay
  pantalla habrá que limpiarlo en el servidor.
- Verificar cuánto aguanta el enlace en horas, y el consumo.
- El **anillo de LEDs no se ha comprobado a ojo** después de arreglar la composición de
  brillo y gamma. Los tests fijan que los estados visibles superan un mínimo con el
  `led_brightness = 48` real, pero eso es aritmética, no una mirada a la placa.

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
