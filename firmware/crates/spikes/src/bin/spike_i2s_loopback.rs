//! Spike 4/4 — Cadena de audio: micrófonos → altavoz (grabar y reproducir).
//!
//! # Nada de loopback en vivo
//!
//! La primera versión de este spike reproducía el micro por el altavoz en tiempo
//! real. En esta placa el altavoz y los micros están a centímetros, así que con
//! ganancia se realimenta y **suelta un pitido ensordecedor** en segundos.
//!
//! Aquí el ciclo es **grabar primero, reproducir después**: mientras se graba el
//! amplificador está apagado, y mientras se reproduce no se escucha. Al no
//! solaparse nunca las dos rutas, el acople es imposible por construcción, no
//! solo improbable. Además el amplificador se apaga siempre al terminar
//! (incluido si algo falla), para que un error no deje el altavoz abierto.
//!
//! **Qué responde:** el spike más valioso de la Fase 0, porque valida de una sola
//! pasada todo lo que queda por confirmar del hardware:
//!   - los 5 pines de I²S del mapa (`luka-board`),
//!   - la secuencia de init del **ES7210** (ADC/micros),
//!   - la secuencia de init del **ES8311** (codec/altavoz),
//!   - el *enable* del amplificador vía **TCA9555**,
//!   - y que los relojes cuadran a 16 kHz con MCLK = 256 × fs = 4.096 MHz.
//!
//! Si al hablarle a la placa te oyes por el altavoz, la cadena de audio entera
//! funciona y la Fase 1 puede construirse encima sin miedo.
//!
//! Los valores de registro salen de los drivers en C de Espressif; están
//! documentados y justificados en `firmware/docs/codec-registers.md`.
//!
//! **Qué esperar**, en ciclos de unos 8 segundos:
//!   1. *Midiendo* (4 s, altavoz apagado): habla y mira cómo sube la barra de nivel.
//!   2. *Pitido* corto de prueba: confirma la salida sin depender del micro.
//!   3. *Reproducción* (4 s): oyes lo que acabas de decir.
//!
//! Si oyes el pitido y luego tu voz, la cadena de audio entera funciona.
//!
//! ```bash
//! cargo run -p spikes --bin spike_i2s_loopback
//! ```

use anyhow::{bail, Context, Result};
use esp_idf_hal::delay::{FreeRtos, BLOCK};
use esp_idf_hal::i2c::{I2cConfig, I2cDriver};
use esp_idf_hal::i2s::config::{
    Config, DataBitWidth, MclkMultiple, SlotMode, StdClkConfig, StdConfig, StdGpioConfig,
    StdSlotConfig,
};
use esp_idf_hal::i2s::{I2sBiDir, I2sDriver};
use esp_idf_hal::peripherals::Peripherals;
use esp_idf_hal::units::FromValueType;

use luka_board::{audio, i2c as bi2c};

// Estos pines hay que nombrarlos literalmente (cada GPIO es un tipo distinto en
// esp-idf-hal). Si alguien cambia el mapa en `luka-board`, esto no compila.
const _: () = assert!(bi2c::SDA == 11 && bi2c::SCL == 10);
const _: () = assert!(luka_board::i2s::MCLK == 12);
const _: () = assert!(luka_board::i2s::BCLK == 13);
const _: () = assert!(luka_board::i2s::WS == 14);
const _: () = assert!(luka_board::i2s::DIN == 15);
const _: () = assert!(luka_board::i2s::DOUT == 16);

/// MCLK = 256 × 16 kHz = 4.096 MHz. Es la relación para la que están calculados
/// los coeficientes de reloj de AMBOS codecs; cambiarla obliga a cambiarlos.
const MCLK_MULTIPLE: u32 = 256;

/// Ganancia aplicada a lo grabado antes de reproducirlo. Los micros MEMS dan poca
/// señal y sin esto casi no se oye nada, lo que parecería (falsamente) que la
/// cadena no funciona. Moderada a propósito: no hay prisa por saturar.
const PLAYBACK_GAIN: i32 = 4;

/// Duración de cada fase de grabación / reproducción.
const CAPTURE_SECS: u32 = 4;

/// Volumen del DAC (registro 0x32 del ES8311).
///
/// Moderado: la cadena de audio ya está confirmada y este spike se ejecuta con la
/// placa sobre la mesa, a un palmo de la cara.
const DAC_VOLUME: u8 = 0x80;

