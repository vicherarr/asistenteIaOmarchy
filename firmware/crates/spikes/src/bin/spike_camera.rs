//! Spike — ¿está la cámara colgada del bus I²C, y cuál es?
//!
//! **Qué responde:** ¿responde el sensor por SCCB, en qué dirección, y es
//! realmente un GC0308?
//!
//! Es la primera prueba de la cámara porque es la única que **no necesita saber
//! el cableado**. El SCCB de estos sensores es I²C corriente, así que si el
//! control está colgado del bus que ya existe (GPIO11/10), aparece aquí sin más.
//! Y hasta que el control no responda, no tiene ningún sentido pelearse con los
//! ocho bits del bus de datos.
//!
//! Se apoya en que las cuatro direcciones de la placa ya están confirmadas
//! (`luka_board::i2c::EXPECTED`): **cualquier dirección nueva es la cámara**.
//!
//! ```bash
//! cargo run -p spikes --bin spike_camera
//! ```
//!
//! # Si no aparece nada nuevo
//!
//! No significa necesariamente que esté mal conectada. Muchos sensores
//! **necesitan reloj en XCLK antes de contestar por SCCB**: sin él, el bloque
//! digital ni siquiera arranca. Como XCLK es justo uno de los pines que no
//! conocemos, ese caso hay que resolverlo con el modelo del módulo en la mano,
//! no a ciegas.

use anyhow::{Context, Result};
use esp_idf_hal::delay::TickType;
use esp_idf_hal::i2c::{I2cConfig, I2cDriver};
use esp_idf_hal::ledc::{config::TimerConfig, LedcDriver, LedcTimerDriver, Resolution};
use esp_idf_hal::peripherals::Peripherals;
use esp_idf_hal::units::FromValueType;

const _: () = assert!(luka_board::i2c::SDA == 11);
const _: () = assert!(luka_board::i2c::SCL == 10);

const ADDR_FIRST: u8 = 0x03;
const ADDR_LAST: u8 = 0x77;

/// Plazo de cada transacción I²C.
///
/// **Nunca `BLOCK` en una herramienta de descubrimiento.** `BLOCK` es espera
/// infinita, y una dirección que no contesta bien —o un módulo nuevo que deja el
/// bus tomado— cuelga el spike para siempre sin imprimir nada. Pasó a la primera:
/// el programa se quedó mudo tras el test de PSRAM y parecía que ni arrancaba.
///
/// Con plazo, una dirección muerta cuesta 20 ms y el barrido siempre termina.
fn plazo() -> u32 {
    TickType::from(std::time::Duration::from_millis(20)).0
}

/// Sensores que comparten dirección SCCB, y cómo distinguirlos.
///
/// La dirección sola no basta: el GC0308 y el OV7670 viven los dos en `0x21`.
/// Lo que los separa es el registro de identidad, que además confirma que hay un
/// sensor de verdad y no otro chip cualquiera que casualmente hace ACK ahí.
struct Sospechoso {
    addr: u8,
    /// Registro de identidad.
    reg: u8,
    /// Lo que debe devolver.
    id: u8,
    nombre: &'static str,
}

const SOSPECHOSOS: &[Sospechoso] = &[
    Sospechoso { addr: 0x21, reg: 0x00, id: 0x9b, nombre: "GC0308" },
    Sospechoso { addr: 0x21, reg: 0x0a, id: 0x76, nombre: "OV7670" },
    Sospechoso { addr: 0x30, reg: 0x0a, id: 0x26, nombre: "OV2640" },
    Sospechoso { addr: 0x3c, reg: 0x0a, id: 0x26, nombre: "OV2640 (dirección alternativa)" },
];

/// Lee un registro de 8 bits: se escribe la dirección del registro y se lee la
/// respuesta, que es el patrón de SCCB y de casi todo el I²C sencillo.
fn leer_reg(i2c: &mut I2cDriver, addr: u8, reg: u8) -> Option<u8> {
    let mut buf = [0u8; 1];
    i2c.write(addr, &[reg], plazo()).ok()?;
    i2c.read(addr, &mut buf, plazo()).ok()?;
    Some(buf[0])
}

// --- TCA9555, puerto 0 ---
// Registros del expansor. El puerto 0 es el par "bajo" de cada pareja.
const REG_OUTPUT_0: u8 = 0x02;
const REG_CONFIG_0: u8 = 0x06;

fn leer_expansor(i2c: &mut I2cDriver, reg: u8) -> Result<u8> {
    let mut buf = [0u8; 1];
    i2c.write(luka_board::i2c::ADDR_TCA9555, &[reg], plazo())?;
    i2c.read(luka_board::i2c::ADDR_TCA9555, &mut buf, plazo())?;
    Ok(buf[0])
}

