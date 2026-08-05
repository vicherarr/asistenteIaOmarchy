//! Watchdog de tareas: que un cuelgue reinicie en vez de congelar.
//!
//! El TWDT ya venía activado en `sdkconfig.defaults` con 10 s, pero **ningún
//! hilo se suscribía**, así que no vigilaba absolutamente nada. Un cuelgue como
//! los que aparecieron en la Fase 1 dejaba la placa muda y encendida, que es el
//! peor estado posible: parece viva y no hace nada.
//!
//! # Qué se vigila y qué no
//!
//! Solo se suscriben los hilos con un **ritmo propio y acotado**:
//!
//! - el **supervisor**, que da una vuelta cada 20 ms y es el latido del aparato;
//! - el hilo de **audio**, que se bloquea en el I²S como mucho una trama (20 ms),
//!   así que si el periférico se atasca se nota enseguida.
//!
//! El hilo de **red no se suscribe a propósito**: asociarse a la WiFi y esperar
//! IP tarda lo que tarde el router (se han medido 5,7 s, y un día malo puede ser
//! más). Vigilarlo convertiría un DHCP lento en un reinicio, que es peor que el
//! problema. Su salud ya la cubren los plazos de la máquina de estados
//! (`SERVER_CONNECT_TIMEOUT_MS` y compañía), que sacan al dispositivo de
//! cualquier espera eterna sin necesidad de reiniciarlo entero.

use esp_idf_svc::hal::sys::{esp, esp_task_wdt_add, esp_task_wdt_reset};

/// Suscribe el hilo actual al watchdog.
///
/// Si falla, se sigue adelante: quedarse sin vigilancia es malo, pero no arrancar
/// por eso lo es más. Queda en el log para que se note.
pub fn subscribe(nombre: &str) {
    // `null` significa "la tarea actual" en la API del ESP-IDF.
    match esp!(unsafe { esp_task_wdt_add(core::ptr::null_mut()) }) {
        Ok(()) => log::info!("watchdog: vigilando el hilo «{nombre}»"),
        Err(e) => log::warn!("watchdog: no se pudo vigilar «{nombre}» ({e:?}); sigue sin vigilancia"),
    }
}

/// Rearma el watchdog desde el hilo actual. Va en cada vuelta del bucle.
pub fn feed() {
    // Se ignora el error: solo puede fallar si el hilo no está suscrito, y en ese
    // caso ya se avisó al arrancar. Quejarse aquí llenaría el log 50 veces por
    // segundo sin aportar nada nuevo.
    unsafe {
        esp_task_wdt_reset();
    }
}
