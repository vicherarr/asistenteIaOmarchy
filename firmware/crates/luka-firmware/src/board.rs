//! Acceso al hardware por I²C: expansor (amplificador y botones) y los dos codecs.
//!
//! Las secuencias de init de los codecs vienen tal cual de `spike_i2s_loopback`,
//! que es lo que las validó contra la placa, y están justificadas registro a
//! registro en `docs/codec-registers.md`. No se "limpian" ni se reordenan: con
//! estos chips, un cambio inocente deja el codec mudo sin dar un solo error.

use anyhow::{bail, Context, Result};
use esp_idf_hal::delay::{FreeRtos, BLOCK};
use esp_idf_hal::i2c::I2cDriver;
use luka_board::{expander, i2c as bi2c};
use std::sync::{Arc, Mutex};

/// El bus I²C, compartido entre el hilo de audio (que abre y cierra el
/// amplificador) y el de botones (que los sondea cada 20 ms).
///
/// Un `Mutex` basta y no compromete el audio: por él solo pasan operaciones
/// esporádicas y cortas. **La ruta de tiempo real es el I²S, y ese no se
/// comparte con nadie.**
pub type SharedI2c = Arc<Mutex<I2cDriver<'static>>>;

pub fn write_reg(i2c: &mut I2cDriver, addr: u8, reg: u8, val: u8) -> Result<()> {
    i2c.write(addr, &[reg, val], BLOCK)
        .with_context(|| format!("I2C {addr:#04x}: escribiendo el registro {reg:#04x}"))
}

pub fn read_reg(i2c: &mut I2cDriver, addr: u8, reg: u8) -> Result<u8> {
    let mut buf = [0u8; 1];
    i2c.write_read(addr, &[reg], &mut buf, BLOCK)
        .with_context(|| format!("I2C {addr:#04x}: leyendo el registro {reg:#04x}"))?;
    Ok(buf[0])
}

fn write_all(i2c: &mut I2cDriver, addr: u8, regs: &[(u8, u8)]) -> Result<()> {
    for (reg, val) in regs {
        write_reg(i2c, addr, *reg, *val)?;
    }
    Ok(())
}

// ============================= TCA9555 =============================

const REG_INPUT_1: u8 = 0x01;
const REG_OUTPUT_1: u8 = 0x03;
const REG_POLARITY_1: u8 = 0x05;
const REG_CONFIG_1: u8 = 0x07;

/// Bit del amplificador dentro del puerto 1 (P8..P15).
const fn pa_bit() -> u8 {
    1 << (expander::PA_ENABLE - 8)
}

/// Deja el expansor listo: **amplificador apagado** y botones como entradas.
///
/// El orden no es negociable: primero el valor de salida a 0, después la
/// dirección. Al revés, el pin pasaría un instante como salida con valor
/// indeterminado, y ese instante es justo el que puede abrir el amplificador.
pub fn init_expander(i2c: &mut I2cDriver) -> Result<()> {
    let out = read_reg(i2c, bi2c::ADDR_TCA9555, REG_OUTPUT_1)?;
    write_reg(i2c, bi2c::ADDR_TCA9555, REG_OUTPUT_1, out & !pa_bit())?;

    // Sin inversión de polaridad: la lógica de "pulsado" se aplica en Rust, donde
    // se ve, en vez de esconderla en un registro del expansor.
    write_reg(i2c, bi2c::ADDR_TCA9555, REG_POLARITY_1, 0x00)?;

    // 1 = entrada. Todo entradas menos el amplificador.
    let cfg = 0xFFu8 & !pa_bit();
    write_reg(i2c, bi2c::ADDR_TCA9555, REG_CONFIG_1, cfg)?;

    log::info!("TCA9555: amplificador APAGADO, botones como entradas");
    Ok(())
}

/// Abre o cierra el amplificador del altavoz.
///
/// El único sitio del firmware que lo enciende es el arranque de la reproducción.
/// Todo lo demás lo apaga. Un amplificador abierto sin querer, con los micros a
/// centímetros, es lo que convierte un fallo pequeño en un pitido insoportable.
pub fn set_amplifier(i2c: &mut I2cDriver, on: bool) -> Result<()> {
    let out = read_reg(i2c, bi2c::ADDR_TCA9555, REG_OUTPUT_1)?;
    let out = if on { out | pa_bit() } else { out & !pa_bit() };
    write_reg(i2c, bi2c::ADDR_TCA9555, REG_OUTPUT_1, out)
}