// =============================== I²C ===============================

fn write_reg(i2c: &mut I2cDriver, addr: u8, reg: u8, val: u8) -> Result<()> {
    i2c.write(addr, &[reg, val], BLOCK)
        .with_context(|| format!("I2C {addr:#04x}: fallo escribiendo reg {reg:#04x} = {val:#04x}"))
}

fn read_reg(i2c: &mut I2cDriver, addr: u8, reg: u8) -> Result<u8> {
    let mut buf = [0u8; 1];
    i2c.write_read(addr, &[reg], &mut buf, BLOCK)
        .with_context(|| format!("I2C {addr:#04x}: fallo leyendo reg {reg:#04x}"))?;
    Ok(buf[0])
}

/// Aplica una lista de escrituras `(registro, valor)` en orden.
fn write_all(i2c: &mut I2cDriver, addr: u8, regs: &[(u8, u8)]) -> Result<()> {
    for (reg, val) in regs {
        write_reg(i2c, addr, *reg, *val)?;
    }
    Ok(())
}

// ============================== ES7210 ==============================
// ADC de 4 canales -> array de 2 micrófonos. Ver docs/codec-registers.md.

/// Coeficientes de reloj para 16 kHz con MCLK 4.096 MHz (tabla `es7210_coeff_div`):
/// `adc_div=0x01, dll=0x01, doubler=0x01, osr=0x20, lrck_h=0x01, lrck_l=0x00`.
const ES7210_MAINCLK: u8 = 0x01 | (0x01 << 6) | (0x01 << 7); // = 0xC1

fn es7210_init(i2c: &mut I2cDriver) -> Result<()> {
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
        // Formato: I2S estándar, 16 bits (0x60). SIN TDM: solo queremos los 2 micros
        // como I2S estéreo normal, que es mucho más simple de consumir.
        (0x11, 0x60),
        (0x12, 0x00), // TDM desactivado
        (0x40, 0xC3), // potencia analógica y VMID
        (0x41, 0x70), // bias MIC1-2
        (0x42, 0x70), // bias MIC3-4
        // Ganancia de los micros. 0x10 es el bit de "habilitar PGA"; el nibble bajo
        // es la ganancia. 0x1A ≈ 30 dB, que es lo que usa el driver de Espressif.
        (0x43, 0x1A),
        (0x44, 0x1A),
        (0x45, 0x1A),
        (0x46, 0x1A),
        (0x47, 0x08), // enciende MIC1-4
        (0x48, 0x08),
        (0x49, 0x08),
        (0x4A, 0x08),
        // --- reloj: 16 kHz @ MCLK 4.096 MHz ---
        (0x07, 0x20),          // osr
        (0x02, ES7210_MAINCLK), // adc_div | doubler<<6 | dll<<7
        (0x04, 0x01),          // lrck_h
        (0x05, 0x00),          // lrck_l
        (0x06, 0x04),          // apaga la DLL
        (0x4B, 0x0F),          // enciende bias+ADC+PGA de MIC1-2
        (0x4C, 0x0F),          // ídem MIC3-4
        (0x00, 0x71),          // habilita el dispositivo
        (0x00, 0x41),
    ])?;

    log::info!("ES7210: OK");
    Ok(())
}

// ============================== ES8311 ==============================
// Codec de salida hacia el amplificador y el altavoz.

