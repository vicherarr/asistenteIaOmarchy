//! Wake word "Luka" en la placa.
//!
//! El crate tiene dos mitades muy distintas:
//!
//! - [`decision`] es aritmética pura —media móvil, umbral, refractario— y se
//!   prueba con `cargo test` en el PC, sin placa.
//! - [`Detector`] es el único `unsafe` del firmware: envuelve el componente en
//!   C que junta el frontend de espectrograma con TFLite Micro.
//!
//! Esa separación es deliberada. La parte que más se toca al afinar el
//! detector (cuándo damos por dicha la palabra) es justo la que no necesita
//! hardware para probarse; la parte con `unsafe` es fija y pequeña.
//!
//! # Compilar para el PC
//!
//! Fuera del ESP32 el `Detector` no existe: no hay componente en C con el que
//! enlazar. El crate compila igual para que los tests de [`decision`] corran
//! en el host.

#![cfg_attr(not(test), no_std)]
#![deny(unsafe_code)]

pub mod decision;

pub use decision::Politica;

/// El modelo entrenado, empotrado en el binario.
///
/// Ocupa ~40 kB, así que va en flash con el firmware y no hay nada que
/// aprovisionar por separado: reentrenar la palabra es recompilar y grabar.
///
/// El `align(16)` no es adorno: TFLite lee el flatbuffer **en sitio**, sin
/// copiarlo, y necesita que empiece alineado. `include_bytes!` no garantiza
/// alineación ninguna, y desalineado el modelo carga con datos basura.
#[cfg(target_os = "espidf")]
#[repr(C, align(16))]
struct ModeloAlineado<const N: usize>([u8; N]);

#[cfg(target_os = "espidf")]
static MODELO: ModeloAlineado<{ include_bytes!("../modelo/luka.tflite").len() }> =
    ModeloAlineado(*include_bytes!("../modelo/luka.tflite"));

/// Arena de tensores por defecto.
///
/// Los modelos de microWakeWord piden entre 30 y 50 kB. Se pide de más a
/// propósito: quedarse corto no se ve al compilar, sino como un fallo al
/// arrancar, y 64 kB en un chip con 8 MB de PSRAM no es un problema.
#[cfg(target_os = "espidf")]
pub const ARENA_POR_DEFECTO: usize = 64 * 1024;

#[cfg(target_os = "espidf")]
mod ffi {
    use core::ffi::c_void;

    extern "C" {
        pub fn luka_ww_create(model: *const u8, arena_bytes: usize) -> *mut c_void;
        pub fn luka_ww_destroy(ww: *mut c_void);
        pub fn luka_ww_process(
            ww: *mut c_void,
            pcm: *const i16,
            samples: usize,
            probabilities: *mut u8,
            max_probabilities: usize,
        ) -> i32;
        pub fn luka_ww_reset(ww: *mut c_void);
        pub fn luka_ww_arena_used(ww: *const c_void) -> usize;
    }
}

#[cfg(target_os = "espidf")]
pub use detector::{Detector, MAX_PROBABILIDADES};

#[cfg(target_os = "espidf")]
#[allow(unsafe_code)]
mod detector {
    use super::ffi;
    use core::ffi::c_void;

    /// Tope de probabilidades por llamada a [`Detector::procesar`].
    ///
    /// Una trama de 20 ms produce como mucho una probabilidad; 8 deja margen
    /// de sobra si alguna vez se le pasan varias tramas juntas.
    pub const MAX_PROBABILIDADES: usize = 8;

    /// El detector. Dueño de la memoria del lado C; la libera al caer.
    pub struct Detector {
        handle: *mut c_void,
    }

    // El handle solo lo toca el hilo que lo posee (`detect`), pero tiene que
    // poder cruzar la frontera de hilo al crearse. Nada dentro es compartido.
    unsafe impl Send for Detector {}

    impl Detector {
        /// Carga el modelo empotrado. `None` si el modelo no es válido o no
        /// hay memoria para el arena.
        pub fn new(arena_bytes: usize) -> Option<Self> {
            let handle = unsafe { ffi::luka_ww_create(super::MODELO.0.as_ptr(), arena_bytes) };
            if handle.is_null() {
                None
            } else {
                Some(Self { handle })
            }
        }

        /// Alimenta PCM mono de 16 kHz y devuelve las probabilidades nuevas.
        ///
        /// Casi siempre devuelve un trozo vacío: el modelo infiere cada dos o
        /// tres tramas, no con cada una.
        pub fn procesar<'a>(&mut self, pcm: &[i16], salida: &'a mut [u8; MAX_PROBABILIDADES]) -> &'a [u8] {
            let n = unsafe {
                ffi::luka_ww_process(
                    self.handle,
                    pcm.as_ptr(),
                    pcm.len(),
                    salida.as_mut_ptr(),
                    MAX_PROBABILIDADES,
                )
            };
            if n <= 0 {
                &[]
            } else {
                &salida[..n as usize]
            }
        }

        /// Olvida el estado interno. Obligatorio al volver de hablar: si no,
        /// el detector arrastra el eco de la propia voz de Luka.
        pub fn reset(&mut self) {
            unsafe { ffi::luka_ww_reset(self.handle) }
        }

        /// Bytes de arena realmente usados, para el log de arranque.
        pub fn arena_usada(&self) -> usize {
            unsafe { ffi::luka_ww_arena_used(self.handle) }
        }
    }

    impl Drop for Detector {
        fn drop(&mut self) {
            unsafe { ffi::luka_ww_destroy(self.handle) }
        }
    }
}
