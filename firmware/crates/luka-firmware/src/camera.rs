//! El hilo de la cámara: una foto cuando el servidor la pide.
//!
//! Vive aparte por la misma razón que `detect`: capturar y comprimir cuesta unos
//! 100 ms seguidos, y eso en el bucle del supervisor se comería el sondeo de
//! botones y el watchdog, y en el callback del WebSocket tumbaría el enlace —que
//! es exactamente cómo se perdió media tarde con el barge-in.
//!
//! # Por qué la compresión no es opcional
//!
//! El GC0308 **no lleva compresor JPEG** (a diferencia del OV2640): solo saca
//! RGB565. Un fotograma en crudo son 150 kB, y comprimido unos 6. Por un enlace
//! que mueve ~32 kB/s, eso es la diferencia entre cinco segundos y dos décimas.

use crate::net::NetCommand;
use esp_idf_hal::cpu::Core;
use esp_idf_hal::task::thread::ThreadSpawnConfiguration;
use std::sync::mpsc::{Receiver, SyncSender};

/// Calidad JPEG (1-63 en esta librería; **menor es mejor**).
///
/// 12 da unos 6 kB a 320x240, que es tamaño de sobra para que un modelo
/// multimodal describa una escena. Subir la calidad engorda el JPEG y acerca el
/// tope de 64 kB de una trama, que es lo que permite mandarla sin trocear.
const CALIDAD: i32 = 12;

/// Espejo de `luka_cam_pins_t`. El pinout sale de `luka_board`, que es la fuente
/// de verdad de la placa, así que el lado C no repite ni un número.
#[repr(C)]
struct Pins {
    xclk: i32,
    pclk: i32,
    vsync: i32,
    href: i32,
    data: [i32; 8],
    sccb_port: i32,
    xclk_hz: i32,
}

extern "C" {
    fn luka_cam_init(pins: *const Pins) -> i32;
    fn luka_cam_capture_jpeg(out: *mut *mut u8, out_len: *mut usize, quality: i32) -> i32;
    fn luka_cam_release(buf: *mut u8);
}

fn pines() -> Pins {
    use luka_board::camera as c;
    Pins {
        xclk: c::XCLK as i32,
        pclk: c::PCLK as i32,
        vsync: c::VSYNC as i32,
        href: c::HREF as i32,
        data: core::array::from_fn(|i| c::DATA[i] as i32),
        // El bus que el firmware ya abrió (I2C0), no pines sueltos.
        sccb_port: 0,
        xclk_hz: c::XCLK_HZ as i32,
    }
}

/// Arranca el sensor. **Hay que llamarlo pronto, antes de levantar la red.**
///
/// El driver pide ~30 kB **contiguos de RAM interna con capacidad DMA**, y eso
/// no se puede servir desde PSRAM. Si se hace tarde, el WiFi, el TLS y el arena
/// de la wake word ya se han llevado la interna y queda fragmentada: el fallo es
///
/// ```text
/// cam_dma_config: DMA buffer 30720 Byte malloc failed,
/// the current largest free block: 11264 Byte
/// ```
///
/// que además llega como un `ESP_FAIL` pelado desde `esp_camera_init` y no
/// menciona la palabra memoria por ningún lado. En el spike no se notaba porque
/// allí no había ni red ni modelo compitiendo por la interna.
#[allow(unsafe_code)]
pub fn init() -> bool {
    let err = unsafe { luka_cam_init(&pines()) };
    if err != 0 {
        log::error!("cámara: init falló ({err}); no habrá fotos");
        return false;
    }
    log::info!(
        "cámara lista ({}x{})",
        luka_board::camera::WIDTH,
        luka_board::camera::HEIGHT
    );
    true
}

pub struct Camera {
    peticiones: Receiver<()>,
    net_tx: SyncSender<NetCommand>,
    viva: bool,
}

impl Camera {
    pub fn new(peticiones: Receiver<()>, net_tx: SyncSender<NetCommand>, viva: bool) -> Self {
        Self { peticiones, net_tx, viva }
    }

    /// Prioridad **por debajo de la red**, igual que el detector.
    ///
    /// Comprimir un JPEG son ~70 ms de CPU seguidos. Con la prioridad por defecto
    /// de pthreads eso empata con la tarea del cliente WebSocket, y ahí ya se
    /// aprendió lo que pasa: `Could not lock ws-client` y el enlace al suelo.
    /// Una foto que tarda un pelo más no la nota nadie; perder el enlace, sí.
    const PRIORIDAD: u8 = 4;
    const PILA: usize = 8192;

    pub fn spawn(mut self) -> anyhow::Result<()> {
        ThreadSpawnConfiguration {
            name: Some(c"camera"),
            stack_size: Self::PILA,
            priority: Self::PRIORIDAD,
            pin_to_core: Some(Core::Core1),
            ..Default::default()
        }
        .set()?;

        let spawned = std::thread::Builder::new()
            .name("camera".into())
            .stack_size(Self::PILA)
            .spawn(move || self.run_loop());

        // Restaurar siempre: la configuración es global al proceso y la heredaría
        // el siguiente hilo que se cree.
        ThreadSpawnConfiguration::default().set()?;
        spawned?;
        Ok(())
    }

    #[allow(unsafe_code)]
    fn run_loop(&mut self) {
        // Si el sensor no arrancó, el hilo NO se muere: sigue consumiendo
        // peticiones y contestando que no, en vez de dejar al servidor esperando.
        let viva = self.viva;

        crate::watchdog::subscribe("camera");

        loop {
            crate::watchdog::feed();
            // Con plazo y no bloqueante: hay que seguir dando señales de vida al
            // watchdog aunque nadie pida fotos en horas.
            let Ok(()) = self.peticiones.recv_timeout(std::time::Duration::from_secs(1)) else {
                continue;
            };

            if !viva {
                log::warn!("cámara: se pidió una foto pero el sensor no arrancó");
                continue;
            }

            let inicio = std::time::Instant::now();
            let mut buf: *mut u8 = core::ptr::null_mut();
            let mut len: usize = 0;
            let err = unsafe { luka_cam_capture_jpeg(&mut buf, &mut len, CALIDAD) };
            if err != 0 || buf.is_null() {
                log::error!("cámara: no se pudo capturar (error {err})");
                continue;
            }

            // Se copia a un Vec para soltar cuanto antes la memoria del driver, y
            // porque la red necesita ser dueña de lo que envía.
            let jpeg = unsafe { core::slice::from_raw_parts(buf, len) }.to_vec();
            unsafe { luka_cam_release(buf) };

            if jpeg.len() > luka_proto::MAX_FRAME_BYTES - 1 {
                // Antes que mandar media imagen, no mandar ninguna: el otro
                // extremo también comprueba el tope y la rechazaría entera.
                log::error!(
                    "cámara: el JPEG no cabe en una trama ({} B). Toca trocear o bajar calidad.",
                    jpeg.len()
                );
                continue;
            }

            log::info!("cámara: {} B en {} ms", jpeg.len(), inicio.elapsed().as_millis());
            if self.net_tx.try_send(NetCommand::SendImage(jpeg)).is_err() {
                log::warn!("cámara: imagen descartada, la red no da abasto");
            }
        }
    }
}