fn es8311_init(i2c: &mut I2cDriver) -> Result<()> {
    let addr = bi2c::ADDR_ES8311;
    log::info!("ES8311: inicializando codec de salida…");

    // Reset. El 0x80 final es el power-on: sin él, TODO lo demás se escribe sin
    // error y el codec se queda mudo en power-down. Es el fallo clásico de este chip
    // y es indistinguible de un problema de cableado, así que va con su comprobación.
    write_reg(i2c, addr, 0x00, 0x1F)?;
    FreeRtos::delay_ms(20);
    write_reg(i2c, addr, 0x00, 0x00)?;
    write_reg(i2c, addr, 0x00, 0x80)?;

    // --- Reloj: 16 kHz @ MCLK 4.096 MHz ---
    //
    // Coeficientes de la tabla `coeff_div` del driver de Espressif:
    //   pre_div=1, pre_multi=0, adc_div=1, dac_div=1, fs_mode=0,
    //   lrck_h=0x00, lrck_l=0xFF, bclk_div=4, adc_osr=0x10, dac_osr=0x10
    //
    // Varios registros son de campos empaquetados y **lectura-modificación-escritura**:
    // escribirlos enteros pisa bits que el chip usa para otra cosa. La aritmética de
    // abajo es literalmente la de `es8311_sample_frequency_config()`.
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

    // 0x01: habilita todos los relojes; MCLK entra por su propio pin (no por BCLK).
    write_reg(i2c, addr, 0x01, 0x3F)?;

    // 0x02: pre-divisor y pre-multiplicador, conservando los 3 bits bajos.
    let reg02 = read_reg(i2c, addr, 0x02)?;
    write_reg(i2c, addr, 0x02,
        (reg02 & 0x07) | ((PRE_DIV - 1) << 5) | (PRE_MULTI << 3))?;

    write_reg(i2c, addr, 0x03, (FS_MODE << 6) | ADC_OSR)?;
    write_reg(i2c, addr, 0x04, DAC_OSR)?;
    write_reg(i2c, addr, 0x05, ((ADC_DIV - 1) << 4) | (DAC_DIV - 1))?;

    // 0x06: divisor de BCLK. Ojo, el registro lleva `bclk_div - 1` cuando es < 19.
    // (Escribir el valor sin restar deja el reloj de bit mal y el DAC no suena.)
    let reg06 = read_reg(i2c, addr, 0x06)?;
    let bclk_field = if BCLK_DIV < 19 { BCLK_DIV - 1 } else { BCLK_DIV };
    write_reg(i2c, addr, 0x06, (reg06 & 0xE0) | bclk_field)?;

    // 0x07: parte alta del divisor de LRCK, conservando los 2 bits altos.
    let reg07 = read_reg(i2c, addr, 0x07)?;
    write_reg(i2c, addr, 0x07, (reg07 & 0xC0) | LRCK_H)?;
    write_reg(i2c, addr, 0x08, LRCK_L)?;

    write_all(i2c, addr, &[
        // Formato: esclavo (el ESP32 genera los relojes), I2S, 16 bits.
        (0x00, 0x80),
        (0x09, 0x0C), // resolución de entrada = 16 bits
        (0x0A, 0x0C), // resolución de salida  = 16 bits
        // Cadena analógica. Ninguno es valor por defecto: hay que escribirlos todos.
        (0x0D, 0x01), // enciende la circuitería analógica
        (0x0E, 0x02), // habilita PGA y modulador del ADC
        (0x12, 0x00), // enciende el DAC
        (0x13, 0x10), // habilita la salida al driver del altavoz
        (0x1C, 0x6A),        // bypass del ecualizador del ADC + cancela offset DC
        (0x37, 0x08),        // bypass del ecualizador del DAC
        (0x32, DAC_VOLUME),  // volumen de salida (bajo a propósito)
    ])?;

    // Verifica que el chip quedó realmente encendido y no en power-down.
    let reg00 = read_reg(i2c, addr, 0x00)?;
    if reg00 & 0x80 == 0 {
        bail!("ES8311 quedó en power-down (reg00 = {reg00:#04x}): la secuencia de init falló");
    }

    log::info!("ES8311: OK (reg00 = {reg00:#04x})");
    Ok(())
}

// ============================= TCA9555 =============================

const TCA_CONFIG_PORT1: u8 = 0x07;
const TCA_OUTPUT_PORT1: u8 = 0x03;

/// Enciende o apaga el amplificador (línea P8 = bit 0 del puerto 1).
///
/// Solo se enciende justo antes de reproducir y se apaga en cuanto se acaba. Un
/// amplificador abierto sin querer es precisamente lo que convierte un fallo
/// pequeño en un pitido insoportable.
fn set_amplifier(i2c: &mut I2cDriver, on: bool) -> Result<()> {
    let addr = bi2c::ADDR_TCA9555;
    let bit = 1u8 << (luka_board::expander::PA_ENABLE - 8);

    // Primero el valor de salida, luego conmutar a salida: así el pin no pasa por
    // un estado indeterminado al cambiar de dirección.
    let out = read_reg(i2c, addr, TCA_OUTPUT_PORT1)?;
    let out = if on { out | bit } else { out & !bit };
    write_reg(i2c, addr, TCA_OUTPUT_PORT1, out)?;

    let cfg = read_reg(i2c, addr, TCA_CONFIG_PORT1)?;
    write_reg(i2c, addr, TCA_CONFIG_PORT1, cfg & !bit)?; // 0 = salida

    Ok(())
}

