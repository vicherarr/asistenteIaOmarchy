//! Spike 2/4 — Anillo de LEDs RGB (WS2812 sobre RMT).
//!
//! **Qué responde:** ¿es GPIO38 la línea de datos, cuántos LEDs hay realmente y en
//! qué orden físico están? También valida el orden de color (GRB vs RGB), que en
//! los WS2812 es una fuente clásica de "el rojo sale verde".
//!
//! Se ejecuta pronto porque a partir de aquí el anillo sirve de instrumento de
//! depuración para todo lo demás: es la única salida del dispositivo sin cable.
//!
//! **Qué mirar:**
//!   1. Fase A — recuento: los LEDs se encienden **uno a uno**. Cuéntalos.
//!   2. Fase B — color: el anillo entero va a rojo, luego verde, luego azul.
//!      Si los ves cambiados, hay que corregir el orden de canales.
//!   3. Fase C — gamma: una respiración lenta. Debe subir y bajar suave, sin
//!      saltos ni parpadeo al final del apagado.
//!
//! ```bash
//! cargo run -p spikes --bin spike_rgb
//! ```

use anyhow::{Context, Result};
use esp_idf_hal::peripherals::Peripherals;
use smart_leds::{SmartLedsWrite, RGB8};
use std::thread::sleep;
use std::time::Duration;
use ws2812_esp32_rmt_driver::lib_smart_leds::Ws2812Esp32Rmt;

const _: () = assert!(luka_board::leds::DATA == 38);

/// Cuántos LEDs recorre la fase de recuento.
///
/// Se prueban más de los 7 documentados a propósito: si la placa tuviera 8 o 12,
/// la fase de recuento lo revela en vez de dejar LEDs muertos sin explicación.
const PROBE_COUNT: usize = 16;

/// Corrección gamma (γ≈2.6) para WS2812.
///
/// Los LEDs son lineales en PWM pero el ojo no: sin esta tabla, un brillo del 3 %
/// se percibe como un 25 % y las respiraciones salen a escalones. Se calcula en
/// compilación, así que no cuesta nada en runtime.
const GAMMA: [u8; 256] = {
    let mut table = [0u8; 256];
    let mut i = 0;
    while i < 256 {
        // Aproximación entera de (i/255)^2.6 * 255, sin floats en contexto const.
        let x = i as u32;
        let x2 = x * x / 255; // ^2
        let x3 = x2 * x / 255; // ^3
        // Mezcla ponderada entre ^2 y ^3 ≈ ^2.6
        table[i] = ((x2 * 2 + x3 * 3) / 5) as u8;
        i += 1;
    }
    table
};

/// Aplica gamma y reordena los canales al cableado real de la placa.
///
/// El intercambio rojo/verde vive en `luka-board` (medido en la primera pasada de
/// este spike), no aquí: así el resto del firmware razona siempre en RGB normal.
fn to_wire(color: RGB8) -> RGB8 {
    let (r, g, b) = luka_board::leds::to_wire(
        GAMMA[color.r as usize],
        GAMMA[color.g as usize],
        GAMMA[color.b as usize],
    );
    RGB8 { r, g, b }
}

/// Escala un color al brillo configurado en `cfg.toml`.
fn dim(color: RGB8, brightness: u8) -> RGB8 {
    let scale = |c: u8| ((c as u16 * brightness as u16) / 255) as u8;
    RGB8 {
        r: scale(color.r),
        g: scale(color.g),
        b: scale(color.b),
    }
}

fn main() -> Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take().context("no se pudieron tomar los periféricos")?;
    let brightness = luka_config::device::LED_BRIGHTNESS;

    log::info!("=== Spike 2/4: anillo RGB ===");
    log::info!(
        "DATA=GPIO{}  LEDs esperados={}  brillo={brightness}",
        luka_board::leds::DATA,
        luka_board::leds::COUNT
    );

    let mut ring = Ws2812Esp32Rmt::new(peripherals.rmt.channel0, peripherals.pins.gpio38)
        .context("no se pudo abrir el driver RMT del WS2812")?;

    let off = RGB8::default();
    let write = |ring: &mut Ws2812Esp32Rmt, pixels: &[RGB8]| -> Result<()> {
        let out: Vec<RGB8> = pixels.iter().map(|c| to_wire(dim(*c, brightness))).collect();
        ring.write(out.into_iter())
            .map_err(|e| anyhow::anyhow!("fallo escribiendo al anillo: {e:?}"))
    };

    loop {
        // --- Fase A: recuento. Uno a uno, en blanco, para poder contarlos. ---
        log::info!("Fase A — recuento: cuenta cuántos LEDs se encienden");
        for i in 0..PROBE_COUNT {
            let mut pixels = vec![off; PROBE_COUNT];
            pixels[i] = RGB8 { r: 255, g: 255, b: 255 };
            write(&mut ring, &pixels)?;
            log::info!("  LED #{i}");
            sleep(Duration::from_millis(400));
        }

        // --- Fase B: orden de color. R, G, B en todo el anillo. ---
        log::info!("Fase B — color: debes ver ROJO, luego VERDE, luego AZUL");
        for (name, color) in [
            ("ROJO", RGB8 { r: 255, g: 0, b: 0 }),
            ("VERDE", RGB8 { r: 0, g: 255, b: 0 }),
            ("AZUL", RGB8 { r: 0, g: 0, b: 255 }),
        ] {
            log::info!("  {name}");
            write(&mut ring, &vec![color; luka_board::leds::COUNT])?;
            sleep(Duration::from_millis(1200));
        }

        // --- Fase C: gamma. Respiración suave, sin escalones. ---
        log::info!("Fase C — gamma: respiración suave en cian");
        for step in (0..=255).chain((0..255).rev()) {
            let color = RGB8 { r: 0, g: step, b: step };
            write(&mut ring, &vec![color; luka_board::leds::COUNT])?;
            sleep(Duration::from_millis(4));
        }

        write(&mut ring, &vec![off; PROBE_COUNT])?;
        log::info!("--- ciclo completo, repitiendo en 2 s ---");
        sleep(Duration::from_secs(2));
    }
}