fn escribir_expansor(i2c: &mut I2cDriver, reg: u8, valor: u8) -> Result<()> {
    i2c.write(luka_board::i2c::ADDR_TCA9555, &[reg, valor], plazo())?;
    Ok(())
}

/// Saca al sensor de reposo y de reset.
///
/// **Leer-modificar-escribir siempre.** En este mismo puerto 0 vive el CS de la
/// tarjeta SD (P3): escribir el registro entero lo pisaría.
fn despertar_sensor(i2c: &mut I2cDriver) -> Result<()> {
    use luka_board::expander::{CAM_POWER_DOWN, CAM_RESET, CAM_SELECT};

    let pd = 1u8 << CAM_POWER_DOWN;
    let rst = 1u8 << CAM_RESET;
    let sel = 1u8 << CAM_SELECT;

    // Primero los VALORES y luego la dirección, igual que con el amplificador:
    // al revés, el pin pasaría un instante como salida con valor indeterminado.
    //
    // Encendida (power_down bajo), SELECCIONADA (select ALTO) y en reset.
    //
    // Lo del select a alto costó un barrido de polaridades: con esa línea a bajo
    // el sensor no contesta siquiera por SCCB y parece que no hay cámara. El
    // port de terceros del que sale el pinout no lo documenta.
    let out = leer_expansor(i2c, REG_OUTPUT_0)?;
    escribir_expansor(i2c, REG_OUTPUT_0, (out & !pd & !rst) | sel)?;

    // 0 = salida. Solo estas tres; el resto del puerto se queda como estaba.
    let cfg = leer_expansor(i2c, REG_CONFIG_0)?;
    escribir_expansor(i2c, REG_CONFIG_0, cfg & !pd & !rst & !sel)?;
    log::info!("expansor: power_down=0, select=1, reset=0 (en reset)");

    // El reset necesita anchura: unos milisegundos con el reloj ya corriendo.
    std::thread::sleep(std::time::Duration::from_millis(10));

    let out = leer_expansor(i2c, REG_OUTPUT_0)?;
    escribir_expansor(i2c, REG_OUTPUT_0, out | rst)?;
    log::info!("expansor: reset liberado");

    // El sensor tarda en tener el SCCB listo tras el reset.
    std::thread::sleep(std::time::Duration::from_millis(50));
    Ok(())
}

/// Fija `power_down` y `camera_select` a valores concretos y da un pulso de
/// reset, para probar una combinación de polaridad.
fn combinacion(i2c: &mut I2cDriver, pd_val: u8, sel_val: u8) -> Result<()> {
    use luka_board::expander::{CAM_POWER_DOWN, CAM_RESET, CAM_SELECT};
    let pd = 1u8 << CAM_POWER_DOWN;
    let rst = 1u8 << CAM_RESET;
    let sel = 1u8 << CAM_SELECT;

    let mut out = leer_expansor(i2c, REG_OUTPUT_0)?;
    out = if pd_val == 1 { out | pd } else { out & !pd };
    out = if sel_val == 1 { out | sel } else { out & !sel };
    escribir_expansor(i2c, REG_OUTPUT_0, out & !rst)?; // en reset
    std::thread::sleep(std::time::Duration::from_millis(10));
    escribir_expansor(i2c, REG_OUTPUT_0, out | rst)?; // fuera de reset
    std::thread::sleep(std::time::Duration::from_millis(80));
    Ok(())
}

/// Pinout que se le pasa al shim en C. Espejo de `luka_cam_pins_t`.
#[repr(C)]
struct LukaCamPins {
    xclk: i32,
    pclk: i32,
    vsync: i32,
    href: i32,
    data: [i32; 8],
    sda: i32,
    scl: i32,
    xclk_hz: i32,
}

extern "C" {
    fn luka_cam_init(pins: *const LukaCamPins) -> i32;
    fn luka_cam_capture_jpeg(out: *mut *mut u8, out_len: *mut usize, quality: i32) -> i32;
    fn luka_cam_release(buf: *mut u8);
    fn luka_cam_last_size(width: *mut i32, height: *mut i32);
}

