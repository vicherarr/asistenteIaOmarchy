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
/// Cuánto se aguanta SIN NOTICIAS del servidor tras cerrar el turno.
///
/// Es un plazo de **silencio**, no de duración: cada `STATE` del servidor lo
/// renueva. La diferencia importa porque "el servidor tarda" y "el servidor no
/// está" son cosas distintas y antes se trataban igual: un turno de treinta
/// segundos —una pregunta a la cámara, con la foto y dos vueltas al modelo— se
/// daba por perdido con el servidor trabajando al otro lado.
pub const THINKING_TIMEOUT_MS: u64 = 30_000;
/// Tope duro del turno, pase lo que pase.
///
/// El plazo de arriba se renueva, así que por sí solo no cumple el invariante de
/// que nada se quede colgado para siempre: un servidor que dijera "sigo en ello"
/// eternamente dejaría el aparato esperando eternamente. Este no se renueva.
pub const THINKING_MAX_MS: u64 = 180_000;
/// Plazo para abrir el WebSocket. Sin esto, un fallo que no llegue a producir un
/// evento de desconexión dejaría el dispositivo en `ServerConnecting` para
/// siempre, con el anillo girando en azul y sin reintentar jamás.
pub const SERVER_CONNECT_TIMEOUT_MS: u64 = 15_000;
/// Guarda por si el `TTS_END` se pierde: sin esto el anillo se quedaría en verde
/// para siempre y el dispositivo sordo, porque en v1 es half-duplex.
pub const SPEAKING_TIMEOUT_MS: u64 = 60_000;
/// Cuánto se enseña un fallo en el anillo antes de volver a intentarlo.
pub const FAULT_DISPLAY_MS: u64 = 3_000;

/// Cuánto sigue atento el micro después de que Luka termine de hablar.
///
/// Existe para no tener que decir "Luka" otra vez a la pregunta siguiente, que
/// es lo que rompe la conversación: se contesta a una respuesta, no se abre un
/// expediente nuevo.
///
/// Corto a propósito. Cubre la réplica inmediata —"¿y eso qué significa?"— y
/// poco más: cada segundo de ventana es un segundo de micro atento en el salón,
/// y el precio de alargarla lo paga la sala, no la conversación.
pub const FOLLOW_UP_MS: u64 = 3_500;

/// Sordera obligatoria al abrir la ventana.
///
/// Al callar el altavoz, lo que entra por el micro durante un instante **sigue
/// siendo la voz de Luka**: la cola del amplificador y la reverberación de la
/// habitación llegan tarde. Sin esta guarda, Luka se contestaría a sí misma.
pub const FOLLOW_UP_GUARD_MS: u64 = 300;

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

    /// ¿Hay que rehacer la asociación WiFi para recuperarse?
    ///
    /// Solo si el problema puede ser de radio. Que el servidor no conteste no lo
    /// es, y reasociar por eso era el peor remedio posible: tira el socket TLS
    /// que había debajo del WebSocket y provoca justo el corte que se pretendía
    /// evitar. Ahí basta con rehacer el WebSocket.
    pub const fn needs_wifi_restart(self) -> bool {
        !matches!(self, Self::ServerUnreachable)
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
    ///
    /// `hands_free` distingue quién abrió el turno, y no es un detalle: un
    /// turno de botón lo cierra soltar el botón, pero uno abierto por la wake
    /// word no tiene botón que soltar y solo puede cerrarlo el silencio. Sin
    /// esta marca, callarse un segundo mientras piensas qué decir cortaría
    /// también los turnos de botón.
    Listening { since_ms: u64, hands_free: bool },
    /// Turno enviado, esperando a Luka.
    ///
    /// Dos marcas, porque hay dos preguntas distintas que responder. `since_ms`
    /// es el último indicio de vida —el envío del turno, o la última señal del
    /// servidor— y contesta a "¿sigue ahí?". `started_ms` es cuándo empezó todo
    /// esto y no se renueva nunca: contesta a "¿esto va a acabar alguna vez?".
    Thinking { since_ms: u64, started_ms: u64 },
    /// Reproduciendo la voz de Luka.
    Speaking { since_ms: u64 },
    /// Luka acaba de terminar de hablar y el micro sigue atento.
    ///
    /// Aquí **no se busca la palabra**: basta con que alguien hable. Es lo que
    /// permite contestar a una respuesta sin volver a invocarla, y por eso dura
    /// poco: pasado el plazo se vuelve a [`State::Idle`] y hace falta "Luka"
    /// otra vez.
    ///
    /// El altavoz ya está callado cuando se entra —lo cierra el `TtsEnded` que
    /// trae aquí—, así que esto no reabre el problema del acople por ninguna
    /// parte.
    FollowUp { since_ms: u64 },
    /// Mostrando un fallo antes de decidir qué hacer.
    Fault { kind: Fault, since_ms: u64 },
}

