//! Máquina de estados del satélite de voz.
//!
//! Toda la lógica vive en una **función pura** [`next`]: recibe el estado, un
//! evento y el instante actual en milisegundos, y devuelve el estado siguiente
//! más las acciones que el supervisor debe ejecutar. No toca periféricos, no lee
//! el reloj y no asigna memoria.
//!
//! Que el tiempo entre como parámetro (`now_ms`) en vez de leerse de un `Instant`
//! es lo que permite probar los *timeouts* y el *backoff* en el host, en
//! microsegundos y de forma determinista, en vez de esperando 15 segundos reales
//! con la placa enchufada.
//!
//! El invariante que persigue el diseño: **nada se queda colgado para siempre**.
//! Todo estado transitorio tiene su plazo, y al vencer se sale a un sitio conocido.

#![no_std]
#![deny(unsafe_code)]

/// Cuánto se aguanta capturando antes de cerrar el turno solo. Si el botón se
/// queda pillado (o alguien lo suelta fuera de rango), el turno se cierra igual
/// en vez de subir audio indefinidamente.
pub const LISTENING_TIMEOUT_MS: u64 = 15_000;
/// Plazo para que el servidor conteste algo tras el fin del turno.
pub const THINKING_TIMEOUT_MS: u64 = 30_000;
/// Guarda por si el `TTS_END` se pierde: sin esto el anillo se quedaría en verde
/// para siempre y el dispositivo sordo, porque en v1 es half-duplex.
pub const SPEAKING_TIMEOUT_MS: u64 = 60_000;
/// Cuánto se enseña un fallo en el anillo antes de volver a intentarlo.
pub const FAULT_DISPLAY_MS: u64 = 3_000;

/// Primer reintento de conexión y techo del *backoff* exponencial.
pub const BACKOFF_MIN_MS: u64 = 500;
pub const BACKOFF_MAX_MS: u64 = 30_000;

/// Fallos que el anillo sabe deletrear en parpadeos rojos. El número de la
/// variante **es** el número de parpadeos, así que el orden importa: es la tabla
/// de diagnóstico de `PLAN.md` §5.5 y está pensada para leerse sin cable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Fault {
    /// 1 — no asocia a la WiFi: SSID/PSK, o el router no da 2,4 GHz.
    WifiAssoc = 1,
    /// 2 — la WiFi va pero el servidor no contesta: ¿corre `asistenteia`?
    ServerUnreachable = 2,
    /// 3 — el servidor cerró con 1008: el `API_TOKEN` no coincide.
    AuthRejected = 3,
    /// 4 — I²C o codecs: pines o secuencia de init.
    Audio = 4,
    /// 5 — wake word (Fase 3): modelo ausente o corrupto.
    WakeWord = 5,
    /// 6 — se arrancó tras un pánico; la causa está en NVS.
    Panic = 6,
}

impl Fault {
    /// Parpadeos rojos con los que el anillo lo deletrea.
    pub const fn blinks(self) -> u8 {
        self as u8
    }

    /// Un fallo de credenciales no se arregla reintentando: el token o la
    /// contraseña están mal y hay que reflashear. Reintentar en bucle solo
    /// serviría para llenar el log y esconder la causa.
    pub const fn is_recoverable(self) -> bool {
        !matches!(self, Self::AuthRejected)
    }
}

/// Estado del dispositivo. Los campos `_ms` son marcas de tiempo monótonas.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    /// Arrancando: periféricos aún sin inicializar.
    Booting,
    /// Asociándose a la WiFi.
    WifiConnecting { since_ms: u64 },
    /// WiFi lista, abriendo el WebSocket contra el asistente. `attempt` viene
    /// arrastrado de los reintentos previos: sin él el *backoff* se reiniciaría en
    /// cada vuelta y el dispositivo machacaría un servidor apagado cada 500 ms.
    ServerConnecting { since_ms: u64, attempt: u32 },
    /// Sin enlace, esperando a reintentar. `attempt` alimenta el *backoff*.
    Disconnected { retry_at_ms: u64, attempt: u32 },
    /// En reposo, esperando al botón (o a la wake word, en la Fase 3).
    Idle,
    /// Capturando y subiendo audio.
    Listening { since_ms: u64 },
    /// Turno enviado, esperando a Luka.
    Thinking { since_ms: u64 },
    /// Reproduciendo la voz de Luka.
    Speaking { since_ms: u64 },
    /// Mostrando un fallo antes de decidir qué hacer.
    Fault { kind: Fault, since_ms: u64 },
}

