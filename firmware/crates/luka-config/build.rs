//! Lee `firmware/cfg.toml` y lo expone al crate como variables de entorno de
//! compilación, de modo que `lib.rs` pueda convertirlas en `const` con `env!()`.
//!
//! Ventajas frente a leer TOML en el dispositivo: cero parsing en runtime, cero
//! dependencias en el binario, y un fallo de configuración se detecta al compilar
//! en vez de a las tres de la mañana con la placa montada en la pared.

use std::path::{Path, PathBuf};

/// Sube por el árbol de directorios buscando `cfg.toml`.
///
/// Se busca en vez de fijar la ruta porque el crate puede compilarse desde la
/// raíz del workspace, desde su propio directorio o desde el repo padre.
fn find_cfg(start: &Path) -> Option<PathBuf> {
    let mut dir = Some(start);
    while let Some(d) = dir {
        let candidate = d.join("cfg.toml");
        if candidate.is_file() {
            return Some(candidate);
        }
        dir = d.parent();
    }
    None
}

/// Extrae `tabla.clave` del TOML, con un mensaje de error que dice exactamente
/// qué falta y dónde arreglarlo.
fn get<'a>(doc: &'a toml::Value, table: &str, key: &str) -> &'a toml::Value {
    doc.get(table)
        .unwrap_or_else(|| panic!("cfg.toml: falta la sección [{table}]"))
        .get(key)
        .unwrap_or_else(|| panic!("cfg.toml: falta la clave '{key}' en [{table}]"))
}

fn emit_str(doc: &toml::Value, table: &str, key: &str, env_name: &str) {
    let value = get(doc, table, key)
        .as_str()
        .unwrap_or_else(|| panic!("cfg.toml: [{table}].{key} debe ser texto entre comillas"));
    println!("cargo:rustc-env={env_name}={value}");
}

fn emit_int(doc: &toml::Value, table: &str, key: &str, env_name: &str) {
    let value = get(doc, table, key)
        .as_integer()
        .unwrap_or_else(|| panic!("cfg.toml: [{table}].{key} debe ser un número"));
    println!("cargo:rustc-env={env_name}={value}");
}

/// Igual que [`emit_int`], pero con valor por defecto si la clave no está.
///
/// Las claves nuevas entran así a propósito: un `cfg.toml` que ya funcionaba
/// —y que no está versionado, así que nadie puede actualizarlo por ti— tiene
/// que seguir compilando después de una actualización del firmware.
fn emit_int_default(doc: &toml::Value, table: &str, key: &str, env_name: &str, por_defecto: i64) {
    let value = doc
        .get(table)
        .and_then(|t| t.get(key))
        .map(|v| {
            v.as_integer()
                .unwrap_or_else(|| panic!("cfg.toml: [{table}].{key} debe ser un número"))
        })
        .unwrap_or(por_defecto);
    println!("cargo:rustc-env={env_name}={value}");
}

/// Un booleano se emite como 0 o 1: `lib.rs` no puede parsear texto en `const`.
fn emit_bool_default(doc: &toml::Value, table: &str, key: &str, env_name: &str, por_defecto: bool) {
    let value = doc
        .get(table)
        .and_then(|t| t.get(key))
        .map(|v| {
            v.as_bool()
                .unwrap_or_else(|| panic!("cfg.toml: [{table}].{key} debe ser true o false"))
        })
        .unwrap_or(por_defecto);
    println!("cargo:rustc-env={env_name}={}", if value { 1 } else { 0 });
}

/// Resuelve la ruta del certificado del servidor y la expone al crate.
///
/// El PEM no se pasa como variable de entorno (es multilínea y se llevaría mal):
/// se emite su **ruta**, y `lib.rs` lo empotra con `include_str!`. Así el
/// certificado viaja dentro del binario y el dispositivo no depende de nada
/// externo para validar al servidor.
fn emit_cert(doc: &toml::Value, cfg_dir: &Path) {
    let rel = get(doc, "server", "tls_cert")
        .as_str()
        .expect("cfg.toml: [server].tls_cert debe ser una ruta entre comillas");

    let path = cfg_dir.join(rel);
    if !path.is_file() {
        panic!(
            "\n\nNo se encuentra el certificado del servidor en {}.\n\
             Cópialo del asistente con:\n\n    \
             ./firmware/scripts/sync-cert.sh\n\n\
             El firmware fija ese certificado (pinning) y no confía en ninguna CA.\n",
            path.display()
        );
    }

    // Comprobación barata que ahorra un fallo de TLS incomprensible más tarde.
    let pem = std::fs::read_to_string(&path).expect("no se puede leer el certificado");
    if !pem.contains("BEGIN CERTIFICATE") {
        panic!("{} no parece un certificado PEM", path.display());
    }

    println!("cargo:rerun-if-changed={}", path.display());
    println!("cargo:rustc-env=LUKA_TLS_CERT_PATH={}", path.display());
}

fn main() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let Some(path) = find_cfg(&manifest) else {
        panic!(
            "\n\nNo se encuentra cfg.toml.\n\
             Copia la plantilla y rellénala:\n\n    \
             cp firmware/cfg.toml.example firmware/cfg.toml\n\n\
             (cfg.toml está en .gitignore: lleva la clave de la WiFi y el API_TOKEN.)\n"
        );
    };

    println!("cargo:rerun-if-changed={}", path.display());

    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("No se puede leer {}: {e}", path.display()));
    let doc: toml::Value = raw
        .parse()
        .unwrap_or_else(|e| panic!("{} no es TOML válido: {e}", path.display()));

    emit_str(&doc, "wifi", "ssid", "LUKA_WIFI_SSID");
    emit_str(&doc, "wifi", "password", "LUKA_WIFI_PASSWORD");
    emit_str(&doc, "server", "host", "LUKA_SERVER_HOST");
    emit_int(&doc, "server", "port", "LUKA_SERVER_PORT");
    emit_str(&doc, "server", "api_token", "LUKA_API_TOKEN");
    emit_cert(&doc, path.parent().unwrap());
    emit_str(&doc, "device", "name", "LUKA_DEVICE_NAME");
    emit_int(&doc, "device", "led_brightness", "LUKA_LED_BRIGHTNESS");
    emit_int_default(&doc, "device", "wake_threshold", "LUKA_WAKE_THRESHOLD", 200);
    emit_bool_default(&doc, "device", "wake_calibration", "LUKA_WAKE_CALIBRATION", false);
}