/// Intenta capturar un fotograma y comprimirlo.
///
/// Esto es lo que confirma **el bus de datos**: que el sensor conteste por SCCB
/// no dice nada de los ocho bits de D0-D7 ni de los sincronismos. Si el ancho y
/// el alto salen correctos y el JPEG tiene un tamaño razonable, el interfaz DVP
/// entero queda verificado.
#[allow(unsafe_code)]
fn capturar() {
    let pins = LukaCamPins {
        xclk: luka_board::camera::XCLK as i32,
        pclk: luka_board::camera::PCLK as i32,
        vsync: luka_board::camera::VSYNC as i32,
        href: luka_board::camera::HREF as i32,
        data: core::array::from_fn(|i| luka_board::camera::DATA[i] as i32),
        sda: luka_board::i2c::SDA as i32,
        scl: luka_board::i2c::SCL as i32,
        xclk_hz: luka_board::camera::XCLK_HZ as i32,
    };

    let err = unsafe { luka_cam_init(&pins) };
    if err != 0 {
        log::error!("luka_cam_init falló con {err}. El sensor habla por SCCB pero");
        log::error!("el interfaz DVP no arranca: revisa D0-D7, PCLK, VSYNC o HREF.");
        return;
    }

    let mut buf: *mut u8 = core::ptr::null_mut();
    let mut len: usize = 0;
    let err = unsafe { luka_cam_capture_jpeg(&mut buf, &mut len, 12) };
    if err != 0 || buf.is_null() {
        log::error!("no se pudo capturar/comprimir (error {err})");
        return;
    }

    let (mut w, mut h) = (0i32, 0i32);
    unsafe { luka_cam_last_size(&mut w, &mut h) };

    // Los dos primeros bytes de un JPEG son SIEMPRE 0xFF 0xD8. Comprobarlo
    // distingue "hay una imagen" de "hay 50 kB de basura del tamaño correcto",
    // que desde el log se ven idénticos.
    let cabecera = unsafe { core::slice::from_raw_parts(buf, len.min(2)) };
    let parece_jpeg = cabecera.len() == 2 && cabecera[0] == 0xFF && cabecera[1] == 0xD8;

    log::info!("--- Fotograma capturado ---");
    log::info!("  {w}x{h}, JPEG de {len} B");
    if parece_jpeg {
        log::info!("  cabecera JPEG correcta (FF D8)");
        log::info!("");
        log::info!("RESULTADO: el interfaz DVP funciona. Pinout CONFIRMADO al completo.");
    } else {
        log::error!("  la cabecera NO es la de un JPEG: {cabecera:02x?}");
        log::error!("RESULTADO: se captura algo, pero no es una imagen válida.");
    }

    unsafe { luka_cam_release(buf) };
}