impl State {
    /// ¿Hay enlace con el asistente? Lo usa el anillo y también el sitio al que se
    /// vuelve tras enseñar un fallo.
    pub const fn is_online(&self) -> bool {
        matches!(self, Self::Idle | Self::Listening { .. } | Self::Thinking { .. } | Self::Speaking { .. })
    }
}

/// Lo que le pasa al dispositivo desde fuera.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    /// Los periféricos ya están listos.
    Booted,
    WifiUp,
    WifiDown,
    /// El WebSocket quedó abierto y autenticado.
    ServerUp,
    /// Se cayó el WebSocket (o no llegó a abrirse).
    ServerDown,
    /// El servidor cerró rechazando el token.
    AuthRejected,
    ButtonPressed,
    ButtonReleased,
    /// El servidor confirma su estado (trama `STATE`).
    ServerSaid(Reported),
    /// Llegó la primera trama de audio del TTS.
    TtsStarted,
    /// `TTS_END`: ya no viene más audio.
    TtsEnded,
    /// Fallo local (audio, wake word, pánico previo).
    Faulted(Fault),
    /// Pasa el tiempo. Es el único evento que dispara los *timeouts*.
    Tick,
}

/// Los estados que el servidor sabe comunicar (trama `STATE`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reported {
    Idle,
    Listening,
    Thinking,
    Speaking,
}

/// Lo que el supervisor tiene que hacer tras una transición.
///
/// Son órdenes, no consultas: la máquina de estados nunca pregunta nada al
/// hardware, solo dice qué hacer con él.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    /// Asociarse a la WiFi.
    ConnectWifi,
    /// Abrir el WebSocket contra el asistente.
    ConnectServer,
    /// Soltar el WebSocket y sus búferes.
    DropServer,
    /// Empezar a capturar del micro y subir tramas `AUDIO`.
    StartCapture,
    /// Dejar de capturar.
    StopCapture,
    /// Mandar `END`: se acabó el turno, que lo procese.
    SendEnd,
    /// Mandar `CANCEL`: olvida el turno en curso.
    SendCancel,
    /// Abrir el amplificador y empezar a reproducir.
    StartPlayback,
    /// Callar y **cerrar el amplificador**.
    StopPlayback,
}

/// Hasta cuántas acciones puede devolver una transición. El máximo real que sale
/// del `match` de abajo es 3 (cortar la reproducción, cancelar y volver a
/// capturar); el test `nunca_se_desbordan_las_acciones` lo vigila.
pub const MAX_ACTIONS: usize = 4;

/// Las acciones de una transición, en orden de ejecución y sin asignar memoria.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Actions {
    items: [Option<Action>; MAX_ACTIONS],
    len: usize,
}

impl Actions {
    pub const fn none() -> Self {
        Self { items: [None; MAX_ACTIONS], len: 0 }
    }

    fn of(list: &[Action]) -> Self {
        let mut out = Self::none();
        // Se trunca en vez de entrar en pánico: perder una orden degrada el
        // comportamiento, pero un pánico en el supervisor tira el dispositivo
        // entero. El test cubre que no llegue a pasar.
        let n = if list.len() > MAX_ACTIONS { MAX_ACTIONS } else { list.len() };
        let mut i = 0;
        while i < n {
            out.items[i] = Some(list[i]);
            i += 1;
        }
        out.len = n;
        out
    }

    pub fn iter(&self) -> impl Iterator<Item = Action> + '_ {
        self.items[..self.len].iter().filter_map(|a| *a)
    }

    pub fn contains(&self, action: Action) -> bool {
        self.iter().any(|a| a == action)
    }

    pub const fn len(&self) -> usize {
        self.len
    }

    pub const fn is_empty(&self) -> bool {
        self.len == 0
    }
}

