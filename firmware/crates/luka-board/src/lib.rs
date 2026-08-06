//! BSP del **Waveshare ESP32-S3-AUDIO-Board** (ESP32-S3R8, 8 MB PSRAM / 16 MB flash).
//!
//! Fuente única de verdad para pines, direcciones I²C y parámetros de audio. Es
//! `no_std` y sin dependencias del ESP-IDF a propósito: así se puede razonar y
//! testear sobre él en el host, y los drivers reales se construyen encima usando
//! traits de `embedded-hal`.
//!
//! # ⚠️ Estado de verificación
//!
//! Waveshare **no publica la tabla de GPIOs** de esta placa. Los valores marcados
//! como [`Confidence::Reported`] provienen de un port de ESPHome de terceros para
//! esta misma placa y **están pendientes de confirmar contra el hardware**.
//! Ejecuta los spikes de la Fase 0 (`spike_i2c_scan`, `spike_rgb`) y actualiza
//! este archivo con lo que salga: es el único sitio que hay que tocar.

#![no_std]
#![deny(unsafe_code)]

/// De dónde sale un dato del mapa de la placa y cuánto fiarse de él.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Confidence {
    /// Confirmado contra el hardware por un spike de la Fase 0.
    Verified,
    /// Tomado de documentación de terceros. Plausible, sin confirmar.
    Reported,
    /// Sin documentar en ninguna fuente.
    Unknown,
}

/// Todo el mapa de pines está confirmado contra el hardware real (Fase 0, 2026-08-05).
///
/// El firmware lo usa para avisar por log (y por LED) si alguna vez se arranca con
/// un mapa sin comprobar — p.ej. al portarlo a otra revisión de la placa.
pub const PINOUT_VERIFIED: bool = true;

/// Bus I²C de control: codecs, expansor GPIO y RTC cuelgan de aquí.
pub mod i2c {
    use super::Confidence;

    pub const SDA: u32 = 11;
    pub const SCL: u32 = 10;
    /// Confirmado por `spike_i2c_scan`: los 4 chips esperados responden.
    pub const CONFIDENCE: Confidence = Confidence::Verified;

    /// 100 kHz: modo estándar, el más tolerante a pistas largas y pull-ups flojos.
    /// Solo se escriben registros al arrancar, así que la velocidad es irrelevante.
    pub const FREQ_HZ: u32 = 100_000;

    /// Codec DAC → amplificador → altavoz.
    pub const ADDR_ES8311: u8 = 0x18;
    /// Expansor GPIO: enable del amplificador y los botones.
    pub const ADDR_TCA9555: u8 = 0x20;
    /// ADC de 4 canales → array de 2 micrófonos.
    pub const ADDR_ES7210: u8 = 0x40;
    /// Reloj de tiempo real.
    pub const ADDR_PCF85063: u8 = 0x51;

    /// Lo que el escaneo I²C debería encontrar, con nombre legible para el log.
    pub const EXPECTED: &[(u8, &str)] = &[
        (ADDR_ES8311, "ES8311 (codec/altavoz)"),
        (ADDR_TCA9555, "TCA9555 (expansor GPIO)"),
        (ADDR_ES7210, "ES7210 (ADC/micrófonos)"),
        (ADDR_PCF85063, "PCF85063 (RTC)"),
    ];
}

/// Bus I²S de audio, **compartido en full-duplex**.
///
/// El ES7210 (captura) y el ES8311 (reproducción) cuelgan del mismo periférico y
/// comparten BCLK y WS. De ahí la regla de arquitectura del plan: **un solo hilo
/// posee el driver I²S** y hace lectura y escritura en el mismo bucle.
pub mod i2s {
    use super::Confidence;

    /// Reloj maestro hacia ambos codecs.
    pub const MCLK: u32 = 12;
    /// Reloj de bit.
    pub const BCLK: u32 = 13;
    /// Word select / LRCK.
    pub const WS: u32 = 14;
    /// ES7210 → ESP32 (micrófonos).
    pub const DIN: u32 = 15;
    /// ESP32 → ES8311 (altavoz).
    pub const DOUT: u32 = 16;
    /// Confirmado por `spike_i2s_loopback`: se oyen los tonos de prueba y la voz
    /// grabada se reproduce, así que las cinco líneas están bien.
    pub const CONFIDENCE: Confidence = Confidence::Verified;
}