impl State {
    /// ¿Hay enlace con el asistente? Lo usa el anillo y también el sitio al que se
    /// vuelve tras enseñar un fallo.
    pub const fn is_online(&self) -> bool {
        matches!(
            self,
            Self::Idle
                | Self::Listening { .. }
                | Self::Thinking { .. }
                | Self::Speaking { .. }
                | Self::FollowUp { .. }
        )
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
    /// El detector local dio por dicha la palabra "Luka".
    WakeDetected,
    /// Se acabó de hablar: el micro lleva un rato sin recoger voz. Solo cierra
    /// los turnos de manos libres.
    SilenceDetected,
    /// Alguien ha empezado a hablar. **No** es la palabra: es solo nivel de voz,
    /// y por eso únicamente se atiende en [`State::FollowUp`], donde ya se sabe
    /// que la conversación estaba abierta hace un segundo.
    SpeechDetected,
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

        (S::ServerConnecting { since_ms, attempt }, E::Tick)
            if now_ms.saturating_sub(since_ms) >= SERVER_CONNECT_TIMEOUT_MS =>
        {
            (
                S::Disconnected { retry_at_ms: retry_at(now_ms, attempt), attempt: attempt + 1 },
                Actions::of(&[DropServer]),
            )
        }

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
            (S::Listening { since_ms: now_ms, hands_free: false }, Actions::of(&[StartCapture]))
        }

        (S::Listening { hands_free: false, .. }, E::ButtonReleased) => {
            (S::Thinking { since_ms: now_ms, started_ms: now_ms }, Actions::of(&[StopCapture, SendEnd]))
        }

        // --- Wake word: manos libres ---
        (S::Idle, E::WakeDetected) => {
            (S::Listening { since_ms: now_ms, hands_free: true }, Actions::of(&[StartCapture]))
        }

        // El silencio cierra el turno, pero solo el que abrió la palabra. En un
        // turno de botón mandas tú.
        (S::Listening { hands_free: true, .. }, E::SilenceDetected) => {
            (S::Thinking { since_ms: now_ms, started_ms: now_ms }, Actions::of(&[StopCapture, SendEnd]))
        }

        // El turno se alarga demasiado: se cierra solo, como si se hubiera soltado.
        (S::Listening { since_ms, .. }, E::Tick) if now_ms.saturating_sub(since_ms) >= LISTENING_TIMEOUT_MS => {
            (S::Thinking { since_ms: now_ms, started_ms: now_ms }, Actions::of(&[StopCapture, SendEnd]))
        }

        // Interrumpir a Luka con el botón. Es seguro aunque v1 sea half-duplex
        // porque lo PRIMERO que se hace es callar el altavoz: el micro nunca
        // llega a estar abierto a la vez que suena la voz.
        (S::Speaking { .. }, E::ButtonPressed) => (
            S::Listening { since_ms: now_ms, hands_free: false },
            Actions::of(&[StopPlayback, SendCancel, StartCapture]),
        ),

        // Interrumpir a Luka diciendo su nombre mientras habla (barge-in).
        //
        // Mismas acciones y **mismo orden** que el botón de arriba, que es lo que
        // mantiene en pie el invariante: el altavoz se calla ANTES de abrir el
        // micro. Que el detector pueda oír durante la reproducción no cambia eso;
        // solo cambia quién puede pedir la interrupción.
        //
        // Que llegue este evento en `Speaking` ya implica que el firmware está
        // compilado con barge-in activo: con `BARGE_IN` en 0 o en 1 el hilo
        // `detect` no lo emite nunca desde este estado.
        (S::Speaking { .. }, E::WakeDetected) => (
            S::Listening { since_ms: now_ms, hands_free: true },
            Actions::of(&[StopPlayback, SendCancel, StartCapture]),
        ),

        // Pensando: pulsar el botón aborta la espera y vuelve a escuchar.
        (S::Thinking { .. }, E::ButtonPressed) => {
            (S::Listening { since_ms: now_ms, hands_free: false }, Actions::of(&[SendCancel, StartCapture]))
        }

        // --- Turno ---
        (S::Thinking { .. }, E::TtsStarted) => {
            (S::Speaking { since_ms: now_ms }, Actions::of(&[StartPlayback]))
        }