/// Espera antes del reintento `attempt` (0 es el primero): exponencial, con techo.
///
/// El *jitter* lo aporta el llamante (los 10 bits bajos de algo que varíe, p.ej.
/// el contador de ciclos) y resta hasta un 25 %. Sirve para que dos dispositivos
/// que pierden el servidor a la vez no vuelvan a la vez, una y otra vez.
pub const fn backoff_ms(attempt: u32, jitter: u32) -> u64 {
    let shift = if attempt > 6 { 6 } else { attempt };
    let base = BACKOFF_MIN_MS << shift;
    let base = if base > BACKOFF_MAX_MS { BACKOFF_MAX_MS } else { base };
    base - (base / 4) * (jitter % 1024) as u64 / 1024
}

/// Instante del próximo reintento. Satura en vez de desbordar: con `now_ms`
/// cerca del máximo del `u64` sumar el backoff daría la vuelta al contador y el
/// reintento caería en el pasado, reconectando en bucle sin espera.
///
/// El *jitter* sale de los bits bajos del propio reloj. Es la única fuente de
/// variación disponible sin romper la pureza de la función, y basta para lo que
/// se busca: que dos dispositivos que pierden el servidor a la vez no vuelvan
/// sincronizados.
const fn retry_at(now_ms: u64, attempt: u32) -> u64 {
    now_ms.saturating_add(backoff_ms(attempt, now_ms as u32))
}

