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
| Botones ×3 | TCA9555 P9/P10/P11 | ⬜ **sin verificar** — los usa la Fase 1 |

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
- **`crates/spikes`** — los 4 programas de la Fase 0.
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

## 🔄 Fase 1 — en curso

Botón → grabar → WebSocket → Luka responde → suena por el altavoz. LEDs de estado. Sin ML.

### Hecho: TLS con certificado fijado (*pinning*)
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

### Siguiente
1. **Lado Python** (en `master`, según lo acordado): endpoint WebSocket `/device/ws` y
   *sink* de audio configurable `pc` | `device` | `both`.
2. **Firmware**: cliente WS con pinning, máquina de estados, anillo de LEDs.
3. Verificar los **botones del TCA9555**, lo único del mapa que sigue sin confirmar.

**Decisión pendiente:** la wake word (Fase 3, según lo acordado).

**Pendiente menor:** `ccache` sin instalar (`sudo pacman -S ccache`);
`ws2812-esp32-rmt-driver` usa la API RMT legacy (aviso de deprecación, cosa del crate).

---

## Cómo ejecutar

```bash
# Tests en host — DESDE LA RAÍZ DEL REPO (esquiva la config de Xtensa)
cargo test --manifest-path firmware/crates/luka-board/Cargo.toml
cargo test --manifest-path firmware/crates/luka-config/Cargo.toml

# Spikes — desde firmware/, con la placa en /dev/ttyACM0
cd firmware
cargo build -p spikes
espflash flash --chip esp32s3 --port /dev/ttyACM0 --monitor \
    target/xtensa-esp32s3-espidf/debug/spike_i2c_scan
```
