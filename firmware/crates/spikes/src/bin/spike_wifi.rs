//! Spike 3/4 — WiFi y alcance del servidor.
//!
//! **Qué responde:** ¿se asocia la placa a la WiFi con las credenciales de
//! `cfg.toml`, qué IP y qué cobertura tiene, y **llega al PC donde corre el
//! asistente**? Lo último es lo que de verdad importa: asociarse a la red y no
//! alcanzar el servidor son fallos distintos con arreglos distintos.
//!
//! Prueba el TCP contra el puerto del asistente (8765) en vez de un simple ping,
//! porque es exactamente el camino que usará el firmware — y de paso detecta el
//! caso clásico de `HOST=127.0.0.1` en el `.env`, donde la red va bien pero el
//! servidor no escucha en la LAN.
//!
//! ```bash
//! cargo run -p spikes --bin spike_wifi
//! ```

use anyhow::{Context, Result};
use esp_idf_svc::eventloop::EspSystemEventLoop;
use esp_idf_svc::hal::peripherals::Peripherals;
use esp_idf_svc::nvs::EspDefaultNvsPartition;
use esp_idf_svc::wifi::{AuthMethod, BlockingWifi, ClientConfiguration, Configuration, EspWifi};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

/// Margen para asociarse y obtener IP por DHCP.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(20);
/// Un TCP en LAN se abre en milisegundos; 5 s ya es un "no está".
const TCP_TIMEOUT: Duration = Duration::from_secs(5);

fn main() -> Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    log::info!("=== Spike 3/4: WiFi ===");
    log::info!("config: {}", luka_config::summary());

    let peripherals = Peripherals::take().context("no se pudieron tomar los periféricos")?;
    let sys_loop = EspSystemEventLoop::take()?;
    let nvs = EspDefaultNvsPartition::take()?;

    let mut wifi = BlockingWifi::wrap(
        EspWifi::new(peripherals.modem, sys_loop.clone(), Some(nvs))?,
        sys_loop,
    )?;

    wifi.set_configuration(&Configuration::Client(ClientConfiguration {
        ssid: luka_config::wifi::SSID
            .try_into()
            .map_err(|_| anyhow::anyhow!("el SSID supera los 32 caracteres"))?,
        password: luka_config::wifi::PASSWORD
            .try_into()
            .map_err(|_| anyhow::anyhow!("la contraseña supera los 64 caracteres"))?,
        auth_method: AuthMethod::WPA2Personal,
        ..Default::default()
    }))?;

    wifi.start()?;
    log::info!("radio arrancada; asociando a «{}»…", luka_config::wifi::SSID);

    // `connect()` bloquea hasta asociarse; el timeout lo impone el propio driver.
    let _ = CONNECT_TIMEOUT;
    wifi.connect().context(
        "no se pudo asociar. Revisa el SSID/clave de cfg.toml y que el router \
         emita en 2,4 GHz (el ESP32-S3 NO habla 5 GHz)",
    )?;
    log::info!("asociado; esperando IP por DHCP…");
    wifi.wait_netif_up().context("asociado pero sin IP: ¿DHCP del router?")?;

    let ip_info = wifi.wifi().sta_netif().get_ip_info()?;
    log::info!("--- Red ---");
    log::info!("  IP        {}", ip_info.ip);
    log::info!("  máscara   {:?}", ip_info.subnet.mask);
    log::info!("  gateway   {}", ip_info.subnet.gateway);

    // La cobertura importa: por debajo de -75 dBm el audio empieza a cortarse.
    match wifi.wifi().get_rssi() {
        Ok(rssi) => {
            let quality = match rssi {
                r if r >= -55 => "excelente",
                r if r >= -65 => "buena",
                r if r >= -75 => "justa (puede cortar el audio)",
                _ => "MALA: acerca la placa al router",
            };
            log::info!("  RSSI      {rssi} dBm ({quality})");
        }
        Err(e) => log::warn!("  RSSI      no disponible: {e}"),
    }

    // --- Lo importante: ¿se alcanza el asistente? ---
    let host = luka_config::server::HOST;
    let port = luka_config::server::PORT;
    log::info!("--- Servidor ---");
    log::info!("  probando TCP a {host}:{port}…");

    let addr: SocketAddr = format!("{host}:{port}")
        .parse()
        .with_context(|| format!("server.host de cfg.toml no es una IP válida: {host}"))?;

    match TcpStream::connect_timeout(&addr, TCP_TIMEOUT) {
        Ok(_) => {
            log::info!("  OK: el asistente acepta conexiones. Camino de red LIMPIO.");
        }
        Err(e) => {
            log::error!("  FALLO: {e}");
            log::error!("");
            log::error!("La placa está en la red pero no llega al asistente. Mira:");
            log::error!("  1. ¿Corre el asistente?            asistenteia status");
            log::error!("  2. HOST del .env: si es 127.0.0.1, solo escucha en el propio PC.");
            log::error!("     Para que la placa entre hace falta 0.0.0.0 (o la IP de la LAN).");
            log::error!("  3. ¿Firewall del PC bloqueando el puerto {port}?");
            log::error!("  4. ¿Es {host} la IP correcta del PC? (cambia con DHCP)");
        }
    }

    log::info!("--- fin del spike; la radio queda activa ---");
    loop {
        std::thread::sleep(Duration::from_secs(60));
    }
}
