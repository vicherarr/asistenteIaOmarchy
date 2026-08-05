//! Satélite de voz de Luka — **binario incompleto (Fase 1 en curso)**.
//!
//! Ahora mismo solo hace la puesta en marcha del hardware: I²C, expansor (con el
//! amplificador apagado) y los dos codecs. Sirve para comprobar que la cadena de
//! inicialización funciona fuera de los spikes, y es el esqueleto sobre el que
//! van los cuatro módulos que faltan.
//!
//! Pendiente, por este orden (ver `PROGRESO.md`):
//!   1. `audio.rs` — hilo dueño del I²S, half-duplex.
//!   2. `net.rs`   — WiFi + cliente WebSocket con el certificado fijado.
//!   3. `ring.rs`  — hilo del anillo de LEDs a 50 fps.
//!   4. el supervisor que une `luka_state::next` con las acciones.
//!
//! # Seguridad
//!
//! Lo primero que se hace tras abrir el I²C es **forzar el amplificador a
//! apagado**, y no hay ninguna ruta que lo encienda todavía. Mientras el hilo de
//! audio no exista, este binario no puede producir sonido.

mod board;

use anyhow::{Context, Result};
use esp_idf_hal::i2c::{I2cConfig, I2cDriver};
use esp_idf_hal::peripherals::Peripherals;
use esp_idf_hal::units::FromValueType;
use luka_board::{i2c as bi2c, PINOUT_VERIFIED};
use std::sync::{Arc, Mutex};

// Los pines se nombran literalmente más abajo (cada GPIO es un tipo distinto en
// esp-idf-hal), así que si alguien cambia el mapa del BSP esto no compila.
const _: () = assert!(bi2c::SDA == 11 && bi2c::SCL == 10);

fn main() -> Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    log::info!("luka-firmware {} arrancando", env!("CARGO_PKG_VERSION"));
    log::info!("config: {}", luka_config::summary());
    if !PINOUT_VERIFIED {
        log::warn!("El mapa de pines NO está verificado contra esta placa.");
    }

    let peripherals = Peripherals::take()?;

    let i2c = I2cDriver::new(
        peripherals.i2c0,
        peripherals.pins.gpio11,
        peripherals.pins.gpio10,
        &I2cConfig::new().baudrate(bi2c::FREQ_HZ.Hz()),
    )
    .context("abriendo el bus I2C")?;
    let i2c: board::SharedI2c = Arc::new(Mutex::new(i2c));

    {
        let mut guard = i2c.lock().expect("el mutex del I2C no debería estar envenenado");
        // Amplificador apagado ANTES que nada. Todo lo demás puede fallar sin
        // consecuencias; el altavoz abierto por error, no.
        board::init_expander(&mut guard).context("inicializando el TCA9555")?;
        board::es7210_init(&mut guard).context("inicializando el ES7210")?;
        board::es8311_init(&mut guard).context("inicializando el ES8311")?;

        let botones = board::read_buttons(&mut guard).context("leyendo los botones")?;
        log::info!("Botones en reposo: {botones:?} (ninguno debería estar pulsado)");
    }

    log::info!("Hardware listo. El resto de la Fase 1 está sin implementar todavía.");
    board::silence(&i2c);
    Ok(())
}
