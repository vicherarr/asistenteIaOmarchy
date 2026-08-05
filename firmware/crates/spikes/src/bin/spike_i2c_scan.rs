//! Spike 1/4 — Escaneo del bus I²C.
//!
//! **Qué responde:** ¿son correctos los pines SDA/SCL del mapa (`luka-board`), y
//! están en el bus los cuatro chips esperados (ES8311, TCA9555, ES7210, PCF85063)?
//!
//! Es la primera prueba de la Fase 0 porque es concluyente y barata: si aparecen
//! las cuatro direcciones esperadas, los pines I²C quedan confirmados de golpe. Si
//! no aparece nada, SDA/SCL están mal (o intercambiados) y no tiene sentido seguir.
//!
//! ```bash
//! cargo run -p spikes --bin spike_i2c_scan
//! ```

use anyhow::{Context, Result};
use esp_idf_hal::delay::BLOCK;
use esp_idf_hal::i2c::{I2cConfig, I2cDriver};
use esp_idf_hal::peripherals::Peripherals;
// `FromValueType` es lo que aporta el sufijo `.Hz()` sobre enteros.
use esp_idf_hal::units::FromValueType;

// El tipo de cada pin es distinto en esp-idf-hal, así que abajo hay que nombrar
// `gpio11`/`gpio10` literalmente. Estas comprobaciones fallan en COMPILACIÓN si
// alguien cambia el mapa en `luka-board` y se olvida de este archivo.
const _: () = assert!(luka_board::i2c::SDA == 11);
const _: () = assert!(luka_board::i2c::SCL == 10);

/// Rango de direcciones válido para dispositivos I²C de 7 bits.
/// 0x00-0x02 y 0x78-0x7F están reservadas por el estándar.
const ADDR_FIRST: u8 = 0x03;
const ADDR_LAST: u8 = 0x77;

fn main() -> Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take().context("no se pudieron tomar los periféricos")?;

    log::info!("=== Spike 1/4: escaneo I2C ===");
    log::info!(
        "SDA=GPIO{}  SCL=GPIO{}  @{} kHz",
        luka_board::i2c::SDA,
        luka_board::i2c::SCL,
        luka_board::i2c::FREQ_HZ / 1000
    );

    let config = I2cConfig::new().baudrate(luka_board::i2c::FREQ_HZ.Hz());
    let mut i2c = I2cDriver::new(
        peripherals.i2c0,
        peripherals.pins.gpio11, // SDA
        peripherals.pins.gpio10, // SCL
        &config,
    )
    .context("no se pudo abrir el bus I2C (¿pines equivocados?)")?;

    let mut found = [false; 128];
    let mut count = 0;

    for addr in ADDR_FIRST..=ADDR_LAST {
        // Sonda estándar: una lectura de 1 byte. Si alguien responde con ACK, hay
        // un chip ahí. El valor leído da igual (y suele ser basura).
        let mut buf = [0u8; 1];
        if i2c.read(addr, &mut buf, BLOCK).is_ok() {
            found[addr as usize] = true;
            count += 1;
        }
    }

    // --- Informe ---
    log::info!("--- Dispositivos encontrados: {count} ---");
    for addr in ADDR_FIRST..=ADDR_LAST {
        if found[addr as usize] {
            let known = luka_board::i2c::EXPECTED
                .iter()
                .find(|(a, _)| *a == addr)
                .map(|(_, name)| *name)
                .unwrap_or("desconocido (no estaba en el mapa)");
            log::info!("  {addr:#04x}  {known}");
        }
    }

    log::info!("--- Contraste con el mapa esperado ---");
    let mut all_present = true;
    for (addr, name) in luka_board::i2c::EXPECTED {
        if found[*addr as usize] {
            log::info!("  OK      {addr:#04x}  {name}");
        } else {
            log::error!("  FALTA   {addr:#04x}  {name}");
            all_present = false;
        }
    }

    if count == 0 {
        log::error!("");
        log::error!("No respondió NADIE en el bus. Casi siempre es una de estas:");
        log::error!("  - SDA y SCL intercambiados: prueba SDA=GPIO10, SCL=GPIO11.");
        log::error!("  - Los pines no son los del mapa (Waveshare no publica la tabla).");
        log::error!("  - La placa no está alimentada por el puerto correcto.");
    } else if all_present {
        log::info!("");
        log::info!("RESULTADO: los 4 chips responden. Pines I2C CONFIRMADOS.");
        log::info!("Siguiente paso: pon i2c::CONFIDENCE = Verified en luka-board.");
    } else {
        log::warn!("");
        log::warn!("RESULTADO: el bus funciona pero el mapa no cuadra del todo.");
        log::warn!("Apunta las direcciones reales de arriba y corrige luka-board.");
    }

    // Se queda vivo: si `main` retorna, el ESP-IDF reinicia y no da tiempo a leer.
    loop {
        std::thread::sleep(std::time::Duration::from_secs(60));
    }
}