/// Anillo de LEDs RGB direccionables (WS2812), gobernado por RMT.
pub mod leds {
    use super::Confidence;

    pub const DATA: u32 = 38;
    /// Confirmado contando a ojo con `spike_rgb`: son exactamente 7.
    pub const COUNT: usize = 7;
    /// GPIO, recuento y orden de color confirmados por `spike_rgb`.
    pub const CONFIDENCE: Confidence = Confidence::Verified;

    /// Los canales **rojo y verde van intercambiados** en esta placa.
    ///
    /// Medido con `spike_rgb`: enviando `(r=255, g=0, b=0)` el anillo se ve VERDE,
    /// y con `(r=0, g=255, b=0)` se ve ROJO. El azul sí es correcto. Es el
    /// desajuste RGB↔GRB de siempre entre lo que emite el driver y lo que espera
    /// el chip del LED.
    ///
    /// En vez de esparcir el apaño por el código de animación, se corrige en un
    /// único punto: [`to_wire`], justo antes de escribir al bus.
    pub const RED_GREEN_SWAPPED: bool = true;

    /// Convierte un color lógico `(r, g, b)` al orden de bytes que espera el anillo.
    ///
    /// Todo el resto del firmware razona en RGB normal; esta función es la única
    /// que sabe cómo está cableado el hardware.
    pub const fn to_wire(r: u8, g: u8, b: u8) -> (u8, u8, u8) {
        if RED_GREEN_SWAPPED {
            (g, r, b)
        } else {
            (r, g, b)
        }
    }
}

/// Líneas del expansor I²C TCA9555 (no son GPIOs del ESP32).
pub mod expander {
    use super::Confidence;

    /// Enable del amplificador, **activo a nivel alto**. En bajo el altavoz queda mudo.
    /// Confirmado por `spike_i2s_loopback`.
    pub const PA_ENABLE: u8 = 8;
    /// Confirmados por `spike_buttons`, que vigiló las 16 líneas del expansor y
    /// vio moverse exactamente estas tres.
    pub const BUTTON_1: u8 = 9;
    pub const BUTTON_2: u8 = 10;
    pub const BUTTON_3: u8 = 11;
    /// Los tres botones son **activos a nivel bajo** (pull-up): en reposo leen 1 y
    /// pulsados leen 0. Es lo que da la vuelta a la lógica en `is_pressed`.
    pub const BUTTONS_ACTIVE_LOW: bool = true;

    // --- Puerto 0: la cámara ---
    //
    // Estas tres son la razón de que el sensor no conteste al arrancar: el
    // TCA9555 se enciende con TODO como entradas, así que quedan al aire y la
    // cámara se queda apagada y en reset. Hay que tomarlas explícitamente.
    //
    // Ojo al tocar el puerto 0: **P3 es el CS de la tarjeta SD**, así que se
    // escribe leyendo-modificando-escribiendo, nunca el registro entero.

    /// Apagado del sensor, **activo a nivel alto**: a bajo, la cámara vive.
    pub const CAM_POWER_DOWN: u8 = 5;
    /// Selección de cámara. **Activo a nivel ALTO**, y no es un detalle menor:
    /// con esta línea a bajo el sensor ni siquiera contesta por SCCB, así que
    /// parece que no hay cámara. Costó un barrido de polaridades descubrirlo
    /// (`spike_camera`), porque el port de terceros no lo documenta.
    pub const CAM_SELECT: u8 = 6;
    pub const CAM_SELECT_ACTIVE_HIGH: bool = true;
    /// Reset del sensor, **activo a nivel bajo**: pulso a bajo y de vuelta a alto.
    pub const CAM_RESET: u8 = 7;
    /// Chip select de la tarjeta SD. Aquí solo para no pisarlo sin querer.
    pub const SD_CS: u8 = 3;
    /// Mapa del expansor confirmado al completo (Fase 1, 2026-08-05).
    pub const CONFIDENCE: Confidence = Confidence::Verified;
}

