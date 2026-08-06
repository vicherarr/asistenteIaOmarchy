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

    /// Interrumpir a Luka diciendo "Luka" mientras habla. Tres posiciones:
    ///
    /// - `0` — apagado. El micro no sale del hilo de audio mientras suena el
    ///   altavoz, exactamente como hasta la Fase 3.
    /// - `1` — **solo medir**. El detector escucha durante la reproducción y
    ///   registra por el log hasta dónde llega la confianza, pero no puede
    ///   despertar pase lo que pase. Es la posición con la que se averigua
    ///   cuánto se parece a "Luka" la propia voz de Luka, que es el único dato
    ///   con el que se puede elegir [`WAKE_THRESHOLD_BARGE`] sin adivinar.
    /// - `2` — activo.
    ///
    /// Esto **no es el acople de la Fase 0**, y la diferencia es la que permite
    /// que exista: aquel era un lazo cerrado (micro → altavoz → micro) que se
    /// realimentaba hasta el pitido. Aquí lo capturado va al detector y **se
    /// tira**; nada de lo que entra por el micro se reproduce. El camino es
    /// abierto y no hay nada que pueda diverger. Lo que sí puede pasar es que
    /// Luka se dispare oyéndose a sí misma, que corta una respuesta y no rompe
    /// nada.
    pub const BARGE_IN: u8 = super::parse_u32(env!("LUKA_BARGE_IN")) as u8;

    /// Umbral de la wake word **mientras Luka habla**, más alto que el normal.
    ///
    /// Quien interrumpe está cerca y alza la voz; la de Luka llega al micro
    /// atenuada y coloreada por el altavoz. Pedir más aquí cuesta tener que
    /// decirlo con algo más de intención, y a cambio evita el fallo que se paga:
    /// cortarse a sí misma a mitad de una respuesta larga.
    pub const WAKE_THRESHOLD_BARGE: u8 = super::parse_u32(env!("LUKA_WAKE_THRESHOLD_BARGE")) as u8;
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
