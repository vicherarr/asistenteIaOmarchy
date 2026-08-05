//! El anillo de 7 LEDs como interfaz de estado.
//!
//! No es decoración: en v1 no hay pantalla, así que el anillo es **el único canal
//! de diagnóstico** que no exige tener la placa enchufada al PC leyendo el log.
//! Se diseña como tal.
//!
//! Principio: **el movimiento distingue, el color confirma**. Cada estado tiene su
//! animación propia (giro, respiración, vúmetro, parpadeo), no solo un color, para
//! que se lea de un vistazo y de reojo — a dos metros y con el difusor puesto,
//! distinguir cian de verde no es tan obvio como parece en una tabla.
//!
//! Todo es la función pura [`frame`]: estado + tiempo + nivel → 7 colores. Sin
//! periféricos y sin reloj, así que las animaciones se prueban en el host en vez
//! de a ojo con la placa delante.

#![no_std]
#![deny(unsafe_code)]

use luka_board::leds::COUNT;
use luka_state::{Fault, State};

/// Un color, en RGB lógico. El cruce rojo/verde de esta placa se corrige al
/// escribir al bus, en `luka_board::leds::to_wire`, no aquí.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Rgb {
    pub r: u8,
    pub g: u8,
    pub b: u8,
}

impl Rgb {
    pub const BLACK: Self = Self::new(0, 0, 0);
    pub const WHITE: Self = Self::new(255, 255, 255);
    pub const RED: Self = Self::new(255, 0, 0);
    pub const GREEN: Self = Self::new(0, 255, 0);
    pub const BLUE: Self = Self::new(40, 90, 255);
    pub const CYAN: Self = Self::new(0, 210, 255);
    pub const VIOLET: Self = Self::new(150, 60, 255);
    pub const AMBER: Self = Self::new(255, 140, 0);

    pub const fn new(r: u8, g: u8, b: u8) -> Self {
        Self { r, g, b }
    }

    /// Escala el color a una fracción `k/255` de su intensidad.
    pub const fn scaled(self, k: u8) -> Self {
        Self {
            r: ((self.r as u16 * k as u16) / 255) as u8,
            g: ((self.g as u16 * k as u16) / 255) as u8,
            b: ((self.b as u16 * k as u16) / 255) as u8,
        }
    }

    pub const fn is_off(self) -> bool {
        self.r == 0 && self.g == 0 && self.b == 0
    }
}

/// Un fotograma del anillo.
pub type Ring = [Rgb; COUNT];

pub const OFF: Ring = [Rgb::BLACK; COUNT];

// ------------------------------------------------------------------- gamma

/// Corrección gamma, obligatoria en estos LEDs.
///
/// Los WS2812 son lineales en PWM y el ojo no: sin corregir, el 3 % del `Idle` se
/// ve como un 25 % y las respiraciones salen a saltos en vez de suaves.
///
/// La tabla aproxima γ≈2,2 mezclando las curvas exactas de γ=2 y γ=3 (4:1). Se
/// hace así porque en `const fn` no hay aritmética de coma flotante, y la mezcla
/// sale con enteros y da un error de un par de niveles frente a la curva real:
/// invisible, y a cambio la tabla se calcula en compilación y no ocupa código.
pub const GAMMA: [u8; 256] = {
    let mut table = [0u8; 256];
    let mut i = 0usize;
    while i < 256 {
        let x = i as u32;
        let g2 = x * x / 255; // γ = 2
        let g3 = x * x * x / (255 * 255); // γ = 3
        table[i] = ((g2 * 4 + g3) / 5) as u8;
        i += 1;
    }
    table
};

/// Aplica brillo global y gamma. Es el último paso antes del bus.
///
/// El orden importa: el brillo se aplica **en escala perceptual** (antes de la
/// gamma), que es como lo espera quien pone `led_brightness = 128` en `cfg.toml`
/// esperando "la mitad de brillo", no "la mitad de PWM".
pub fn finish(ring: Ring, brightness: u8) -> Ring {
    let mut out = OFF;
    for (i, color) in ring.iter().enumerate() {
        let c = color.scaled(brightness);
        out[i] = Rgb::new(GAMMA[c.r as usize], GAMMA[c.g as usize], GAMMA[c.b as usize]);
    }
    out
}

// -------------------------------------------------------------- animaciones

/// Onda triangular 0→255→0 sobre un periodo, para las respiraciones.
///
/// Triangular y no senoidal porque no hay coma flotante y, tras la gamma, la
/// diferencia no se aprecia: la curva de la gamma ya redondea las esquinas.
fn breathe(t_ms: u64, period_ms: u64) -> u8 {
    let phase = (t_ms % period_ms) * 512 / period_ms; // 0..511
    if phase < 256 {
        phase as u8
    } else {
        (511 - phase) as u8
    }
}