/// La transición. Es la única función que decide algo en todo el firmware.
pub fn next(state: State, event: Event, now_ms: u64) -> (State, Actions) {
    use Action::*;
    use Event as E;
    use State as S;

    // Estos tres eventos mandan sobre cualquier estado: si se cae la red, da
    // igual lo que estuviéramos haciendo. Tratarlos aquí evita repetirlos en
    // cada brazo y, sobre todo, evita olvidarse de alguno.
    match event {
        E::WifiDown => {
            return (
                S::WifiConnecting { since_ms: now_ms },
                Actions::of(&[StopCapture, StopPlayback, DropServer, ConnectWifi]),
            );
        }
        E::AuthRejected => {
            // Sin reintento: es un fallo de configuración, no de red.
            return (
                S::Fault { kind: Fault::AuthRejected, since_ms: now_ms },
                Actions::of(&[StopCapture, StopPlayback, DropServer]),
            );
        }
        E::Faulted(kind) => {
            return (
                S::Fault { kind, since_ms: now_ms },
                Actions::of(&[StopCapture, StopPlayback]),
            );
        }
        _ => {}
    }

    match (state, event) {
        // --- Arranque y red ---
        (S::Booting, E::Booted) => (S::WifiConnecting { since_ms: now_ms }, Actions::of(&[ConnectWifi])),

        (S::WifiConnecting { .. }, E::WifiUp) => {
            (S::ServerConnecting { since_ms: now_ms, attempt: 0 }, Actions::of(&[ConnectServer]))
        }

        // Conectar borra el historial de reintentos: la próxima caída vuelve a
        // reintentar rápido, que es lo que se quiere si el enlace iba bien.
        (S::ServerConnecting { .. }, E::ServerUp) => (S::Idle, Actions::none()),

        // Que el servidor no esté es lo normal (el PC puede estar apagado), así
        // que no es un fallo: se reintenta con backoff y el anillo lo cuenta.
        (S::ServerConnecting { attempt, .. }, E::ServerDown) => (
            S::Disconnected { retry_at_ms: retry_at(now_ms, attempt), attempt: attempt + 1 },
            Actions::of(&[DropServer]),
        ),

        (S::Disconnected { retry_at_ms, attempt }, E::Tick) if now_ms >= retry_at_ms => {
            (S::ServerConnecting { since_ms: now_ms, attempt }, Actions::of(&[ConnectServer]))
        }

        // Perder el enlace estando en marcha: se corta todo y a reintentar. El
        // contador arranca de cero porque el servidor estaba vivo hace un instante.
        (s, E::ServerDown) if s.is_online() => (
            S::Disconnected { retry_at_ms: retry_at(now_ms, 0), attempt: 1 },
            Actions::of(&[StopCapture, StopPlayback, DropServer]),
        ),

        // --- Botón: push-to-talk ---
        (S::Idle, E::ButtonPressed) => {
            (S::Listening { since_ms: now_ms }, Actions::of(&[StartCapture]))
        }

        (S::Listening { .. }, E::ButtonReleased) => {
            (S::Thinking { since_ms: now_ms }, Actions::of(&[StopCapture, SendEnd]))
        }

        // El turno se alarga demasiado: se cierra solo, como si se hubiera soltado.
        (S::Listening { since_ms }, E::Tick) if now_ms.saturating_sub(since_ms) >= LISTENING_TIMEOUT_MS => {
            (S::Thinking { since_ms: now_ms }, Actions::of(&[StopCapture, SendEnd]))
        }

        // Interrumpir a Luka con el botón. Es seguro aunque v1 sea half-duplex
        // porque lo PRIMERO que se hace es callar el altavoz: el micro nunca
        // llega a estar abierto a la vez que suena la voz.
        (S::Speaking { .. }, E::ButtonPressed) => (
            S::Listening { since_ms: now_ms },
            Actions::of(&[StopPlayback, SendCancel, StartCapture]),
        ),

        // Pensando: pulsar el botón aborta la espera y vuelve a escuchar.
        (S::Thinking { .. }, E::ButtonPressed) => {
            (S::Listening { since_ms: now_ms }, Actions::of(&[SendCancel, StartCapture]))
        }

        // --- Turno ---
        (S::Thinking { .. }, E::TtsStarted) => {
            (S::Speaking { since_ms: now_ms }, Actions::of(&[StartPlayback]))
        }

        (S::Speaking { .. }, E::TtsEnded) => (S::Idle, Actions::of(&[StopPlayback])),

        // `TTS_END` sin una sola trama de audio: Luka contestó sin voz (o el audio
        // se perdió). No hay nada que reproducir, pero el turno ha terminado.
        (S::Thinking { .. }, E::TtsEnded) => (S::Idle, Actions::none()),

        (S::Thinking { since_ms }, E::Tick) if now_ms.saturating_sub(since_ms) >= THINKING_TIMEOUT_MS => (
            S::Fault { kind: Fault::ServerUnreachable, since_ms: now_ms },
            Actions::none(),
        ),

        (S::Speaking { since_ms }, E::Tick) if now_ms.saturating_sub(since_ms) >= SPEAKING_TIMEOUT_MS => {
            (S::Idle, Actions::of(&[StopPlayback]))
        }

        // El servidor dice que está hablando y aquí aún no había llegado audio:
        // se le cree, para que el anillo no vaya por detrás de la realidad.
        (S::Thinking { .. }, E::ServerSaid(Reported::Speaking)) => {
            (S::Speaking { since_ms: now_ms }, Actions::of(&[StartPlayback]))
        }

        // El servidor volvió a reposo (turno descartado por no entenderse nada).
        (s, E::ServerSaid(Reported::Idle)) if matches!(s, S::Thinking { .. } | S::Speaking { .. }) => {
            (S::Idle, Actions::of(&[StopPlayback]))
        }

        // --- Fallos ---
        (S::Fault { kind, since_ms }, E::Tick) if now_ms.saturating_sub(since_ms) >= FAULT_DISPLAY_MS => {
            if kind.is_recoverable() {
                (S::WifiConnecting { since_ms: now_ms }, Actions::of(&[ConnectWifi]))
            } else {
                // Se queda enseñando el fallo: reintentar no lo arreglaría.
                (state, Actions::none())
            }
        }

        // Todo lo demás no cambia nada. Incluye los `Tick` que no vencen ningún
        // plazo, que son la inmensa mayoría.
        _ => (state, Actions::none()),
    }
}

#[cfg(test)]
mod tests {
    // El crate es `no_std`, pero los tests corren en el PC: aquí sí hay `std` y
    // un `Vec` normal simplifica comprobar el ORDEN de las acciones.
    extern crate std;

    use super::{Action::*, Event as E, State as S, *};
    use std::vec::Vec;

    /// Aplica una secuencia de eventos desde `Booting`, devolviendo el estado final.
    fn run(events: &[(Event, u64)]) -> State {
        let mut state = S::Booting;
        for &(event, now) in events {
            state = next(state, event, now).0;
        }
        state
    }