/// Interfaz DVP de cámara (cabecera de 24 pines de la placa).
///
/// # Estado
///
/// Waveshare tampoco publica esto: los valores vienen de un port de terceros
/// ([jensenbox/waveshare-esp32-s3-audio]), y `spike_camera` los ha **confirmado
/// al completo**: el sensor se identifica como GC0308 en `0x21` y entrega un
/// fotograma real que comprime a un JPEG válido. Control y bus de datos, los dos
/// verificados contra el hardware.
///
/// [jensenbox/waveshare-esp32-s3-audio]: https://github.com/jensenbox/waveshare-esp32-s3-audio
///
/// # El SCCB va por el bus I²C que ya existe
///
/// `SIOD`/`SIOC` son los mismos GPIO11/GPIO10 del bus de control, y la dirección
/// del sensor (`0x21`) no choca con ninguno de los cuatro chips de la placa. No
/// hay que abrir un segundo bus.
///
/// # Y las líneas de control van por el expansor
///
/// `power_down`, `camera_select` y `hardware_reset` **no son GPIOs del ESP32**:
/// cuelgan del TCA9555 (ver [`expander`]). Esa es la razón de que el sensor no
/// respondiera al primer escaneo: el expansor arranca con todo como entradas, así
/// que esas tres líneas quedan al aire y la cámara nunca sale de reset.
pub mod camera {
    use super::Confidence;

    /// Reloj que el ESP32 le da al sensor. Sin él, el bloque digital del sensor
    /// no arranca y **ni siquiera contesta por SCCB**.
    pub const XCLK: u32 = 43;
    /// Reloj de píxel, del sensor al ESP32.
    pub const PCLK: u32 = 44;
    /// Sincronismo vertical (nueva imagen).
    pub const VSYNC: u32 = 21;
    /// Referencia horizontal (hay datos válidos).
    pub const HREF: u32 = 1;
    /// Bus de datos de 8 bits, de D0 a D7.
    pub const DATA: [u32; 8] = [2, 17, 18, 39, 45, 46, 47, 48];

    /// Frecuencia de XCLK. 20 MHz es lo que usa el driver de Espressif y sale de
    /// dividir los 80 MHz del APB por 4, así que es exacta y no arrastra jitter.
    pub const XCLK_HZ: u32 = 20_000_000;

    /// Dirección SCCB del sensor. La comparten GC0308 y OV7670, así que **no
    /// basta para identificarlo**: hay que leerle el registro de identidad.
    pub const ADDR_SENSOR: u8 = 0x21;
    /// Registro de identidad del GC0308 y el valor que debe devolver.
    pub const GC0308_ID_REG: u8 = 0x00;
    pub const GC0308_ID: u8 = 0x9b;

    /// Resolución de captura.
    ///
    /// **QVGA y no VGA por una razón medida**: a 640x480 el driver rechaza cada
    /// fotograma con `FB-SIZE: 599040 != 614400`, doce líneas de menos, de forma
    /// consistente. Viene de la ventana de salida del GC0308, no del cableado.
    ///
    /// Y encaja con el uso: para describirle una escena a un modelo multimodal
    /// sobra, y el JPEG se queda en ~6 kB, que por este enlace es un suspiro.
    pub const WIDTH: u16 = 320;
    pub const HEIGHT: u16 = 240;

    /// Confirmado por `spike_camera` de extremo a extremo: el sensor contesta en
    /// `0x21` con ID `0x9B`, y se captura un fotograma de 320x240 que comprime a
    /// un JPEG con cabecera válida. Control y bus de datos, los dos verificados.
    pub const CONFIDENCE: Confidence = Confidence::Verified;
}

/// Parámetros del pipeline de audio.
pub mod audio {
    /// 16 kHz: lo que espera Whisper. El `stt_engine` del asistente normaliza a
    /// esta frecuencia igualmente, así que enviarla ya así ahorra un remuestreo.
    pub const SAMPLE_RATE_HZ: u32 = 16_000;
    pub const CHANNELS: u16 = 1;
    pub const BITS_PER_SAMPLE: u16 = 16;

    /// 20 ms por trama: el compromiso habitual entre latencia y sobrecarga.
    pub const FRAME_MS: u32 = 20;
    /// Muestras por trama (320 a 16 kHz).
    pub const FRAME_SAMPLES: usize = (SAMPLE_RATE_HZ * FRAME_MS / 1000) as usize;
    /// Bytes por trama (640 con PCM16 mono).
    pub const FRAME_BYTES: usize = FRAME_SAMPLES * (BITS_PER_SAMPLE as usize / 8);