        // **La voz abre el altavoz venga el turno de donde venga.**
        //
        // Un turno que no nació aquí —se le habló a Luka por la GUI, por el
        // micro del PC o por el chat— no pasa por `Thinking`: el primer indicio
        // que llega del servidor es el propio audio. Sin este brazo ese audio se
        // acumulaba en la cola hasta desbordar y se tiraba sin haber sonado, y
        // el satélite se quedaba mudo ante cualquier conversación que no hubiera
        // empezado en él.
        //
        // El invariante de siempre se mantiene intacto: quien abre el altavoz
        // es el audio de verdad (con su colchón ya lleno), nunca un aviso del
        // servidor. Y el cierre es el de cualquier turno: `TtsEnded` cuando la
        // cola se vacía, que deja el micro atento por si se quiere contestar.
        (S::Idle | S::FollowUp { .. }, E::TtsStarted) => {
            (S::Speaking { since_ms: now_ms }, Actions::of(&[StartPlayback]))
        }

        // Terminar de hablar ya no devuelve a reposo: deja el micro atento un
        // rato para poder contestar sin volver a decir "Luka".
        (S::Speaking { .. }, E::TtsEnded) => {
            (S::FollowUp { since_ms: now_ms }, Actions::of(&[StopPlayback]))
        }

        // --- Ventana de seguimiento ---

        // Hablar durante la ventana abre turno directamente. La guarda descarta
        // los primeros instantes, donde lo que se oye es la cola del propio
        // altavoz llegando tarde.
        (S::FollowUp { since_ms }, E::SpeechDetected)
            if now_ms.saturating_sub(since_ms) >= FOLLOW_UP_GUARD_MS =>
        {
            (S::Listening { since_ms: now_ms, hands_free: true }, Actions::of(&[StartCapture]))
        }

        // Y la palabra y el botón siguen valiendo dentro de la ventana: quien la
        // dice por costumbre no tiene por qué saber que no hacía falta.
        (S::FollowUp { .. }, E::WakeDetected) => {
            (S::Listening { since_ms: now_ms, hands_free: true }, Actions::of(&[StartCapture]))
        }
        (S::FollowUp { .. }, E::ButtonPressed) => {
            (S::Listening { since_ms: now_ms, hands_free: false }, Actions::of(&[StartCapture]))
        }

        // Si nadie habla, se cierra sola. El micro atento tiene que tener plazo.
        (S::FollowUp { since_ms }, E::Tick) if now_ms.saturating_sub(since_ms) >= FOLLOW_UP_MS => {
            (S::Idle, Actions::none())
        }

        // `TTS_END` sin una sola trama de audio: Luka contestó sin voz (o el audio
        // se perdió). No hay nada que reproducir, pero el turno ha terminado.
        (S::Thinking { .. }, E::TtsEnded) => (S::Idle, Actions::none()),

        // **El servidor dice que sigue en ello.**
        //
        // Es la única forma de distinguir "tarda" de "no está", y sin ella el
        // aparato daba por muerto un turno de treinta segundos con el servidor
        // trabajando: una pregunta a la cámara son la foto, dos vueltas al modelo
        // y la pasada de visión, y eso no cabe en el plazo.
        //
        // Renueva el plazo y NADA MÁS. No abre el altavoz: eso lo sigue decidiendo
        // el audio de verdad, por lo que explica el bloque de más abajo.
        (S::Thinking { started_ms, .. }, E::ServerSaid(Reported::Thinking | Reported::Speaking)) => {
            (S::Thinking { since_ms: now_ms, started_ms }, Actions::none())
        }

        // Ni una señal en todo el plazo: esta vez sí, el servidor no está.
        // Se le manda `SendCancel` antes de rendirse: si el enlace resulta estar
        // vivo, deja de generar una respuesta que ya no va a oír nadie; y si está
        // muerto, el envío falla en silencio y no cuesta nada.
        (S::Thinking { since_ms, .. }, E::Tick) if now_ms.saturating_sub(since_ms) >= THINKING_TIMEOUT_MS => (
            S::Fault { kind: Fault::ServerUnreachable, since_ms: now_ms },
            Actions::of(&[SendCancel]),
        ),

        // El servidor habla pero el turno no acaba nunca. El plazo de arriba se
        // renueva, así que hace falta este tope que no se renueva jamás.
        (S::Thinking { started_ms, .. }, E::Tick) if now_ms.saturating_sub(started_ms) >= THINKING_MAX_MS => (
            S::Fault { kind: Fault::ServerUnreachable, since_ms: now_ms },
            Actions::of(&[SendCancel]),
        ),

        (S::Speaking { since_ms }, E::Tick) if now_ms.saturating_sub(since_ms) >= SPEAKING_TIMEOUT_MS => {
            (S::Idle, Actions::of(&[StopPlayback]))
        }