/// Apaga el amplificador ignorando errores.
///
/// Para las rutas de limpieza: si algo ya ha fallado, el objetivo es que el
/// altavoz quede mudo, no propagar un segundo error encima del primero.
fn silence(i2c: &mut I2cDriver) {
    let _ = set_amplifier(i2c, false);
}

// ================================ Main ================================

fn main() -> Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take().context("no se pudieron tomar los periféricos")?;

    log::info!("=== Spike 4/4: loopback de audio ===");
    log::info!(
        "{} Hz, {} bits, tramas de {} ms | MCLK = {}x fs = {} Hz",
        audio::SAMPLE_RATE_HZ,
        audio::BITS_PER_SAMPLE,
        audio::FRAME_MS,
        MCLK_MULTIPLE,
        audio::SAMPLE_RATE_HZ * MCLK_MULTIPLE,
    );

    // --- I²C: configurar los tres chips ---
    let mut i2c = I2cDriver::new(
        peripherals.i2c0,
        peripherals.pins.gpio11, // SDA
        peripherals.pins.gpio10, // SCL
        &I2cConfig::new().baudrate(bi2c::FREQ_HZ.Hz()),
    )
    .context("no se pudo abrir el bus I2C")?;

    es7210_init(&mut i2c)?;
    es8311_init(&mut i2c)?;

    // --- I²S full-duplex ---
    // Un único driver posee RX y TX, porque ES7210 y ES8311 comparten BCLK y WS
    // en el mismo periférico. Es la regla de arquitectura del plan (§5.2).
    let std_config = StdConfig::new(
        Config::default(),
        StdClkConfig::from_sample_rate_hz(audio::SAMPLE_RATE_HZ)
            .mclk_multiple(MclkMultiple::M256),
        StdSlotConfig::philips_slot_default(DataBitWidth::Bits16, SlotMode::Stereo),
        StdGpioConfig::default(),
    );

    let mut i2s = I2sDriver::new_std_bidir(
        peripherals.i2s0,
        &std_config,
        peripherals.pins.gpio13,       // BCLK
        peripherals.pins.gpio15,       // DIN  (ES7210 -> ESP)
        peripherals.pins.gpio16,       // DOUT (ESP -> ES8311)
        Some(peripherals.pins.gpio12), // MCLK
        peripherals.pins.gpio14,       // WS
    )
    .context("no se pudo abrir el I2S (¿pines equivocados?)")?;

    i2s.tx_enable().context("no se pudo habilitar el TX del I2S")?;
    i2s.rx_enable().context("no se pudo habilitar el RX del I2S")?;

    // Arranca con el altavoz MUDO y solo se abre para reproducir.
    set_amplifier(&mut i2c, false)?;

    let result = run_cycles(&mut i2s, &mut i2c);

    // Pase lo que pase, el altavoz queda mudo.
    silence(&mut i2c);
    result
}