/// Índice del LED "cabeza" de un giro, a vueltas por segundo expresadas en
/// milésimas (1000 = 1 vuelta/s).
///
/// El reloj se reduce antes de multiplicar porque con un *uptime* largo el
/// producto se sale del `u64`. El módulo es de 1000 s exactos a propósito: en ese
/// punto el giro ha dado un número **entero** de vueltas para cualquier velocidad
/// (`milliturns * COUNT` siempre es múltiplo de `COUNT`), así que la reducción no
/// produce ningún salto visible en la animación.
fn head(t_ms: u64, milliturns_per_s: u64) -> usize {
    const WRAP_MS: u64 = 1_000_000;
    (((t_ms % WRAP_MS) * milliturns_per_s * COUNT as u64) / WRAP_MS) as usize % COUNT
}

/// Un LED encendido girando; el resto apagado.
fn spinner(color: Rgb, t_ms: u64, milliturns_per_s: u64) -> Ring {
    let mut ring = OFF;
    ring[head(t_ms, milliturns_per_s)] = color;
    ring
}

/// Cabeza brillante con estela detrás.
fn comet(color: Rgb, t_ms: u64, milliturns_per_s: u64, tail: &[u8]) -> Ring {
    let mut ring = OFF;
    let h = head(t_ms, milliturns_per_s);
    ring[h] = color;
    for (n, &k) in tail.iter().enumerate() {
        // Hacia atrás en el anillo, con módulo: la estela cruza el punto de unión
        // sin cortarse, que es justo donde se notaría el fallo.
        let idx = (h + COUNT - (n + 1) % COUNT) % COUNT;
        ring[idx] = color.scaled(k);
    }
    ring
}

/// Vúmetro: cuántos LEDs se encienden según el nivel, con el último a media luz
/// para que el salto entre niveles no sea tan brusco.
///
/// Es lo que más se agradece en uso real: ves si el micro te capta y si estás
/// demasiado lejos, sin tener que adivinarlo.
fn vu_meter(color: Rgb, level: u8) -> Ring {
    let mut ring = OFF;
    let scaled = level as usize * COUNT * 2 / 256; // en medios LED
    let full = scaled / 2;
    for slot in ring.iter_mut().take(full.min(COUNT)) {
        *slot = color;
    }
    if scaled % 2 == 1 && full < COUNT {
        ring[full] = color.scaled(70);
    }
    // Siempre al menos un punto tenue: un anillo del todo apagado mientras se
    // escucha parecería que el dispositivo se ha colgado.
    if ring[0].is_off() {
        ring[0] = color.scaled(25);
    }
    ring
}

/// Parpadeos rojos que deletrean un código de fallo, al estilo de los pitidos de
/// arranque de una BIOS: `k` destellos, pausa larga, y vuelta a empezar.
fn fault_blinks(kind: Fault, t_ms: u64) -> Ring {
    const ON_MS: u64 = 180;
    const OFF_MS: u64 = 220;
    const PAUSE_MS: u64 = 1_500;

    let blinks = kind.blinks() as u64;
    let train_ms = blinks * (ON_MS + OFF_MS);
    let phase = t_ms % (train_ms + PAUSE_MS);

    if phase < train_ms && phase % (ON_MS + OFF_MS) < ON_MS {
        [Rgb::RED; COUNT]
    } else {
        OFF
    }
}

/// El fotograma que toca pintar.
///
/// - `t_ms`: reloj monótono. Las animaciones son función de él, así que no hay
///   estado que mantener entre fotogramas.
/// - `level`: 0-255. En `Listening` es el RMS del micro (que el VAD ya calcula,
///   así que el vúmetro sale gratis); en `Speaking`, la amplitud del TTS. En el
///   resto de estados se ignora.
///
/// El resultado **aún no lleva brillo global ni gamma**: eso lo hace [`finish`],
/// separado para poder comprobar los patrones en los tests sin que la corrección
/// los distorsione.
pub fn frame(state: State, t_ms: u64, level: u8) -> Ring {
    match state {
        // Barrido rápido de una vuelta: confirma que los 7 LEDs viven.
        State::Booting => spinner(Rgb::WHITE.scaled(50), t_ms, 3_000),

        State::WifiConnecting { .. } => spinner(Rgb::BLUE, t_ms, 1_000),

        // Dos LEDs opuestos para distinguirlo de "buscando la red" incluso de
        // reojo: mismo color, movimiento distinto.
        State::ServerConnecting { .. } => {
            let mut ring = spinner(Rgb::BLUE, t_ms, 1_500);
            ring[(head(t_ms, 1_500) + COUNT / 2) % COUNT] = Rgb::BLUE.scaled(120);
            ring
        }

        State::Disconnected { .. } => spinner(Rgb::AMBER, t_ms, 300),

        // Un solo "faro" al 3 %, respirando muy despacio: dice "vivo, esperando"
        // sin molestar de noche.
        State::Idle => {
            let mut ring = OFF;
            let k = 4 + breathe(t_ms, 6_000) / 24; // ~1,5 % a ~6 %
            ring[0] = Rgb::WHITE.scaled(k);
            ring
        }

        State::Listening { .. } => vu_meter(Rgb::CYAN, level),

        State::Thinking { .. } => comet(Rgb::VIOLET, t_ms, 1_500, &[110, 40]),

        // Respira con la voz: acompaña lo que se oye en vez de ir por su cuenta.
        State::Speaking { .. } => {
            let k = 60 + (level as u16 * 195 / 255) as u8;
            [Rgb::GREEN.scaled(k); COUNT]
        }

        State::Fault { kind, .. } => fault_blinks(kind, t_ms),
    }
}

