//! Configuración del firmware, empotrada en compilación desde `firmware/cfg.toml`.
//!
//! Todo son `const`: no hay parsing ni asignaciones en runtime, y un valor mal
//! puesto (un puerto que no es número, una sección que falta) rompe el build en
//! vez de romper el dispositivo.
//!
//! Los secretos viven en `cfg.toml`, que **no se versiona**. Ver `cfg.toml.example`.

#![no_std]
#![deny(unsafe_code)]

/// Convierte un literal decimal a `u32` en tiempo de compilación.
///
/// Existe porque `env!()` entrega `&str` y necesitamos `const` numéricas. Al ser
/// `const fn`, un valor no numérico en `cfg.toml` aborta la compilación.
const fn parse_u32(s: &str) -> u32 {
    let bytes = s.as_bytes();
    assert!(!bytes.is_empty(), "valor numérico vacío en cfg.toml");

    let mut acc: u32 = 0;
    let mut i = 0;
    while i < bytes.len() {
        let d = bytes[i];
        assert!(d >= b'0' && d <= b'9', "valor no numérico en cfg.toml");
        acc = acc * 10 + (d - b'0') as u32;
        i += 1;
    }
    acc
}

/// Red WiFi a la que se asocia el dispositivo.
pub mod wifi {
    pub const SSID: &str = env!("LUKA_WIFI_SSID");
    pub const PASSWORD: &str = env!("LUKA_WIFI_PASSWORD");
}

/// Servidor del asistente (el PC donde corre `asistenteia`).
pub mod server {
    pub const HOST: &str = env!("LUKA_SERVER_HOST");
    pub const PORT: u16 = super::parse_u32(env!("LUKA_SERVER_PORT")) as u16;
    /// Debe coincidir con el `API_TOKEN` del `.env` del asistente.
    pub const API_TOKEN: &str = env!("LUKA_API_TOKEN");

    /// Certificado del servidor, **fijado** (*pinning*) en el binario.
    ///
    /// El firmware no confía en ninguna autoridad certificadora: solo acepta este
    /// certificado exacto. Es lo apropiado aquí, porque el del asistente es
    /// autofirmado y con `CN=localhost`, así que jamás pasaría una validación
    /// normal contra una IP de la red local. Fijarlo da una garantía **más
    /// fuerte** que la validación habitual: no basta con que alguien presente un
    /// certificado válido, tiene que ser este.
    ///
    /// Termina en NUL porque es lo que espera la capa TLS del ESP-IDF.
    /// Se copia del asistente con `firmware/scripts/sync-cert.sh`.
    pub const TLS_CERT_PEM: &str = concat!(include_str!(env!("LUKA_TLS_CERT_PATH")), "\0");

    /// URL del WebSocket del dispositivo.
    pub const WS_PATH: &str = "/device/ws";
}

/// Identidad y preferencias del propio dispositivo.
pub mod device {
    pub const NAME: &str = env!("LUKA_DEVICE_NAME");
    /// Brillo del anillo RGB (0-255). Se aplica tras la corrección gamma.
    pub const LED_BRIGHTNESS: u8 = super::parse_u32(env!("LUKA_LED_BRIGHTNESS")) as u8;

    /// Umbral de la wake word (0-255) sobre la media de la ventana.
    ///
    /// Alto por defecto: "Luka" es una palabra corta y el fallo que se paga
    /// caro es el falso positivo. Se baja con el modo calibración delante.
    pub const WAKE_THRESHOLD: u8 = super::parse_u32(env!("LUKA_WAKE_THRESHOLD")) as u8;

    /// Modo calibración: el anillo enseña en violeta la confianza del detector
    /// mientras está en reposo, para poder ajustar el umbral a ojo.
    ///
    /// Apagado por defecto porque en uso normal el reposo debe ser discreto.
    pub const WAKE_CALIBRATION: bool = super::parse_u32(env!("LUKA_WAKE_CALIBRATION")) != 0;
}

/// Resumen apto para log: confirma que la config llegó **sin filtrar secretos**.
///
/// Nunca incluye la contraseña ni el token; solo su longitud, que es justo lo que
/// hace falta para distinguir "está vacío" de "está mal".
pub fn summary() -> impl core::fmt::Display {
    struct Summary;
    impl core::fmt::Display for Summary {
        fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
            write!(
                f,
                "wifi.ssid={} wifi.password=<{} chars> server={}:{} api_token=<{} chars> device={}",
                wifi::SSID,
                wifi::PASSWORD.len(),
                server::HOST,
                server::PORT,
                server::API_TOKEN.len(),
                device::NAME,
            )
        }
    }
    Summary
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_u32_acepta_decimales() {
        assert_eq!(parse_u32("0"), 0);
        assert_eq!(parse_u32("8765"), 8765);
        assert_eq!(parse_u32("255"), 255);
    }

    #[test]
    #[should_panic(expected = "valor no numérico")]
    fn parse_u32_rechaza_basura() {
        parse_u32("80a");
    }

    /// El certificado fijado tiene que estar bien formado y terminado en NUL, o la
    /// capa TLS del ESP-IDF lo rechaza con un error que no dice nada útil.
    #[test]
    fn el_certificado_fijado_es_valido() {
        let pem = server::TLS_CERT_PEM;
        assert!(pem.contains("-----BEGIN CERTIFICATE-----"), "no parece un PEM");
        assert!(pem.contains("-----END CERTIFICATE-----"), "PEM incompleto");
        assert!(pem.ends_with('\0'), "el PEM debe terminar en NUL para esp-tls");
        // Un certificado RSA-4096 ronda los 1,8 KB; muy por debajo sería un fichero
        // truncado o un placeholder.
        assert!(pem.len() > 500, "el certificado parece truncado ({} bytes)", pem.len());
    }

    /// Detecta el error tonto pero caro: compilar con la plantilla sin rellenar.
    #[test]
    fn la_config_no_tiene_placeholders() {
        assert!(!wifi::SSID.is_empty(), "wifi.ssid vacío");
        assert_ne!(wifi::SSID, "TU_SSID", "cfg.toml sigue siendo la plantilla");
        assert!(!server::API_TOKEN.is_empty(), "server.api_token vacío");
        assert!(server::PORT > 0);
    }
}