fn main() -> Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take().context("no se pudieron tomar los periféricos")?;

    log::info!("=== Spike cámara: ¿responde el sensor por SCCB? ===");

    let config = I2cConfig::new().baudrate(luka_board::i2c::FREQ_HZ.Hz());
    let mut i2c = I2cDriver::new(
        peripherals.i2c0,
        peripherals.pins.gpio11, // SDA
        peripherals.pins.gpio10, // SCL
        &config,
    )
    .context("no se pudo abrir el bus I2C")?;

    // --- 1. Reloj en XCLK ---
    //
    // Va ANTES de tocar el sensor: sin reloj su bloque digital no arranca y no
    // contesta por SCCB ni aunque lo saques de reset. Fue exactamente lo que
    // hizo que el primer barrido no encontrara nada.
    //
    // Se genera con LEDC porque es el periférico que sabe sacar una onda
    // cuadrada limpia a esta frecuencia sin ocupar la CPU.
    let timer = LedcTimerDriver::new(
        peripherals.ledc.timer0,
        &TimerConfig::new()
            .frequency(luka_board::camera::XCLK_HZ.Hz().into())
            // 1 bit basta para un reloj: solo hay que alternar. Y cuanta menos
            // resolución, más frecuencia admite el temporizador.
            .resolution(Resolution::Bits1),
    )
    .context("no se pudo configurar el temporizador de XCLK")?;

    let mut xclk = LedcDriver::new(peripherals.ledc.channel0, &timer, peripherals.pins.gpio43)
        .context("no se pudo abrir XCLK en GPIO43")?;
    // Duty al 50 %: con 1 bit de resolución, eso es 1 de 2.
    xclk.set_duty(1).context("no se pudo arrancar XCLK")?;
    log::info!(
        "XCLK: {} MHz en GPIO{}",
        luka_board::camera::XCLK_HZ / 1_000_000,
        luka_board::camera::XCLK
    );

    // --- 2. Sacarla de reposo por el expansor ---
    if let Err(e) = despertar_sensor(&mut i2c) {
        log::error!("no se pudo tocar el expansor: {e:#}");
    }

    // --- 2b. Si no contesta, probar las otras polaridades ---
    //
    // `power_down` y `camera_select` se han supuesto activos a nivel alto y
    // cámara 0, pero eso viene de un port de terceros. Son cuatro combinaciones
    // y cada una cuesta 100 ms: sale mucho más barato probarlas que discutirlas.
    if leer_reg(&mut i2c, luka_board::camera::ADDR_SENSOR, 0x00).is_none() {
        log::info!("--- No contesta. Probando polaridades ---");
        for pd in [0u8, 1] {
            for sel in [0u8, 1] {
                if let Err(e) = combinacion(&mut i2c, pd, sel) {
                    log::warn!("  pd={pd} sel={sel}: fallo en el expansor: {e:#}");
                    continue;
                }
                match leer_reg(&mut i2c, luka_board::camera::ADDR_SENSOR, 0x00) {
                    Some(v) => log::info!("  pd={pd} sel={sel}  ->  reg0x00 = {v:#04x}  *** CONTESTA ***"),
                    None => log::info!("  pd={pd} sel={sel}  ->  sin respuesta"),
                }
            }
        }
        // Se deja en la combinación confirmada para el barrido de abajo.
        let _ = combinacion(&mut i2c, 0, 1);
    }

    // --- 3. Quién hay en el bus ---
    let mut nuevas: Vec<u8> = Vec::new();
    log::info!("--- Barrido del bus ---");
    for addr in ADDR_FIRST..=ADDR_LAST {
        // Traza de avance: si algo se atasca, el log dice exactamente en qué
        // dirección, en vez de dejar un programa mudo.
        if addr % 0x10 == 0 {
            log::info!("  ... sondeando {addr:#04x}");
        }
        let mut buf = [0u8; 1];
        if i2c.read(addr, &mut buf, plazo()).is_ok() {
            match luka_board::i2c::EXPECTED.iter().find(|(a, _)| *a == addr) {
                Some((_, nombre)) => log::info!("  {addr:#04x}  {nombre}  (ya conocido)"),
                None => {
                    log::info!("  {addr:#04x}  *** NUEVO ***");
                    nuevas.push(addr);
                }
            }
        }
    }

    // --- Identificar lo nuevo ---
    if nuevas.is_empty() {
        log::warn!("");
        log::warn!("RESULTADO: no hay ninguna dirección nueva en el bus.");
        log::warn!("");
        log::warn!("El SCCB de la cámara NO está colgado de este bus, o el sensor");
        log::warn!("no arranca. Las causas, por probabilidad:");
        log::warn!("");
        log::warn!("  1. XCLK no es GPIO43, o el reloj no llega al sensor. El");
        log::warn!("     pinout viene de un port de terceros, no de Waveshare.");
        log::warn!("  2. Las líneas de control no son P5/P6/P7 del expansor, o su");
        log::warn!("     polaridad es la contraria a la supuesta.");
        log::warn!("  3. Alimentación: estos sensores piden 2,8 V para la parte");
        log::warn!("     analógica y muchos módulos lo generan a bordo.");
        log::warn!("  4. El cable plano está flojo o del revés.");
        log::warn!("");
        log::warn!("Ya NO puede ser 'le falta reloj': este spike lo genera.");
    } else {
        for addr in &nuevas {
            log::info!("");
            log::info!("--- Identificando {addr:#04x} ---");
            let mut identificado = false;
            for s in SOSPECHOSOS.iter().filter(|s| s.addr == *addr) {
                match leer_reg(&mut i2c, s.addr, s.reg) {
                    Some(v) if v == s.id => {
                        log::info!("  reg {:#04x} = {v:#04x}  -> ES UN {}", s.reg, s.nombre);
                        identificado = true;
                    }
                    Some(v) => log::info!("  reg {:#04x} = {v:#04x}  (un {} daría {:#04x})", s.reg, s.nombre, s.id),
                    None => log::warn!("  reg {:#04x}: no se pudo leer", s.reg),
                }
            }
            if !identificado {
                // Volcado de los primeros registros: con esto se identifica a mano
                // un sensor que no esté en la lista.
                log::info!("  No cuadra con ninguno conocido. Primeros registros:");
                for reg in [0x00u8, 0x0a, 0x0b, 0xf0] {
                    match leer_reg(&mut i2c, *addr, reg) {
                        Some(v) => log::info!("    reg {reg:#04x} = {v:#04x}"),
                        None => log::info!("    reg {reg:#04x} = <sin respuesta>"),
                    }
                }
            }
        }
        log::info!("");
        log::info!("Sensor OK. Ahora el bus de datos: capturando un fotograma.");

        // **Soltar XCLK antes de seguir.** Este spike lo genera con LEDC canal 0
        // para poder hablar por SCCB, pero el driver de cámara quiere generarlo
        // él (con su propio temporizador). Dos periféricos sobre el mismo GPIO no
        // acaban bien, así que aquí se cede el pin.
        drop(xclk);
        drop(timer);

        capturar();
    }

    // Se queda vivo: si `main` retorna, el ESP-IDF reinicia y no da tiempo a leer.
    loop {
        std::thread::sleep(std::time::Duration::from_secs(60));
    }
}
