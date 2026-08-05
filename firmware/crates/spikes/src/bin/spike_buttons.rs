//! **Spike: los botones del TCA9555.**
//!
//! Es lo único del mapa de pines que la Fase 0 dejó sin confirmar, y la Fase 1
//! entera se apoya en él: el push-to-talk *es* un botón.
//!
//! No da por buenas las líneas P9/P10/P11 de `luka-board` (que vienen de
//! documentación de terceros, no del hardware): vigila **los 16 pines del
//! expansor** y dice cuáles se mueven. Así, si están en otro sitio, sale aquí en
//! vez de en medio del firmware, donde parecería un fallo del WebSocket.
//!
//! # Seguridad
//!
//! Este spike **no toca el audio en absoluto**. Lo primero que hace es forzar el
//! amplificador a apagado, y no hay ni I²S ni reproducción en todo el programa:
//! por construcción no puede producir acople ni ruido.

use anyhow::{Context, Result};
use esp_idf_hal::delay::BLOCK;
use esp_idf_hal::i2c::{I2cConfig, I2cDriver};
use esp_idf_hal::peripherals::Peripherals;
use esp_idf_hal::units::FromValueType;
use luka_board::{expander, i2c as bi2c};
use std::thread::sleep;
use std::time::{Duration, Instant};

// El mapa de este spike tiene que coincidir con el del BSP.
const _: () = assert!(bi2c::SDA == 11 && bi2c::SCL == 10);

// --- Registros del TCA9555 ---
const REG_INPUT_0: u8 = 0x00;
const REG_OUTPUT_0: u8 = 0x02;
const REG_POLARITY_0: u8 = 0x04;
const REG_CONFIG_0: u8 = 0x06;

/// Cuánto se vigila antes de resumir.
const OBSERVE_SECS: u64 = 90;
/// Los botones mecánicos rebotan unos pocos ms; 20 ms de periodo los absorbe sin
/// perder pulsaciones cortas.
const POLL_MS: u64 = 20;

fn write_reg(i2c: &mut I2cDriver, reg: u8, val: u8) -> Result<()> {
    i2c.write(bi2c::ADDR_TCA9555, &[reg, val], BLOCK)
        .with_context(|| format!("escribiendo el registro {reg:#04x} del TCA9555"))
}

fn read_reg(i2c: &mut I2cDriver, reg: u8) -> Result<u8> {
    let mut buf = [0u8; 1];
    i2c.write_read(bi2c::ADDR_TCA9555, &[reg], &mut buf, BLOCK)
        .with_context(|| format!("leyendo el registro {reg:#04x} del TCA9555"))?;
    Ok(buf[0])
}

/// Los 16 pines del expansor como un solo `u16` (P0 en el bit 0).
fn read_all(i2c: &mut I2cDriver) -> Result<u16> {
    let p0 = read_reg(i2c, REG_INPUT_0)? as u16;
    let p1 = read_reg(i2c, REG_INPUT_0 + 1)? as u16;
    Ok(p0 | (p1 << 8))
}

/// Deja el expansor en un estado seguro y observable.
///
/// Orden deliberado: **primero** el amplificador a 0, y solo después se configura
/// como salida. Al revés, el pin pasaría por un instante en estado indefinido con
/// el amplificador ya en modo salida, que es exactamente cuando podría abrirse.
fn setup(i2c: &mut I2cDriver) -> Result<()> {
    let pa_bit = 1u8 << (expander::PA_ENABLE - 8);

    let out1 = read_reg(i2c, REG_OUTPUT_0 + 1)?;
    write_reg(i2c, REG_OUTPUT_0 + 1, out1 & !pa_bit)?;

    // Sin inversión de polaridad: se quiere ver el nivel eléctrico tal cual, para
    // poder deducir si los botones son activos a nivel alto o bajo.
    write_reg(i2c, REG_POLARITY_0, 0x00)?;
    write_reg(i2c, REG_POLARITY_0 + 1, 0x00)?;

    // Todo entradas menos el amplificador (1 = entrada en el TCA9555).
    write_reg(i2c, REG_CONFIG_0, 0xFF)?;
    write_reg(i2c, REG_CONFIG_0 + 1, 0xFF & !pa_bit)?;

    log::info!("Amplificador FORZADO A APAGADO. Este spike no reproduce nada.");
    Ok(())
}