    /// Audio retenido antes del disparo, para no cortar el principio de la frase.
    pub const PREROLL_MS: u32 = 1_000;
    pub const PREROLL_SAMPLES: usize = (SAMPLE_RATE_HZ * PREROLL_MS / 1000) as usize;
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Todos los GPIOs del mapa, para las comprobaciones cruzadas.
    const ALL_PINS: &[(&str, u32)] = &[
        ("i2c.sda", i2c::SDA),
        ("i2c.scl", i2c::SCL),
        ("i2s.mclk", i2s::MCLK),
        ("i2s.bclk", i2s::BCLK),
        ("i2s.ws", i2s::WS),
        ("i2s.din", i2s::DIN),
        ("i2s.dout", i2s::DOUT),
        ("leds.data", leds::DATA),
    ];

    #[test]
    fn no_hay_pines_duplicados() {
        for (i, (name_a, a)) in ALL_PINS.iter().enumerate() {
            for (name_b, b) in &ALL_PINS[i + 1..] {
                assert_ne!(a, b, "GPIO{a} asignado a la vez a {name_a} y {name_b}");
            }
        }
    }

    /// GPIO 26-32 salen al flash/PSRAM octal del módulo R8: tocarlos cuelga la placa.
    #[test]
    fn ningun_pin_pisa_la_psram_octal() {
        for (name, pin) in ALL_PINS {
            assert!(
                !(26..=32).contains(pin),
                "{name} usa GPIO{pin}, reservado para flash/PSRAM octal"
            );
        }
    }

    /// El ESP32-S3 llega hasta GPIO48.
    #[test]
    fn los_pines_existen_en_el_s3() {
        for (name, pin) in ALL_PINS {
            assert!(*pin <= 48, "{name} usa GPIO{pin}, que no existe en el ESP32-S3");
        }
    }

    #[test]
    fn las_direcciones_i2c_son_de_7_bits_y_unicas() {
        for (addr, name) in i2c::EXPECTED {
            assert!(*addr < 0x80, "{name}: {addr:#04x} no es dirección de 7 bits");
        }
        for (i, (a, na)) in i2c::EXPECTED.iter().enumerate() {
            for (b, nb) in &i2c::EXPECTED[i + 1..] {
                assert_ne!(a, b, "colisión I2C entre {na} y {nb}");
            }
        }
    }

    #[test]
    fn las_tramas_de_audio_cuadran() {
        assert_eq!(audio::FRAME_SAMPLES, 320);
        assert_eq!(audio::FRAME_BYTES, 640);
        assert_eq!(audio::PREROLL_SAMPLES, 16_000);
        // El pre-roll debe ser múltiplo entero de trama: si no, el ring guarda
        // medias tramas y el principio de la frase sale con un chasquido.
        assert_eq!(audio::PREROLL_SAMPLES % audio::FRAME_SAMPLES, 0);
    }

    #[test]
    fn to_wire_corrige_el_cruce_rojo_verde() {
        // Para que el anillo se vea ROJO hay que mandarle el valor en el 2º byte.
        assert_eq!(leds::to_wire(255, 0, 0), (0, 255, 0));
        // Y para verlo VERDE, en el 1º.
        assert_eq!(leds::to_wire(0, 255, 0), (255, 0, 0));
        // El azul no se toca: se veía bien tal cual.
        assert_eq!(leds::to_wire(0, 0, 255), (0, 0, 255));
        // Los grises son invariantes ante el intercambio.
        assert_eq!(leds::to_wire(128, 128, 128), (128, 128, 128));
    }

    /// Las líneas del expansor son de un TCA9555: dos puertos de 8 bits, P0..P15.
    #[test]
    fn las_lineas_del_expansor_son_validas() {
        for (name, line) in [
            ("pa_enable", expander::PA_ENABLE),
            ("button_1", expander::BUTTON_1),
            ("button_2", expander::BUTTON_2),
            ("button_3", expander::BUTTON_3),
        ] {
            assert!(line < 16, "expander.{name} = P{line}, fuera de rango del TCA9555");
        }
    }
}