#[cfg(test)]
mod tests {
    extern crate std;

    use super::*;
    use luka_state::Fault;
    use std::vec::Vec;

    const TODOS_LOS_ESTADOS: [State; 9] = [
        State::Booting,
        State::WifiConnecting { since_ms: 0 },
        State::ServerConnecting { since_ms: 0, attempt: 0 },
        State::Disconnected { retry_at_ms: 0, attempt: 1 },
        State::Idle,
        State::Listening { since_ms: 0 },
        State::Thinking { since_ms: 0 },
        State::Speaking { since_ms: 0 },
        State::Fault { kind: Fault::Audio, since_ms: 0 },
    ];

    /// Cuántos LEDs están encendidos en un fotograma.
    fn encendidos(ring: &Ring) -> usize {
        ring.iter().filter(|c| !c.is_off()).count()
    }

    // ------------------------------------------------------------------ gamma

    #[test]
    fn la_gamma_no_cambia_los_extremos() {
        assert_eq!(GAMMA[0], 0, "el negro tiene que quedarse negro");
        assert_eq!(GAMMA[255], 255, "el blanco tiene que llegar al máximo");
    }

    #[test]
    fn la_gamma_es_monotona() {
        for i in 1..256 {
            assert!(GAMMA[i] >= GAMMA[i - 1], "la gamma baja en {i}: {:?}", &GAMMA[i - 2..=i]);
        }
    }

    /// El objetivo de la curva: los valores bajos tienen que salir MUCHO más
    /// bajos que en lineal, que es justo lo que hace que el 3 % se vea al 3 %.
    #[test]
    fn la_gamma_hunde_los_valores_bajos() {
        for i in 1..255 {
            assert!(GAMMA[i] <= i as u8, "la gamma sube el nivel {i}");
        }
        assert!(GAMMA[128] < 70, "el 50 % debería quedar bien por debajo: {}", GAMMA[128]);
        assert!(GAMMA[26] <= 2, "el 10 % debería quedar casi apagado: {}", GAMMA[26]);
    }

    #[test]
    fn el_brillo_global_escala_todo_el_anillo() {
        let lleno = [Rgb::WHITE; COUNT];
        assert_eq!(finish(lleno, 255)[0], Rgb::WHITE);
        assert!(finish(lleno, 128)[0].r < 128, "el brillo a la mitad debería notarse");
        assert_eq!(finish(lleno, 0), OFF, "brillo 0 tiene que apagar el anillo");
    }

    // ------------------------------------------------------- las animaciones

    /// Ningún estado puede dejar el anillo entero apagado de forma permanente: un
    /// anillo negro es indistinguible de un dispositivo muerto.
    #[test]
    fn ningun_estado_deja_el_anillo_muerto() {
        for state in TODOS_LOS_ESTADOS {
            let vivo = (0..4_000)
                .step_by(20)
                .any(|t| encendidos(&frame(state, t, 0)) > 0);
            assert!(vivo, "{state:?} deja el anillo apagado durante 4 s enteros");
        }
    }

    /// El movimiento es lo que distingue los estados, así que los que giran tienen
    /// que girar de verdad: recorrer los 7 LEDs, no quedarse en uno.
    #[test]
    fn los_estados_de_espera_recorren_el_anillo_entero() {
        for state in [
            State::WifiConnecting { since_ms: 0 },
            State::ServerConnecting { since_ms: 0, attempt: 0 },
            State::Disconnected { retry_at_ms: 0, attempt: 1 },
            State::Thinking { since_ms: 0 },
        ] {
            let mut vistos = [false; COUNT];
            for t in (0..10_000).step_by(10) {
                for (i, c) in frame(state, t, 0).iter().enumerate() {
                    if !c.is_off() {
                        vistos[i] = true;
                    }
                }
            }
            assert!(vistos.iter().all(|v| *v), "{state:?} no llega a todos los LEDs: {vistos:?}");
        }
    }