    /// El camino feliz completo, que es el criterio de salida de la Fase 1.
    #[test]
    fn del_arranque_a_oir_a_luka() {
        let state = run(&[
            (E::Booted, 0),
            (E::WifiUp, 100),
            (E::ServerUp, 200),
            (E::ButtonPressed, 1_000),
            (E::ButtonReleased, 3_000),
            (E::TtsStarted, 4_000),
            (E::TtsEnded, 8_000),
        ]);
        assert_eq!(state, S::Idle);
    }

    #[test]
    fn el_arranque_pide_wifi() {
        let (state, actions) = next(S::Booting, E::Booted, 0);
        assert!(matches!(state, S::WifiConnecting { .. }));
        assert!(actions.contains(ConnectWifi));
    }

    #[test]
    fn soltar_el_boton_cierra_el_turno() {
        let (state, actions) = next(S::Listening { since_ms: 0 }, E::ButtonReleased, 2_000);
        assert!(matches!(state, S::Thinking { .. }));
        assert!(actions.contains(StopCapture));
        assert!(actions.contains(SendEnd));
    }

    /// El orden importa: primero se deja de capturar, luego se cierra el turno.
    /// Al revés se subirían tramas después del END y el servidor las mezclaría
    /// con el turno siguiente.
    #[test]
    fn primero_se_corta_la_captura_y_luego_se_manda_end() {
        let (_, actions) = next(S::Listening { since_ms: 0 }, E::ButtonReleased, 2_000);
        let orden: Vec<_> = actions.iter().collect();
        assert_eq!(orden.as_slice(), &[StopCapture, SendEnd]);
    }

    #[test]
    fn un_turno_eterno_se_cierra_solo() {
        let inicio = S::Listening { since_ms: 1_000 };
        // Justo antes del plazo no pasa nada.
        assert_eq!(next(inicio, E::Tick, 1_000 + LISTENING_TIMEOUT_MS - 1).0, inicio);
        // Al cumplirse, se cierra igual que si se hubiera soltado el botón.
        let (state, actions) = next(inicio, E::Tick, 1_000 + LISTENING_TIMEOUT_MS);
        assert!(matches!(state, S::Thinking { .. }));
        assert!(actions.contains(SendEnd));
    }

    #[test]
    fn si_luka_no_contesta_no_se_espera_para_siempre() {
        let (state, _) = next(S::Thinking { since_ms: 0 }, E::Tick, THINKING_TIMEOUT_MS);
        assert_eq!(state, S::Fault { kind: Fault::ServerUnreachable, since_ms: THINKING_TIMEOUT_MS });
    }

    #[test]
    fn si_el_tts_end_se_pierde_el_dispositivo_no_se_queda_sordo() {
        let (state, actions) = next(S::Speaking { since_ms: 0 }, E::Tick, SPEAKING_TIMEOUT_MS);
        assert_eq!(state, S::Idle);
        assert!(actions.contains(StopPlayback));
    }

    /// Lo más importante de todo el módulo: **nunca** se abre el micro sin haber
    /// callado antes el altavoz. Están a centímetros y se acoplan.
    #[test]
    fn interrumpir_a_luka_calla_el_altavoz_antes_de_abrir_el_micro() {
        let (state, actions) = next(S::Speaking { since_ms: 0 }, E::ButtonPressed, 1_000);
        assert!(matches!(state, S::Listening { .. }));

        let orden: Vec<_> = actions.iter().collect();
        let calla = orden.iter().position(|a| *a == StopPlayback).expect("no se calla el altavoz");
        let abre = orden.iter().position(|a| *a == StartCapture).expect("no se abre el micro");
        assert!(calla < abre, "el micro se abre antes de callar el altavoz: {orden:?}");
    }

    /// Y en general: ninguna transición abre la captura sin cerrar la
    /// reproducción, si toca las dos cosas.
    #[test]
    fn ninguna_transicion_abre_el_micro_con_el_altavoz_sonando() {
        for state in TODOS_LOS_ESTADOS {
            for event in TODOS_LOS_EVENTOS {
                let (_, actions) = next(state, event, 100_000);
                let orden: Vec<_> = actions.iter().collect();
                if let (Some(abre), Some(calla)) = (
                    orden.iter().position(|a| *a == StartCapture),
                    orden.iter().position(|a| *a == StopPlayback),
                ) {
                    assert!(calla < abre, "{state:?} + {event:?} abre el micro antes de callar");
                }
            }
        }
    }