/// Apaga el amplificador tragándose el error.
///
/// Para las rutas de limpieza y de pánico: si algo ya ha ido mal, lo que importa
/// es que el altavoz quede mudo, no propagar un segundo error encima del primero.
pub fn silence(i2c: &SharedI2c) {
    if let Ok(mut guard) = i2c.lock() {
        let _ = set_amplifier(&mut guard, false);
    }
}

/// Estado crudo de los tres botones, ya normalizado a "pulsado = true".
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Buttons {
    pub one: bool,
    pub two: bool,
    pub three: bool,
}

impl Buttons {
    // Lo usará el supervisor cuando exista; se deja aquí porque es donde vive el
    // resto del conocimiento sobre los botones.
    #[allow(dead_code)]
    /// ¿Hay algún botón pulsado? En la Fase 1 los tres hacen lo mismo
    /// (push-to-talk): es más fácil acertar con cualquiera de ellos a ciegas que
    /// buscar el correcto, y los tres se distinguirán cuando haya funciones que
    /// justifiquen distinguirlos.
    pub const fn any(self) -> bool {
        self.one || self.two || self.three
    }
}

/// Lee los botones del expansor.
pub fn read_buttons(i2c: &mut I2cDriver) -> Result<Buttons> {
    let port = read_reg(i2c, bi2c::ADDR_TCA9555, REG_INPUT_1)?;
    // Confirmado por `spike_buttons`: en reposo leen 1 y pulsados 0.
    let level = |line: u8| {
        let raw = port >> (line - 8) & 1 == 1;
        if luka_board::expander::BUTTONS_ACTIVE_LOW { !raw } else { raw }
    };
    Ok(Buttons {
        one: level(expander::BUTTON_1),
        two: level(expander::BUTTON_2),
        three: level(expander::BUTTON_3),
    })
}

// ============================== ES7210 ==============================
// ADC de 4 canales -> array de 2 micrófonos.

/// Coeficientes de reloj para 16 kHz con MCLK 4.096 MHz: `adc_div=1, dll=1, doubler=1`.
const ES7210_MAINCLK: u8 = 0x01 | (0x01 << 6) | (0x01 << 7);

/// Ganancia de los PGA de los micros.
///
/// El driver de Espressif usa 0x1A (~30 dB) y con eso la Fase 0 medía el habla a
/// unos -45 dBFS: funciona, porque el `stt_engine` normaliza con `loudnorm`, pero
/// deja poco margen sobre el ruido de fondo. Se sube a 0x1D (~34,5 dB) para
/// mejorar la relación señal/ruido **antes** de que el audio salga por la red,
/// que es donde ya no se puede arreglar.
const MIC_GAIN: u8 = 0x1D;

pub fn es7210_init(i2c: &mut I2cDriver) -> Result<()> {
    let addr = bi2c::ADDR_ES7210;
    log::info!("ES7210: inicializando ADC de micrófonos…");

    write_all(i2c, addr, &[
        (0x00, 0xFF), // reset software
        (0x00, 0x32),
        (0x09, 0x30), // tiempos de arranque
        (0x0A, 0x30),
        (0x23, 0x2A), // filtro paso-alto ADC1-2
        (0x22, 0x0A),
        (0x21, 0x2A), // filtro paso-alto ADC3-4
        (0x20, 0x0A),
        (0x11, 0x60), // I2S estándar, 16 bits
        (0x12, 0x00), // sin TDM
        (0x40, 0xC3), // potencia analógica y VMID
        (0x41, 0x70), // bias MIC1-2
        (0x42, 0x70), // bias MIC3-4
        (0x43, MIC_GAIN),
        (0x44, MIC_GAIN),
        (0x45, MIC_GAIN),
        (0x46, MIC_GAIN),
        (0x47, 0x08), // enciende MIC1-4
        (0x48, 0x08),
        (0x49, 0x08),
        (0x4A, 0x08),
        (0x07, 0x20),           // osr
        (0x02, ES7210_MAINCLK), // adc_div | doubler<<6 | dll<<7
        (0x04, 0x01),           // lrck_h
        (0x05, 0x00),           // lrck_l
        (0x06, 0x04),           // apaga la DLL
        (0x4B, 0x0F),           // bias+ADC+PGA de MIC1-2
        (0x4C, 0x0F),           // ídem MIC3-4
        (0x00, 0x71),           // habilita
        (0x00, 0x41),
    ])?;

    log::info!("ES7210: OK (ganancia {MIC_GAIN:#04x})");
    Ok(())
}

