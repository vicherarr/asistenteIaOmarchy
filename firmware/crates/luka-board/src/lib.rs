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
    /// Mapa del expansor confirmado al completo (Fase 1, 2026-08-05).
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