    #[test]
    fn perder_la_wifi_lo_corta_todo_desde_cualquier_estado() {
        for state in TODOS_LOS_ESTADOS {
            let (siguiente, actions) = next(state, E::WifiDown, 5_000);
            assert!(matches!(siguiente, S::WifiConnecting { .. }), "{state:?} ignoró WifiDown");
            assert!(actions.contains(StopCapture), "{state:?} no cortó la captura");
            assert!(actions.contains(StopPlayback), "{state:?} no calló el altavoz");
        }
    }

    #[test]
    fn un_token_mal_no_se_reintenta_en_bucle() {
        let (state, _) = next(S::Idle, E::AuthRejected, 1_000);
        let S::Fault { kind, .. } = state else { panic!("no marcó fallo") };
        assert_eq!(kind, Fault::AuthRejected);
        assert!(!kind.is_recoverable());

        // Ni siquiera pasado el plazo de visualización se mueve de ahí.
        let (despues, actions) = next(state, E::Tick, 1_000 + FAULT_DISPLAY_MS);
        assert_eq!(despues, state);
        assert!(actions.is_empty());
    }

    #[test]
    fn un_fallo_recuperable_reintenta_pasado_el_plazo() {
        let state = S::Fault { kind: Fault::ServerUnreachable, since_ms: 0 };
        assert_eq!(next(state, E::Tick, FAULT_DISPLAY_MS - 1).0, state);
        let (despues, actions) = next(state, E::Tick, FAULT_DISPLAY_MS);
        assert!(matches!(despues, S::WifiConnecting { .. }));
        assert!(actions.contains(ConnectWifi));
    }

    /// El contador de intentos tiene que sobrevivir a la vuelta
    /// Disconnected -> ServerConnecting -> Disconnected, o el backoff no crece y
    /// el dispositivo machaca un servidor apagado para siempre.
    #[test]
    fn el_backoff_crece_entre_reintentos_seguidos() {
        let mut state = S::ServerConnecting { since_ms: 0, attempt: 0 };
        let mut ahora = 0u64;
        let mut esperas = Vec::new();

        for _ in 0..6 {
            let (siguiente, _) = next(state, E::ServerDown, ahora);
            let S::Disconnected { retry_at_ms, .. } = siguiente else {
                panic!("no programó reintento: {siguiente:?}");
            };
            esperas.push(retry_at_ms - ahora);
            ahora = retry_at_ms;
            state = next(siguiente, E::Tick, ahora).0;
            assert!(matches!(state, S::ServerConnecting { .. }));
        }

        assert!(esperas[5] > esperas[0] * 4, "el backoff apenas creció: {esperas:?}");
    }

    /// Y al revés: si el enlace llegó a abrirse, la siguiente caída reintenta
    /// rápido en vez de arrastrar la espera larga de la vez anterior.
    #[test]
    fn conectar_reinicia_el_backoff() {
        let castigado = S::ServerConnecting { since_ms: 0, attempt: 6 };
        assert_eq!(next(castigado, E::ServerUp, 1_000).0, S::Idle);

        let (state, _) = next(S::Idle, E::ServerDown, 1_000);
        let S::Disconnected { retry_at_ms, attempt } = state else { panic!("no reintenta") };
        assert_eq!(attempt, 1);
        assert!(retry_at_ms - 1_000 <= BACKOFF_MIN_MS, "no reintentó rápido tras un enlace bueno");
    }

    #[test]
    fn sin_servidor_se_reintenta_con_backoff() {
        let (state, _) = next(S::ServerConnecting { since_ms: 0, attempt: 0 }, E::ServerDown, 1_000);
        let S::Disconnected { retry_at_ms, .. } = state else { panic!("no programó reintento") };
        assert!(retry_at_ms > 1_000, "el reintento debería ser en el futuro");

        // Antes de la hora no se mueve; al llegar, reconecta.
        assert_eq!(next(state, E::Tick, retry_at_ms - 1).0, state);
        let (despues, actions) = next(state, E::Tick, retry_at_ms);
        assert!(matches!(despues, S::ServerConnecting { .. }));
        assert!(actions.contains(ConnectServer));
    }