    #[test]
    fn el_vumetro_sube_con_la_voz() {
        let cuenta = |level| encendidos(&frame(State::Listening { since_ms: 0 }, 0, level));
        assert_eq!(cuenta(0), 1, "en silencio debe quedar el punto tenue");
        assert!(cuenta(128) > cuenta(0));
        assert!(cuenta(255) > cuenta(128));
        assert_eq!(cuenta(255), COUNT, "a tope se encienden los 7");
    }

    #[test]
    fn el_vumetro_es_monotono() {
        let mut previo = 0;
        for level in 0..=255u8 {
            let n = encendidos(&frame(State::Listening { since_ms: 0 }, 0, level));
            assert!(n >= previo, "el vúmetro baja al subir el nivel a {level}");
            previo = n;
        }
    }

    /// Escuchando nunca se apaga del todo: si no, no se sabe si te está oyendo o
    /// si se ha colgado.
    #[test]
    fn escuchando_siempre_hay_algo_encendido() {
        for level in 0..=255u8 {
            assert!(encendidos(&frame(State::Listening { since_ms: 0 }, 0, level)) > 0);
        }
    }

    #[test]
    fn el_faro_del_reposo_es_tenue_y_respira() {
        let brillos: Vec<u8> = (0..6_000).step_by(50).map(|t| frame(State::Idle, t, 0)[0].r).collect();
        let max = *brillos.iter().max().unwrap();
        let min = *brillos.iter().min().unwrap();
        assert!(max > min, "el faro no respira");
        assert!(max < 40, "el faro molesta de noche: llega a {max}");

        // Y es un solo LED: el resto del anillo, apagado.
        assert_eq!(encendidos(&frame(State::Idle, 0, 0)), 1);
    }

    #[test]
    fn hablando_el_anillo_acompana_la_voz() {
        let flojo = frame(State::Speaking { since_ms: 0 }, 0, 20)[0];
        let fuerte = frame(State::Speaking { since_ms: 0 }, 0, 255)[0];
        assert!(fuerte.g > flojo.g, "el verde no sigue a la amplitud del TTS");
        assert!(flojo.g > 0, "en los silencios del habla no debe apagarse");
    }

    // ------------------------------------------------------------- los fallos

    /// El código de parpadeos es el diagnóstico sin cable: si se cuentan mal los
    /// destellos, se mira el problema equivocado.
    #[test]
    fn cada_fallo_parpadea_su_numero() {
        for kind in [
            Fault::WifiAssoc, Fault::ServerUnreachable, Fault::AuthRejected,
            Fault::Audio, Fault::WakeWord, Fault::Panic,
        ] {
            let state = State::Fault { kind, since_ms: 0 };
            // Contar flancos de subida a lo largo de un ciclo completo.
            let mut destellos = 0;
            let mut encendido_antes = false;
            let ciclo = kind.blinks() as u64 * 400 + 1_500;
            for t in 0..ciclo {
                let ahora = !frame(state, t, 0)[0].is_off();
                if ahora && !encendido_antes {
                    destellos += 1;
                }
                encendido_antes = ahora;
            }
            assert_eq!(destellos, kind.blinks(), "{kind:?} parpadea {destellos} veces");
        }
    }

    #[test]
    fn los_fallos_se_ven_en_rojo_y_en_todo_el_anillo() {
        let state = State::Fault { kind: Fault::Audio, since_ms: 0 };
        let encendido = (0..2_000)
            .map(|t| frame(state, t, 0))
            .find(|r| encendidos(r) > 0)
            .expect("el fallo nunca se enciende");
        assert!(encendido.iter().all(|c| *c == Rgb::RED), "un fallo debe verse rojo entero");
    }

    /// La pausa entre trenes es lo que permite contarlos: sin ella, 3 parpadeos y
    /// 4 parpadeos se confunden.
    #[test]
    fn entre_trenes_de_parpadeos_hay_una_pausa_larga() {
        let state = State::Fault { kind: Fault::Audio, since_ms: 0 };
        let mut apagado_seguido = 0u64;
        let mut peor = 0u64;
        for t in 0..8_000 {
            if frame(state, t, 0)[0].is_off() {
                apagado_seguido += 1;
                peor = peor.max(apagado_seguido);
            } else {
                apagado_seguido = 0;
            }
        }
        assert!(peor >= 1_400, "la pausa se queda en {peor} ms; no da tiempo a contar");
    }

    // ------------------------------------------------------------ robustez

    #[test]
    fn ningun_estado_revienta_en_ningun_instante() {
        for state in TODOS_LOS_ESTADOS {
            for t in [0u64, 1, 999, 60_000, u64::MAX / 2, u64::MAX] {
                for level in [0u8, 1, 127, 255] {
                    let _ = finish(frame(state, t, level), 128);
                }
            }
        }
    }
}
