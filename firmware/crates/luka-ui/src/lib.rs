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

/// Aplica gamma y brillo global. Es el último paso antes del bus.
///
/// **El orden importa, y la primera versión lo tenía al revés.** Aplicar el
/// brillo antes de la gamma parece lo natural —"la mitad de brillo percibido"—
/// pero compone dos atenuaciones sobre un `u8`: con el `led_brightness = 48` real
/// de `cfg.toml`, hasta el rojo a plena saturación salía a **7/255** y el estado
/// `Booting` a **(0,0,0)**. El anillo se quedaba negro.
///
/// Así que la gamma va primero, sobre el color lógico a rango completo (que es
/// para lo que está: convertir intensidad percibida en ciclo de trabajo), y el
/// brillo global escala **linealmente el PWM resultante**. Es decir, el brillo es
/// un techo de potencia, no un atenuador perceptual: con 48 el anillo llega como
/// mucho a 48/255 de ciclo, que es exactamente lo que se quiere de un "no me
/// deslumbres".
pub fn finish(ring: Ring, brightness: u8) -> Ring {
    let ceiling = |canal: u8| -> u8 {
        let salida = ((GAMMA[canal as usize] as u16 * brightness as u16) / 255) as u8;
        // **Suelo de 1.** Sin esto, pedir poca luz da negro exacto: la gamma
        // entera manda a cero todo lo que esté por debajo de 23, y el "faro" del
        // reposo pide entre 4 y 14. Estuvo apagado desde el primer día y parecía
        // que el anillo no funcionaba.
        //
        // No es un apaño contra la gamma: es que 1/255 es lo mínimo que el LED
        // sabe encender, así que redondear hacia abajo hasta apagarlo pierde la
        // única distinción que importa —encendido o apagado— justo donde el
        // diseño la estaba usando. Con el brillo global a 0 sí se apaga todo,
        // que es lo que se pide ahí.
        if salida == 0 && canal > 0 && brightness > 0 {
            1
        } else {
            salida
        }
    };
    let mut out = OFF;
    for (i, color) in ring.iter().enumerate() {
        out[i] = Rgb::new(ceiling(color.r), ceiling(color.g), ceiling(color.b));
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

/// Modo calibración: la **confianza del detector** como vúmetro.
///
/// Es la única forma práctica de ajustar el umbral de la wake word sin
/// instrumentar nada ni tener la placa enchufada al PC: te pones a tres metros,
/// dices "Luka" y ves hasta dónde sube el anillo. Si llega al final, el umbral
/// puede subir; si se queda a la mitad, hay que bajarlo.
///
/// En violeta y no en cian para que no se confunda con el vúmetro de micro de
/// `Listening`: son dos cosas distintas —cuánto suena y cuánto se parece a
/// "Luka"— y confundirlas al calibrar lleva a ajustar el umbral mirando el
/// número equivocado.
pub fn calibration(confianza: u8) -> Ring {
    vu_meter(Rgb::VIOLET, confianza)
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
            // El rango tiene que ser lo bastante ancho como para que **la gamma
            // lo separe en varios escalones**; si no, la respiración se queda
            // clavada en un solo valor y parece un LED fijo. Con el brillo real
            // de `cfg.toml` esto recorre unos cinco niveles de PWM, que de noche
            // es un latido suave y de día casi no se aprecia. Lo fija el test
            // `el_faro_del_reposo_respira_despues_de_la_gamma`.
            let k = 24 + (breathe(t_ms, 6_000) as u16 * 68 / 255) as u8;
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
        State::Listening { since_ms: 0, hands_free: false },
        State::Thinking { since_ms: 0 },
        State::Speaking { since_ms: 0 },
        State::Fault { kind: Fault::Audio, since_ms: 0 },
    ];

    /// Cuántos LEDs están encendidos en un fotograma.
    fn encendidos(ring: &Ring) -> usize {
        ring.iter().filter(|c| !c.is_off()).count()
    }

    /// El anillo de calibración tiene que ser monótono y distinguirse del
    /// vúmetro de micro: si se parecieran, se calibraría mirando el otro.
    #[test]
    fn la_calibracion_sube_con_la_confianza_y_no_es_cian() {
        let mut anterior = 0;
        for confianza in [0u8, 64, 128, 192, 255] {
            let n = encendidos(&calibration(confianza));
            assert!(n >= anterior, "la calibración bajó con más confianza");
            anterior = n;
        }
        assert!(encendidos(&calibration(0)) > 0, "a cero debería quedar un punto tenue");
        assert_eq!(encendidos(&calibration(255)), COUNT, "al máximo debería llenarse");

        let calibrando = calibration(255);
        let escuchando = frame(State::Listening { since_ms: 0, hands_free: true }, 0, 255);
        assert_ne!(calibrando[0], escuchando[0], "calibración y escucha se ven igual");
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

    /// El fallo que dejó el "faro" del reposo apagado desde el primer día: la
    /// gamma entera manda a cero todo lo que baje de 23, y el reposo pide entre
    /// 4 y 14. Pedir poca luz **nunca** puede dar negro exacto.
    #[test]
    fn pedir_poca_luz_no_puede_apagar_el_led() {
        for canal in 1..=22u8 {
            let salida = finish([Rgb::new(canal, canal, canal); COUNT], 48)[0];
            assert!(
                salida.r > 0,
                "un blanco al nivel {canal} sale completamente negro"
            );
        }
        // Y el reposo, que es el caso real, tiene que verse encendido siempre.
        for t in (0..6_000).step_by(100) {
            let faro = finish(frame(State::Idle, t, 0), 48)[0];
            assert!(faro.r > 0, "el faro del reposo está apagado en t={t}");
        }
    }

    /// Pero el brillo global a cero sí apaga: es la única forma de callar el
    /// anillo del todo, y el suelo no puede pisarla.
    #[test]
    fn el_brillo_a_cero_sigue_apagando_del_todo() {
        assert_eq!(finish([Rgb::WHITE; COUNT], 0), OFF);
        assert_eq!(finish(frame(State::Idle, 0, 0), 0), OFF);
    }

    #[test]
    fn el_brillo_global_es_un_techo_lineal_del_pwm() {
        let lleno = [Rgb::WHITE; COUNT];
        assert_eq!(finish(lleno, 255)[0], Rgb::WHITE, "a tope no debe atenuar nada");
        assert_eq!(finish(lleno, 128)[0].r, 128, "el brillo es el techo del ciclo de trabajo");
        assert_eq!(finish(lleno, 0), OFF, "brillo 0 tiene que apagar el anillo");
    }

    /// El fallo que dejó el anillo negro en la placa: con el `led_brightness = 48`
    /// real de `cfg.toml`, componer brillo y gamma sobre un `u8` machacaba todos
    /// los colores a 0-7. Los estados que existen para VERSE tienen que verse con
    /// el brillo que está configurado de verdad, no solo con el brillo a tope.
    #[test]
    fn los_estados_visibles_se_ven_con_el_brillo_configurado() {
        /// El mismo valor que `cfg.toml`.
        const BRILLO_REAL: u8 = 48;
        /// Por debajo de esto, a través del difusor y con luz en la habitación,
        /// no se distingue de apagado.
        const MINIMO_VISIBLE: u8 = 16;

        for (nombre, state, level) in [
            ("WifiConnecting", State::WifiConnecting { since_ms: 0 }, 0u8),
            ("ServerConnecting", State::ServerConnecting { since_ms: 0, attempt: 0 }, 0),
            ("Disconnected", State::Disconnected { retry_at_ms: 0, attempt: 1 }, 0),
            ("Listening", State::Listening { since_ms: 0, hands_free: false }, 255),
            ("Thinking", State::Thinking { since_ms: 0 }, 0),
            ("Speaking", State::Speaking { since_ms: 0 }, 255),
            ("Fault", State::Fault { kind: Fault::Audio, since_ms: 0 }, 0),
        ] {
            let pico = (0..4_000)
                .step_by(10)
                .flat_map(|t| {
                    finish(frame(state, t, level), BRILLO_REAL)
                        .into_iter()
                        .map(|c| c.r.max(c.g).max(c.b))
                        .collect::<Vec<_>>()
                })
                .max()
                .unwrap_or(0);

            assert!(
                pico >= MINIMO_VISIBLE,
                "{nombre} llega como mucho a {pico}/255 con brillo {BRILLO_REAL}: invisible"
            );
        }
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
        let cuenta = |level| encendidos(&frame(State::Listening { since_ms: 0, hands_free: false }, 0, level));
        assert_eq!(cuenta(0), 1, "en silencio debe quedar el punto tenue");
        assert!(cuenta(128) > cuenta(0));
        assert!(cuenta(255) > cuenta(128));
        assert_eq!(cuenta(255), COUNT, "a tope se encienden los 7");
    }

    #[test]
    fn el_vumetro_es_monotono() {
        let mut previo = 0;
        for level in 0..=255u8 {
            let n = encendidos(&frame(State::Listening { since_ms: 0, hands_free: false }, 0, level));
            assert!(n >= previo, "el vúmetro baja al subir el nivel a {level}");
            previo = n;
        }
    }

    /// Escuchando nunca se apaga del todo: si no, no se sabe si te está oyendo o
    /// si se ha colgado.
    #[test]
    fn escuchando_siempre_hay_algo_encendido() {
        for level in 0..=255u8 {
            assert!(encendidos(&frame(State::Listening { since_ms: 0, hands_free: false }, 0, level)) > 0);
        }
    }

    /// El faro tiene que respirar **en lo que sale al bus**, no solo en el valor
    /// lógico.
    ///
    /// La versión anterior de este test miraba `frame()` a secas y por eso daba
    /// verde mientras en la placa se veía un LED completamente fijo: el rango
    /// lógico variaba, pero la gamma lo aplastaba todo al mismo escalón de PWM.
    /// Comprobar antes de la última transformación es no comprobar nada.
    #[test]
    fn el_faro_del_reposo_respira_despues_de_la_gamma() {
        const BRILLO_REAL: u8 = 48;
        let salidas: Vec<u8> = (0..6_000)
            .step_by(50)
            .map(|t| finish(frame(State::Idle, t, 0), BRILLO_REAL)[0].r)
            .collect();

        let max = *salidas.iter().max().unwrap();
        let min = *salidas.iter().min().unwrap();
        assert!(min > 0, "el faro se apaga del todo en algún momento");
        assert!(max > min, "el faro no respira: se queda clavado en {max}");
        assert!(max - min >= 2, "la respiración es de un solo escalón ({min}..{max}): no se aprecia");
        assert!(max <= 8, "el faro molesta de noche: llega a {max}/255");

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