// ============================== ES8311 ==============================

/// Volumen del DAC (registro 0x32).
const DAC_VOLUME: u8 = 0xB0;

pub fn es8311_init(i2c: &mut I2cDriver) -> Result<()> {
    let addr = bi2c::ADDR_ES8311;
    log::info!("ES8311: inicializando codec de salida…");

    // El 0x80 final es el power-on. Sin él todo lo demás se escribe sin error y el
    // codec se queda mudo en power-down: el fallo clásico de este chip, y es
    // indistinguible de un problema de cableado. Por eso se verifica al final.
    write_reg(i2c, addr, 0x00, 0x1F)?;
    FreeRtos::delay_ms(20);
    write_reg(i2c, addr, 0x00, 0x00)?;
    write_reg(i2c, addr, 0x00, 0x80)?;

    // Coeficientes de la tabla `coeff_div` del driver de Espressif para
    // 16 kHz @ MCLK 4.096 MHz.
    const PRE_DIV: u8 = 1;
    const PRE_MULTI: u8 = 0;
    const ADC_DIV: u8 = 1;
    const DAC_DIV: u8 = 1;
    const FS_MODE: u8 = 0;
    const ADC_OSR: u8 = 0x10;
    const DAC_OSR: u8 = 0x10;
    const BCLK_DIV: u8 = 4;
    const LRCK_H: u8 = 0x00;
    const LRCK_L: u8 = 0xFF;

    write_reg(i2c, addr, 0x01, 0x3F)?;

    // Registros de campos empaquetados: son lectura-modificación-escritura, y
    // escribirlos enteros pisa bits que el chip usa para otra cosa.
    let reg02 = read_reg(i2c, addr, 0x02)?;
    write_reg(i2c, addr, 0x02, (reg02 & 0x07) | ((PRE_DIV - 1) << 5) | (PRE_MULTI << 3))?;

    write_reg(i2c, addr, 0x03, (FS_MODE << 6) | ADC_OSR)?;
    write_reg(i2c, addr, 0x04, DAC_OSR)?;
    write_reg(i2c, addr, 0x05, ((ADC_DIV - 1) << 4) | (DAC_DIV - 1))?;

    // El registro lleva `bclk_div - 1` cuando es < 19. Escribir el valor sin
    // restar deja el reloj de bit mal y el DAC no suena, sin dar ningún error.
    let reg06 = read_reg(i2c, addr, 0x06)?;
    let bclk_field = if BCLK_DIV < 19 { BCLK_DIV - 1 } else { BCLK_DIV };
    write_reg(i2c, addr, 0x06, (reg06 & 0xE0) | bclk_field)?;

    let reg07 = read_reg(i2c, addr, 0x07)?;
    write_reg(i2c, addr, 0x07, (reg07 & 0xC0) | LRCK_H)?;
    write_reg(i2c, addr, 0x08, LRCK_L)?;

    write_all(i2c, addr, &[
        (0x00, 0x80), // esclavo: los relojes los genera el ESP32
        (0x09, 0x0C), // entrada 16 bits
        (0x0A, 0x0C), // salida 16 bits
        (0x0D, 0x01), // circuitería analógica
        (0x0E, 0x02), // PGA y modulador del ADC
        (0x12, 0x00), // enciende el DAC
        (0x13, 0x10), // salida al driver del altavoz
        (0x1C, 0x6A), // bypass del ecualizador del ADC + cancela offset DC
        (0x37, 0x08), // bypass del ecualizador del DAC
        (0x32, DAC_VOLUME),
    ])?;

    let reg00 = read_reg(i2c, addr, 0x00)?;
    if reg00 & 0x80 == 0 {
        bail!("ES8311 quedó en power-down (reg00 = {reg00:#04x}): la secuencia de init falló");
    }

    log::info!("ES8311: OK (reg00 = {reg00:#04x})");
    Ok(())
}