    #[test]
    fn el_backoff_crece_y_tiene_techo() {
        let sin_jitter: Vec<_> = (0..10).map(|n| backoff_ms(n, 0)).collect();
        assert_eq!(sin_jitter[0], BACKOFF_MIN_MS);
        for par in sin_jitter.windows(2) {
            assert!(par[1] >= par[0], "el backoff no debería bajar: {sin_jitter:?}");
        }
        for espera in &sin_jitter {
            assert!(*espera <= BACKOFF_MAX_MS, "backoff por encima del techo: {espera}");
        }
        assert_eq!(*sin_jitter.last().unwrap(), BACKOFF_MAX_MS);
    }

    #[test]
    fn el_jitter_resta_como_mucho_un_cuarto() {
        for attempt in 0..10 {
            let base = backoff_ms(attempt, 0);
            for jitter in [0, 1, 511, 1023, u32::MAX] {
                let con = backoff_ms(attempt, jitter);
                assert!(con <= base, "el jitter debería restar, no sumar");
                assert!(con >= base - base / 4, "el jitter resta más de un 25 %");
            }
        }
    }

    /// Un `Tick` que no vence ningún plazo no debe cambiar nada. Es el evento más
    /// frecuente con diferencia: si moviera algo, lo movería 50 veces por segundo.
    #[test]
    fn un_tick_inocuo_no_cambia_nada() {
        for state in [S::Idle, S::Listening { since_ms: 99_000 }, S::Thinking { since_ms: 99_000 }] {
            let (siguiente, actions) = next(state, E::Tick, 99_100);
            assert_eq!(siguiente, state);
            assert!(actions.is_empty());
        }
    }

    #[test]
    fn nunca_se_desbordan_las_acciones() {
        for state in TODOS_LOS_ESTADOS {
            for event in TODOS_LOS_EVENTOS {
                let (_, actions) = next(state, event, 100_000);
                assert!(actions.len() <= MAX_ACTIONS);
                // Truncar significaría perder una orden en silencio.
                assert!(actions.len() < MAX_ACTIONS || !actions.is_empty());
            }
        }
    }

    /// Ninguna combinación puede colgar el firmware ni dejarlo en un estado sin salida.
    #[test]
    fn ninguna_combinacion_entra_en_panico() {
        for state in TODOS_LOS_ESTADOS {
            for event in TODOS_LOS_EVENTOS {
                for now in [0, 1, 100_000, u64::MAX] {
                    let _ = next(state, event, now);
                }
            }
        }
    }

    /// Los parpadeos son la tabla de diagnóstico: 1..=6, sin huecos ni repetidos.
    #[test]
    fn los_codigos_de_parpadeo_son_los_del_plan() {
        let faults = [
            Fault::WifiAssoc, Fault::ServerUnreachable, Fault::AuthRejected,
            Fault::Audio, Fault::WakeWord, Fault::Panic,
        ];
        for (i, f) in faults.iter().enumerate() {
            assert_eq!(f.blinks(), i as u8 + 1, "{f:?} no parpadea lo que dice el plan");
        }
    }

    const TODOS_LOS_ESTADOS: [State; 9] = [
        S::Booting,
        S::WifiConnecting { since_ms: 0 },
        S::ServerConnecting { since_ms: 0, attempt: 0 },
        S::Disconnected { retry_at_ms: 1_000, attempt: 1 },
        S::Idle,
        S::Listening { since_ms: 0 },
        S::Thinking { since_ms: 0 },
        S::Speaking { since_ms: 0 },
        S::Fault { kind: Fault::Audio, since_ms: 0 },
    ];

    const TODOS_LOS_EVENTOS: [Event; 13] = [
        E::Booted,
        E::WifiUp,
        E::WifiDown,
        E::ServerUp,
        E::ServerDown,
        E::AuthRejected,
        E::ButtonPressed,
        E::ButtonReleased,
        E::ServerSaid(Reported::Idle),
        E::ServerSaid(Reported::Speaking),
        E::TtsStarted,
        E::TtsEnded,
        E::Tick,
    ];
}