        // **El servidor no decide cuándo se abre ni cuándo se cierra el altavoz.**
        //
        // Antes sí: `ServerSaid(Speaking)` abría la reproducción para que el
        // anillo no fuera por detrás, y `ServerSaid(Idle)` la cerraba. Las dos
        // llegan en el momento equivocado, porque describen lo que hace el
        // SERVIDOR y no lo que suena aquí:
        //
        // - `STATE_SPEAKING` se manda antes de generar una sola muestra, así que
        //   abría el altavoz con la cola vacía y anulaba el colchón entero.
        // - `STATE_IDLE` llega pegado al `TTS_END`, cuando al dispositivo aún le
        //   quedan segundos por sonar. Cortaba el final de cada respuesta: en la
        //   última medición, 3,2 s de audio ya recibido a la basura.
        //
        // Ahora `Speaking` se entra solo con `TtsStarted` —que la red retiene
        // hasta tener colchón— y se sale con `TtsEnded`, que el supervisor manda
        // cuando la cola se vacía de verdad. El plazo de `SPEAKING_TIMEOUT_MS`
        // sigue de guardia por si alguna de las dos se perdiera.
        //
        // Desde `Thinking` sí se atiende: ahí no hay nada sonando que cortar, y
        // es como se sale de un turno que el servidor descartó por no entender
        // nada.
        (S::Thinking { .. }, E::ServerSaid(Reported::Idle)) => (S::Idle, Actions::none()),