/// Bucle principal: grabar en silencio → pitido → reproducir → callar.
fn run_cycles(i2s: &mut I2sDriver<'_, I2sBiDir>, i2c: &mut I2cDriver) -> Result<()> {
    // Estéreo 16 bits: 2 muestras por trama de audio mono.
    let frames_per_sec = (1000 / audio::FRAME_MS) as usize;
    let total_frames = frames_per_sec * CAPTURE_SECS as usize;
    let mut frame = vec![0i16; audio::FRAME_SAMPLES * 2];
    // La grabación entera vive en PSRAM: 4 s estéreo son 256 KB, nada para 8 MB.
    let mut recording = vec![0i16; frame.len() * total_frames];

    loop {
        // ===== Fase 1: grabar con el altavoz apagado =====
        log::info!("--- Fase 1: grabando {CAPTURE_SECS} s (altavoz apagado) — habla ahora ---");
        let (mut sum_sq, mut peak) = (0u64, 0i32);
        for f in 0..total_frames {
            let bytes = unsafe {
                std::slice::from_raw_parts_mut(frame.as_mut_ptr() as *mut u8, frame.len() * 2)
            };
            i2s.read(bytes, BLOCK).context("fallo leyendo del I2S")?;

            let slot = &mut recording[f * frame.len()..(f + 1) * frame.len()];
            for (dst, src) in slot.iter_mut().zip(frame.iter()) {
                let v = (*src as i32 * PLAYBACK_GAIN).clamp(i16::MIN as i32, i16::MAX as i32);
                peak = peak.max(v.abs());
                sum_sq += (v * v) as u64;
                *dst = v as i16;
            }

            if (f + 1) % frames_per_sec == 0 {
                let n = (frame.len() * frames_per_sec) as u64;
                let rms = (sum_sq / n).isqrt() as i32;
                log::info!("  {}s  rms {:5}  pico {:5}  {}", (f + 1) / frames_per_sec, rms, peak, bar(rms));
                sum_sq = 0;
                peak = 0;
            }
        }

        // ===== Fase 2: pitidos =====
        // Valida la salida sin depender del micro: si suena, ES8311 + amplificador +
        // altavoz + relojes están bien, y cualquier fallo es del lado del ADC.
        // Tres tonos distintos y largos: inconfundibles, y si solo se oyeran algunos
        // apuntaría a un problema de reloj y no de amplificador.
        set_amplifier(i2c, true)?;
        log::info!("--- Fase 2: 3 pitidos de prueba (440 / 660 / 880 Hz) ---");
        for freq in [440, 660, 880] {
            beep(i2s, freq, 500)?;
            FreeRtos::delay_ms(150);
        }

        // ===== Fase 3: reproducir lo grabado =====
        // El micro se ignora por completo aquí, así que no hay lazo posible.
        log::info!("--- Fase 3: reproduciendo lo grabado ---");
        for chunk in recording.chunks(frame.len()) {
            let bytes =
                unsafe { std::slice::from_raw_parts(chunk.as_ptr() as *const u8, chunk.len() * 2) };
            i2s.write_all(bytes, BLOCK).context("fallo escribiendo al I2S")?;
        }

        set_amplifier(i2c, false)?;
        log::info!("--- ciclo completo; altavoz mudo, repitiendo en 2 s ---");
        FreeRtos::delay_ms(2000);
    }
}

/// Barra de nivel en texto, en escala **logarítmica (dBFS)**.
///
/// La primera versión era lineal contra el fondo de escala y salía siempre vacía:
/// el habla normal en estos micros ronda los -45 dBFS, que en lineal es un 0,5 %
/// del recorrido. Como el oído es logarítmico, la barra también debe serlo para
/// que "más lleno" signifique "suena más fuerte" de forma útil.
///
/// Rango representado: de -60 dBFS (silencio) a 0 dBFS (saturación).
fn bar(rms: i32) -> String {
    const WIDTH: usize = 30;
    const FLOOR_DB: f32 = -60.0;

    let filled = if rms <= 0 {
        0
    } else {
        let db = 20.0 * (rms as f32 / i16::MAX as f32).log10();
        let frac = ((db - FLOOR_DB) / -FLOOR_DB).clamp(0.0, 1.0);
        (frac * WIDTH as f32).round() as usize
    };
    format!("[{}{}]", "#".repeat(filled), "-".repeat(WIDTH - filled))
}

/// Emite un tono cuadrado suave durante `ms` milisegundos.
fn beep(i2s: &mut I2sDriver<'_, I2sBiDir>, freq_hz: u32, ms: u32) -> Result<()> {
    let total = (audio::SAMPLE_RATE_HZ * ms / 1000) as usize;
    let period = (audio::SAMPLE_RATE_HZ / freq_hz) as usize;
    let amplitude: i16 = 6000; // cómodo, ni tímido ni molesto

    let mut buf = vec![0i16; audio::FRAME_SAMPLES * 2];
    let mut written = 0;
    while written < total {
        for (i, chunk) in buf.chunks_mut(2).enumerate() {
            let sample = if ((written + i) / (period / 2)) % 2 == 0 {
                amplitude
            } else {
                -amplitude
            };
            chunk[0] = sample; // izquierdo
            chunk[1] = sample; // derecho
        }
        let bytes = unsafe {
            std::slice::from_raw_parts(buf.as_ptr() as *const u8, buf.len() * 2)
        };
        i2s.write_all(bytes, BLOCK)?;
        written += audio::FRAME_SAMPLES;
    }
    Ok(())
}