fn describe(line: u8) -> &'static str {
    match line {
        expander::PA_ENABLE => " <- PA_ENABLE (salida, no es un botón)",
        expander::BUTTON_1 => " <- BUTTON_1 según el BSP",
        expander::BUTTON_2 => " <- BUTTON_2 según el BSP",
        expander::BUTTON_3 => " <- BUTTON_3 según el BSP",
        _ => "",
    }
}

fn bits(value: u16) -> String {
    (0..16).rev().map(|i| if value >> i & 1 == 1 { '1' } else { '0' }).collect()
}

fn main() -> Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take()?;
    let mut i2c = I2cDriver::new(
        peripherals.i2c0,
        peripherals.pins.gpio11,
        peripherals.pins.gpio10,
        &I2cConfig::new().baudrate(bi2c::FREQ_HZ.Hz()),
    )
    .context("abriendo el bus I2C")?;

    setup(&mut i2c)?;

    let reposo = read_all(&mut i2c).context("leyendo el estado de reposo")?;
    log::info!("");
    log::info!("=== Spike de botones ===");
    log::info!("Reposo (P15..P0): {}", bits(reposo));
    log::info!("");
    log::info!("PULSA CADA BOTÓN varias veces, uno a uno, durante {OBSERVE_SECS} s.");
    log::info!("Prueba también pulsaciones largas: sirve para el push-to-talk.");
    log::info!("");

    // Por línea: cuántos flancos ha dado y a qué nivel se va al pulsarse.
    let mut cambios = [0u32; 16];
    let mut nivel_activo = [None::<u8>; 16];
    let mut previo = reposo;

    let inicio = Instant::now();
    while inicio.elapsed() < Duration::from_secs(OBSERVE_SECS) {
        sleep(Duration::from_millis(POLL_MS));

        let ahora = match read_all(&mut i2c) {
            Ok(v) => v,
            // Un fallo puntual del bus no debe abortar la observación entera:
            // reconectar el cable a mitad de prueba es de lo más normal.
            Err(e) => {
                log::warn!("lectura fallida: {e:#}");
                continue;
            }
        };
        if ahora == previo {
            continue;
        }

        for line in 0..16u8 {
            let antes = previo >> line & 1;
            let despues = ahora >> line & 1;
            if antes == despues {
                continue;
            }
            cambios[line as usize] += 1;
            // El nivel al que va cuando se separa del reposo es el nivel activo.
            if despues as u16 != (reposo >> line & 1) {
                nivel_activo[line as usize] = Some(despues as u8);
            }
            log::info!(
                "P{line:<2} {} -> {}{}",
                antes,
                despues,
                describe(line)
            );
        }
        previo = ahora;
    }

    // ------------------------------------------------------------- resumen
    log::info!("");
    log::info!("=== Resumen ===");
    let mut activas: Vec<u8> = (0..16u8).filter(|l| cambios[*l as usize] > 0).collect();
    activas.sort_unstable();

    if activas.is_empty() {
        log::error!("Ninguna línea del expansor se movió.");
        log::error!("O no se pulsó nada, o los botones NO cuelgan del TCA9555.");
        log::error!("Siguiente cosa que mirar: que vayan a GPIOs directos del ESP32.");
    } else {
        for line in &activas {
            let nivel = match nivel_activo[*line as usize] {
                Some(0) => "activo a nivel BAJO (pull-up, lo normal)",
                Some(_) => "activo a nivel ALTO",
                None => "nivel activo indeterminado",
            };
            log::info!(
                "P{:<2}: {} flancos, {}{}",
                line,
                cambios[*line as usize],
                nivel,
                describe(*line)
            );
        }

        let esperadas = [expander::BUTTON_1, expander::BUTTON_2, expander::BUTTON_3];
        if activas == esperadas {
            log::info!("");
            log::info!("El BSP acierta: los botones son P9/P10/P11.");
            log::info!("Marca `expander::CONFIDENCE` como Verified en luka-board.");
        } else {
            log::warn!("");
            log::warn!("El BSP dice {esperadas:?} y se movieron {activas:?}.");
            log::warn!("CORRIGE luka-board::expander con lo que sale aquí, y solo ahí.");
        }
    }

    // El amplificador ya estaba apagado; se reafirma por si acaso, que es barato.
    let pa_bit = 1u8 << (expander::PA_ENABLE - 8);
    if let Ok(out1) = read_reg(&mut i2c, REG_OUTPUT_0 + 1) {
        let _ = write_reg(&mut i2c, REG_OUTPUT_0 + 1, out1 & !pa_bit);
    }

    log::info!("");
    log::info!("Fin. El amplificador queda apagado.");
    Ok(())
}