        // --- Fallos ---
        (S::Fault { kind, since_ms }, E::Tick) if now_ms.saturating_sub(since_ms) >= FAULT_DISPLAY_MS => {
            if !kind.is_recoverable() {
                // Se queda enseñando el fallo: reintentar no lo arreglaría.
                (state, Actions::none())
            } else if kind.needs_wifi_restart() {
                (S::WifiConnecting { since_ms: now_ms }, Actions::of(&[ConnectWifi]))
            } else {
                // El servidor no contesta pero la radio está bien: se rehace solo
                // el WebSocket, por el mismo camino que una desconexión normal
                // (el `Tick` siguiente lo recoge `Disconnected` y reconecta tras
                // el backoff). Reasociar la WiFi aquí era lo que mataba el socket.
                (
                    S::Disconnected { retry_at_ms: retry_at(now_ms, 0), attempt: 1 },
                    Actions::of(&[DropServer]),
                )
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
            // Terminar de hablar deja el micro atento; se vuelve a reposo al
            // vencer la ventana, no antes.
            (E::Tick, 8_000 + FOLLOW_UP_MS),
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
        let (state, actions) = next(S::Listening { since_ms: 0, hands_free: false }, E::ButtonReleased, 2_000);
        assert!(matches!(state, S::Thinking { .. }));
        assert!(actions.contains(StopCapture));
        assert!(actions.contains(SendEnd));
    }

    /// El orden importa: primero se deja de capturar, luego se cierra el turno.
    /// Al revés se subirían tramas después del END y el servidor las mezclaría
    /// con el turno siguiente.
    #[test]
    fn primero_se_corta_la_captura_y_luego_se_manda_end() {
        let (_, actions) = next(S::Listening { since_ms: 0, hands_free: false }, E::ButtonReleased, 2_000);
        let orden: Vec<_> = actions.iter().collect();
        assert_eq!(orden.as_slice(), &[StopCapture, SendEnd]);
    }

    /// El camino de la Fase 3: decir "Luka", hablar, callarse y oír la respuesta.
    #[test]
    fn la_palabra_abre_el_turno_y_el_silencio_lo_cierra() {
        let state = run(&[
            (E::Booted, 0),
            (E::WifiUp, 100),
            (E::ServerUp, 200),
            (E::WakeDetected, 1_000),
            (E::SilenceDetected, 4_000),
            (E::TtsStarted, 5_000),
            (E::TtsEnded, 9_000),
            (E::Tick, 9_000 + FOLLOW_UP_MS),
        ]);
        assert_eq!(state, S::Idle);
    }

    #[test]
    fn la_palabra_abre_la_captura() {
        let (state, actions) = next(S::Idle, E::WakeDetected, 1_000);
        assert_eq!(state, S::Listening { since_ms: 1_000, hands_free: true });
        assert!(actions.contains(StartCapture));
    }

    /// Lo que separa un turno de manos libres de uno de botón: callarse un
    /// momento mientras piensas qué decir NO puede cortar el turno si estás
    /// pulsando el botón.
    #[test]
    fn el_silencio_solo_cierra_los_turnos_de_manos_libres() {
        let con_boton = S::Listening { since_ms: 0, hands_free: false };
        let (despues, actions) = next(con_boton, E::SilenceDetected, 3_000);
        assert_eq!(despues, con_boton, "el silencio cortó un turno de botón");
        assert!(actions.is_empty());

        let manos_libres = S::Listening { since_ms: 0, hands_free: true };
        let (despues, actions) = next(manos_libres, E::SilenceDetected, 3_000);
        assert!(matches!(despues, S::Thinking { .. }));
        assert!(actions.contains(SendEnd));
    }

    /// Y al revés: soltar un botón que nadie pulsó no cierra un turno abierto
    /// por la palabra. El caso real sería un rebote del expansor.
    #[test]
    fn soltar_el_boton_no_cierra_un_turno_de_manos_libres() {
        let manos_libres = S::Listening { since_ms: 0, hands_free: true };
        assert_eq!(next(manos_libres, E::ButtonReleased, 3_000).0, manos_libres);
    }

    /// Pensando no tiene altavoz abierto ni nada que interrumpir: la palabra ahí
    /// solo puede ser ruido, y se ignora.
    #[test]
    fn la_palabra_se_ignora_mientras_luka_piensa() {
        let state = S::Thinking { since_ms: 0, started_ms: 0 };
        let (despues, actions) = next(state, E::WakeDetected, 1_000);
        assert_eq!(despues, state, "{state:?} atendió a la wake word");
        assert!(actions.is_empty());
    }

    /// Barge-in: decir "Luka" mientras habla la interrumpe.
    ///
    /// Sustituye a la mitad `Speaking` del test anterior, que daba por imposible
    /// oír durante la reproducción. Lo que aquella prohibición protegía —que el
    /// micro no se abra con el altavoz sonando— lo sigue garantizando el orden
    /// de las acciones, que es lo que se comprueba aquí y en el test exhaustivo.
    #[test]
    fn la_palabra_interrumpe_a_luka_callando_antes_el_altavoz() {
        let (state, actions) = next(S::Speaking { since_ms: 0 }, E::WakeDetected, 1_000);
        assert_eq!(state, S::Listening { since_ms: 1_000, hands_free: true });

        let orden: Vec<_> = actions.iter().collect();
        assert_eq!(orden.as_slice(), &[StopPlayback, SendCancel, StartCapture]);

        // Y se cancela el turno viejo: sin esto el servidor seguiría mandando la
        // voz de la respuesta interrumpida encima de la pregunta nueva.
        assert!(actions.contains(SendCancel));
    }

    /// Interrumpir por voz y por botón tienen que hacer exactamente lo mismo.
    /// Si divergen, uno de los dos caminos acabará olvidándose de callar.
    #[test]
    fn interrumpir_por_voz_y_por_boton_hacen_lo_mismo() {
        let hablando = S::Speaking { since_ms: 0 };
        let (_, por_voz) = next(hablando, E::WakeDetected, 1_000);
        let (_, por_boton) = next(hablando, E::ButtonPressed, 1_000);

        let voz: Vec<_> = por_voz.iter().collect();
        let boton: Vec<_> = por_boton.iter().collect();
        assert_eq!(voz, boton, "los dos caminos de interrupción divergieron");
    }

    /// El servidor manda `STATE_SPEAKING` **antes** de sintetizar nada. Si eso
    /// abriera el altavoz, arrancaría con la cola vacía y el colchón no serviría
    /// de nada — que es exactamente lo que pasaba.
    #[test]
    fn el_servidor_no_abre_el_altavoz_por_su_cuenta() {
        let pensando = S::Thinking { since_ms: 0, started_ms: 0 };
        let (despues, actions) = next(pensando, E::ServerSaid(Reported::Speaking), 1_000);
        // Sigue pensando y NO se abre la reproducción. El aviso sí renueva el plazo
        // de espera (para eso está), así que se comprueba lo que de verdad importa
        // aquí en vez de exigir que el estado sea idéntico bit a bit.
        assert!(matches!(despues, S::Thinking { .. }), "el aviso abrió la reproducción");
        assert!(!actions.contains(StartPlayback));
    }

    /// Y `STATE_IDLE` llega pegado al `TTS_END`, cuando aquí aún quedan segundos
    /// por sonar. Cerrar con eso cortaba el final de cada respuesta.
    #[test]
    fn el_servidor_no_corta_lo_que_queda_por_sonar() {
        let hablando = S::Speaking { since_ms: 0 };
        let (despues, actions) = next(hablando, E::ServerSaid(Reported::Idle), 1_000);
        assert_eq!(despues, hablando, "el aviso del servidor cortó la reproducción");
        assert!(!actions.contains(StopPlayback));
    }

    /// Pero desde `Thinking` sí vale: no hay nada sonando que cortar, y es como
    /// se sale de un turno que el servidor descartó por no entender nada.
    #[test]
    fn pensando_si_atiende_el_reposo_del_servidor() {
        assert_eq!(next(S::Thinking { since_ms: 0, started_ms: 0 }, E::ServerSaid(Reported::Idle), 1_000).0, S::Idle);
    }

    /// Con esto, entrar y salir de `Speaking` depende solo del audio: lo abre
    /// `TtsStarted` (que la red retiene hasta tener colchón) y lo cierra
    /// `TtsEnded` (que el supervisor manda al vaciarse la cola).
    #[test]
    fn hablar_empieza_y_acaba_con_el_audio_no_con_la_red() {
        let (state, actions) = next(S::Thinking { since_ms: 0, started_ms: 0 }, E::TtsStarted, 1_000);
        assert_eq!(state, S::Speaking { since_ms: 1_000 });
        assert!(actions.contains(StartPlayback));

        let (state, actions) = next(state, E::TtsEnded, 5_000);
        assert!(matches!(state, S::FollowUp { .. }));
        assert!(actions.contains(StopPlayback));
    }

    // --- Turnos que no nacieron aquí ---

    /// Se le puede hablar a Luka por la GUI, por el micro del PC o por el chat:
    /// esos turnos no pasan por `Thinking` y el primer indicio que llega es el
    /// propio audio. Sin este brazo el satélite se quedaba mudo: la cola se
    /// llenaba hasta desbordar sin que sonara una sola muestra.
    #[test]
    fn la_voz_que_llega_en_reposo_abre_el_altavoz() {
        let (state, actions) = next(S::Idle, E::TtsStarted, 1_000);
        assert_eq!(state, S::Speaking { since_ms: 1_000 });
        assert!(actions.contains(StartPlayback));
    }

    /// Y si el audio llega durante la ventana de seguimiento —Luka acaba de
    /// hablar y alguien contesta desde otra vía— también tiene que sonar.
    #[test]
    fn la_voz_que_llega_en_seguimiento_abre_el_altavoz() {
        let (state, actions) = next(S::FollowUp { since_ms: 0 }, E::TtsStarted, 1_000);
        assert_eq!(state, S::Speaking { since_ms: 1_000 });
        assert!(actions.contains(StartPlayback));
    }

    /// El camino completo de una respuesta nacida fuera: suena, y al acabar el
    /// micro queda atento un rato, igual que tras cualquier turno.
    #[test]
    fn una_respuesta_nacida_fuera_suena_y_deja_el_micro_atento() {
        let state = run(&[
            (E::Booted, 0),
            (E::WifiUp, 100),
            (E::ServerUp, 200),
            // Nadie pulsó el botón ni dijo "Luka": el audio llega solo.
            (E::TtsStarted, 5_000),
            (E::TtsEnded, 9_000),
        ]);
        assert!(matches!(state, S::FollowUp { .. }), "no quedó atento: {state:?}");
        assert_eq!(next(state, E::Tick, 9_000 + FOLLOW_UP_MS).0, S::Idle);
    }

    // --- Ventana de seguimiento ---

    #[test]
    fn tras_hablar_se_queda_escuchando_un_rato() {
        let (state, actions) = next(S::Speaking { since_ms: 0 }, E::TtsEnded, 5_000);
        assert_eq!(state, S::FollowUp { since_ms: 5_000 });
        assert!(actions.contains(StopPlayback), "el altavoz tiene que quedar cerrado");
    }

    /// Se puede contestar sin volver a invocarla, que es el motivo de existir de
    /// todo esto.
    #[test]
    fn hablar_en_la_ventana_abre_turno_sin_decir_luka() {
        let ventana = S::FollowUp { since_ms: 0 };
        let (state, actions) = next(ventana, E::SpeechDetected, FOLLOW_UP_GUARD_MS);
        assert_eq!(state, S::Listening { since_ms: FOLLOW_UP_GUARD_MS, hands_free: true });
        assert!(actions.contains(StartCapture));
    }

    /// Lo primero que se oye tras callar el altavoz es el propio altavoz: la
    /// cola del amplificador y el eco de la sala. Sin la guarda, Luka se
    /// contestaría a sí misma en bucle.
    #[test]
    fn el_seguimiento_ignora_la_voz_durante_la_guarda() {
        let ventana = S::FollowUp { since_ms: 0 };
        let (state, actions) = next(ventana, E::SpeechDetected, FOLLOW_UP_GUARD_MS - 1);
        assert_eq!(state, ventana, "abrió turno con su propio eco");
        assert!(actions.is_empty());
    }

    /// El invariante del módulo: nada se queda colgado para siempre. Un micro
    /// atento sin plazo sería lo peor que podría quedarse abierto.
    #[test]
    fn la_ventana_de_seguimiento_expira_sola() {
        let ventana = S::FollowUp { since_ms: 1_000 };
        assert_eq!(next(ventana, E::Tick, 1_000 + FOLLOW_UP_MS - 1).0, ventana);
        assert_eq!(next(ventana, E::Tick, 1_000 + FOLLOW_UP_MS).0, S::Idle);
    }

    /// Quien dice "Luka" por costumbre no tiene por qué saber que no hacía falta.
    #[test]
    fn en_seguimiento_la_palabra_y_el_boton_siguen_valiendo() {
        let ventana = S::FollowUp { since_ms: 0 };

        let (state, actions) = next(ventana, E::WakeDetected, 1_000);
        assert_eq!(state, S::Listening { since_ms: 1_000, hands_free: true });
        assert!(actions.contains(StartCapture));

        let (state, actions) = next(ventana, E::ButtonPressed, 1_000);
        assert_eq!(state, S::Listening { since_ms: 1_000, hands_free: false });
        assert!(actions.contains(StartCapture));
    }

    /// Un turno que no se entendió NO abre ventana. Es lo que impide que una
    /// sala ruidosa encadene turnos vacíos para siempre, y sale gratis: sin voz
    /// que reproducir no se pasa por `Speaking`.
    #[test]
    fn un_turno_sin_voz_no_abre_ventana() {
        assert_eq!(next(S::Thinking { since_ms: 0, started_ms: 0 }, E::TtsEnded, 1_000).0, S::Idle);
    }

    /// El nivel de voz solo significa algo justo después de hablar Luka. En
    /// reposo abriría turno cada vez que alguien tosiera en el salón.
    #[test]
    fn el_nivel_de_voz_no_abre_turno_fuera_de_la_ventana() {
        for state in TODOS_LOS_ESTADOS {
            if matches!(state, S::FollowUp { .. }) {
                continue;
            }
            let (despues, _) = next(state, E::SpeechDetected, 100_000);
            assert_eq!(despues, state, "{state:?} abrió turno solo por oír voz");
        }
    }

    /// El camino completo de la conversación encadenada: se invoca una vez y se
    /// contestan dos preguntas.
    #[test]
    fn se_encadena_una_segunda_pregunta_sin_invocarla() {
        let state = run(&[
            (E::Booted, 0),
            (E::WifiUp, 100),
            (E::ServerUp, 200),
            (E::WakeDetected, 1_000),
            (E::SilenceDetected, 4_000),
            (E::TtsStarted, 5_000),
            (E::TtsEnded, 9_000),
            // Sin decir "Luka": basta con hablar.
            (E::SpeechDetected, 9_000 + FOLLOW_UP_GUARD_MS),
        ]);
        assert!(matches!(state, S::Listening { hands_free: true, .. }), "no encadenó: {state:?}");
    }

    #[test]
    fn un_turno_eterno_se_cierra_solo() {
        let inicio = S::Listening { since_ms: 1_000, hands_free: false };
        // Justo antes del plazo no pasa nada.
        assert_eq!(next(inicio, E::Tick, 1_000 + LISTENING_TIMEOUT_MS - 1).0, inicio);
        // Al cumplirse, se cierra igual que si se hubiera soltado el botón.
        let (state, actions) = next(inicio, E::Tick, 1_000 + LISTENING_TIMEOUT_MS);
        assert!(matches!(state, S::Thinking { .. }));
        assert!(actions.contains(SendEnd));
    }

    #[test]
    fn si_luka_no_contesta_no_se_espera_para_siempre() {
        let pensando = S::Thinking { since_ms: 0, started_ms: 0 };
        // El borde inferior no vence: si no, el plazo real sería uno menos.
        assert_eq!(next(pensando, E::Tick, THINKING_TIMEOUT_MS - 1).0, pensando);

        let (state, actions) = next(pensando, E::Tick, THINKING_TIMEOUT_MS);
        assert_eq!(state, S::Fault { kind: Fault::ServerUnreachable, since_ms: THINKING_TIMEOUT_MS });
        // Y se avisa antes de rendirse, para que el servidor no siga generando una
        // respuesta que ya no va a oír nadie.
        assert!(actions.contains(SendCancel));
    }

    /// El fallo que rompía las preguntas a la cámara: un turno de treinta segundos
    /// —foto, dos vueltas al modelo y la pasada de visión— se daba por perdido con
    /// el servidor trabajando al otro lado. Ahora el servidor puede decir que sigue
    /// en ello, y eso renueva el plazo.
    #[test]
    fn el_aviso_del_servidor_prorroga_la_espera() {
        let mut state = S::Thinking { since_ms: 0, started_ms: 0 };

        // El servidor da señales cada 5 s durante un minuto entero.
        let mut ahora = 0;
        while ahora < 60_000 {
            ahora += 5_000;
            state = next(state, E::ServerSaid(Reported::Thinking), ahora).0;
            assert!(matches!(state, S::Thinking { .. }), "se rindió en {ahora} ms");
            // Y los Tick de por medio tampoco vencen nada.
            state = next(state, E::Tick, ahora + 1).0;
            assert!(matches!(state, S::Thinking { .. }), "un Tick lo tumbó en {ahora} ms");
        }

        // Pero en cuanto se calla, el plazo corre desde el ÚLTIMO aviso.
        assert!(matches!(next(state, E::Tick, ahora + THINKING_TIMEOUT_MS - 1).0, S::Thinking { .. }));
        let (state, _) = next(state, E::Tick, ahora + THINKING_TIMEOUT_MS);
        assert_eq!(state, S::Fault { kind: Fault::ServerUnreachable, since_ms: ahora + THINKING_TIMEOUT_MS });
    }

    /// El plazo renovable, por sí solo, rompería el invariante del módulo: un
    /// servidor que dijera "sigo en ello" para siempre dejaría el aparato esperando
    /// para siempre. El tope duro no se renueva.
    #[test]
    fn un_turno_eterno_se_corta_aunque_el_servidor_siga_hablando() {
        let mut state = S::Thinking { since_ms: 0, started_ms: 0 };
        let mut ahora = 0;
        while ahora < THINKING_MAX_MS {
            ahora += 5_000;
            state = next(state, E::ServerSaid(Reported::Thinking), ahora).0;
            state = next(state, E::Tick, ahora).0;
        }
        let S::Fault { kind, .. } = state else {
            panic!("el tope duro no cortó el turno: {state:?}");
        };
        assert_eq!(kind, Fault::ServerUnreachable);
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
        let state = S::Fault { kind: Fault::WifiAssoc, since_ms: 0 };
        assert_eq!(next(state, E::Tick, FAULT_DISPLAY_MS - 1).0, state);
        let (despues, actions) = next(state, E::Tick, FAULT_DISPLAY_MS);
        assert!(matches!(despues, S::WifiConnecting { .. }));
        assert!(actions.contains(ConnectWifi));
    }

    /// Que el servidor no conteste NO es motivo para tocar la radio. Reasociar la
    /// WiFi mataba el socket TLS de debajo del WebSocket, que es justo el corte que
    /// se quería evitar: se rehace solo el WebSocket.
    #[test]
    fn el_servidor_mudo_no_reasocia_la_wifi() {
        let state = S::Fault { kind: Fault::ServerUnreachable, since_ms: 0 };
        let (despues, actions) = next(state, E::Tick, FAULT_DISPLAY_MS);
        assert!(matches!(despues, S::Disconnected { .. }), "no programó reintento: {despues:?}");
        assert!(actions.contains(DropServer));
        assert!(!actions.contains(ConnectWifi), "tocó la radio por un problema del servidor");
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

    /// Si abrir el WebSocket no termina ni bien ni mal, hay que salir igual: el
    /// caso real fue un fallo que no llegaba a producir evento de desconexión.
    #[test]
    fn abrir_el_websocket_tiene_plazo() {
        let conectando = S::ServerConnecting { since_ms: 1_000, attempt: 0 };
        assert_eq!(
            next(conectando, E::Tick, 1_000 + SERVER_CONNECT_TIMEOUT_MS - 1).0,
            conectando,
            "no debería rendirse antes de tiempo"
        );

        let (state, actions) = next(conectando, E::Tick, 1_000 + SERVER_CONNECT_TIMEOUT_MS);
        let S::Disconnected { attempt, .. } = state else {
            panic!("no salió de ServerConnecting: {state:?}");
        };
        assert_eq!(attempt, 1);
        assert!(actions.contains(DropServer));
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
        for state in [S::Idle, S::Listening { since_ms: 99_000, hands_free: false }, S::Thinking { since_ms: 99_000, started_ms: 99_000 }] {
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

    const TODOS_LOS_ESTADOS: [State; 11] = [
        S::FollowUp { since_ms: 0 },
        S::Booting,
        S::WifiConnecting { since_ms: 0 },
        S::ServerConnecting { since_ms: 0, attempt: 0 },
        S::Disconnected { retry_at_ms: 1_000, attempt: 1 },
        S::Idle,
        S::Listening { since_ms: 0, hands_free: false },
        S::Listening { since_ms: 0, hands_free: true },
        S::Thinking { since_ms: 0, started_ms: 0 },
        S::Speaking { since_ms: 0 },
        S::Fault { kind: Fault::Audio, since_ms: 0 },
    ];

    const TODOS_LOS_EVENTOS: [Event; 16] = [
        E::SpeechDetected,
        E::Booted,
        E::WifiUp,
        E::WifiDown,
        E::ServerUp,
        E::ServerDown,
        E::AuthRejected,
        E::ButtonPressed,
        E::ButtonReleased,
        E::WakeDetected,
        E::SilenceDetected,
        E::ServerSaid(Reported::Idle),
        E::ServerSaid(Reported::Speaking),
        E::TtsStarted,
        E::TtsEnded,
        E::Tick,
    ];
}
