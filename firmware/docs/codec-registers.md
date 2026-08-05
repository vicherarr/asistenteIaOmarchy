# Secuencias de inicialización de los codecs

Extraídas de los drivers en C de Espressif ([esp-bsp](https://github.com/espressif/esp-bsp)),
que son la referencia de facto. Sirven para escribir e **revisar** los drivers Rust sin
adivinar valores: el ES8311 se queda mudo en *power-down* si el orden falla, y eso es
indistinguible de un problema de cableado.

Todo lo de aquí está fijado para el caso de esta placa: **16 kHz, mono, 16 bits,
MCLK = 256 × fs = 4.096 MHz**.

---

## ES8311 — codec de salida (altavoz), I²C `0x18`

### Reset y arranque
El `0x80` final es el que enciende el chip. Sin él, todo lo demás se escribe
correctamente y el codec sigue en *power-down*, mudo y sin dar error.

| Orden | Reg | Valor | Qué hace |
|---|---|---|---|
| 1 | `0x00` | `0x1F` | reset |
| 2 | — | — | **esperar 20 ms** |
| 3 | `0x00` | `0x00` | sale del reset |
| 4 | `0x00` | `0x80` | **power-on** ← el crítico |

### Reloj — fila de 16 kHz con MCLK 4.096 MHz
Tabla `coeff_div[]`, campos:
`mclk, rate, pre_div, mult, adc_div, dac_div, fs_mode, lrc_h, lrc_l, bclk_div, adc_osr, dac_osr`

```
{4096000, 16000, 0x01, 0x00, 0x01, 0x01, 0x00, 0x00, 0xff, 0x04, 0x10, 0x10}
```

Se reparte por los registros `0x01`–`0x08` (gestor de reloj). `0x06` es
**lectura-modificación-escritura**: hay bits que no deben pisarse.

### Formato
`0x00` (bit de master/slave — la placa va de **esclavo**, el ESP32 manda los relojes),
`0x09` = resolución de entrada, `0x0A` = resolución de salida. Para 16 bits, `0x0C`.

### Encendido de la cadena analógica
Ninguno es valor por defecto: hay que escribirlos todos.

| Reg | Valor | Qué hace |
|---|---|---|
| `0x0D` | `0x01` | enciende la circuitería analógica |
| `0x0E` | `0x02` | habilita PGA y modulador del ADC |
| `0x12` | `0x00` | enciende el DAC |
| `0x13` | `0x10` | habilita la salida al driver de auriculares/altavoz |
| `0x1C` | `0x6A` | *bypass* del ecualizador del ADC, cancela offset DC |
| `0x37` | `0x08` | *bypass* del ecualizador del DAC |

### Volumen
`0x32` = volumen de voz (0–255). `0x31` bits [6:5] = mute.

---

## ES7210 — ADC de entrada (micrófonos), I²C `0x40`

### Secuencia completa
```
0x00 = 0xFF     reset software
0x00 = 0x32
0x09 = 0x30     tiempo de arranque
0x0A = 0x30
0x23 = 0x2A     filtro paso-alto ADC1-2
0x22 = 0x0A
0x21 = 0x2A     filtro paso-alto ADC3-4
0x20 = 0x0A
0x11 = fmt|bits    formato I2S + resolución  (16 bits -> 0x60; I2S estándar)
0x12 = 0x02        TDM 1xFS  (0x00 si no se usa TDM)
0x40 = 0xC3     potencia analógica y tensión VMID
0x41 = bias     bias de MIC1-2
0x42 = bias     bias de MIC3-4
0x43..0x46 = gain|0x10    ganancia de MIC1-4
0x47..0x4A = 0x08         enciende MIC1-4
--- reloj (ver abajo) ---
0x06 = 0x04     apaga la DLL
0x4B = 0x0F     enciende bias + ADC + PGA de MIC1-2
0x4C = 0x0F     ídem MIC3-4
0x00 = 0x71     habilita el dispositivo
0x00 = 0x41
```

### Reloj — fila de 16 kHz con MCLK 4.096 MHz
Tabla `es7210_coeff_div[]`, campos:
`mclk, lrck, ss_ds, adc_div, dll, doubler, osr, mclk_src, lrck_h, lrck_l`

```
{4096000, 16000, 0x00, 0x01, 0x01, 0x01, 0x20, 0x00, 0x01, 0x00}
```

Se traduce a:

| Reg | Valor | Cálculo |
|---|---|---|
| `0x07` | `0x20` | `osr` |
| `0x02` | `0xC1` | `adc_div \| (doubler << 6) \| (dll << 7)` = `0x01\|0x40\|0x80` |
| `0x04` | `0x01` | `lrck_h` |
| `0x05` | `0x00` | `lrck_l` |

---

## TCA9555 — expansor GPIO, I²C `0x20`

El *enable* del amplificador (`PA_ENABLE`, línea **P8**) cuelga de aquí. P8 es el bit 0
del puerto 1.

| Reg | Función |
|---|---|
| `0x02` / `0x03` | puerto de salida 0 / 1 |
| `0x06` / `0x07` | configuración 0 / 1 (`0` = salida, `1` = entrada) |

Para encender el amplificador: en `0x07` poner el bit 0 a `0` (P8 como salida), y en
`0x03` el bit 0 a `1`.

> **Ojo:** si el amplificador queda habilitado mientras el DAC está en reset o sin
> configurar, suele oírse un chasquido fuerte. Orden correcto: configurar el ES8311
> primero y habilitar el amplificador **al final**.
