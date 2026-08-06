//! El hilo que escucha: wake word, pre-roll y fin de turno por silencio.
//!
//! Se coloca **entre** el hilo de audio y la red, y es quien decide qué se hace
//! con cada trama del micro:
//!
//! ```text
//!   audio_io ──tramas──▶ detect ──┬── en reposo: al detector (y al anillo)
//!                                 └── en turno:  a la red, y vigilando el silencio
//! ```
//!
//! Está aparte del supervisor por una razón medible: la inferencia tarda del
//! orden de 10 ms y sale cada 30, así que en el bucle del supervisor se comería
//! el sondeo de botones y el watchdog. Aquí tiene su propio hilo y su propia
//! prioridad.
//!
//! # Por qué el pre-roll
//!
//! Cuando el detector dice "Luka", la palabra **ya se ha dicho**: el modelo
//! necesita oírla entera para reconocerla. Si el turno empezara a grabar en ese
//! instante, lo que llega al servidor empieza a mitad de la frase siguiente. Por
//! eso se guarda el último segundo de audio en un anillo y, al despertar, se
//! manda antes que nada.

use crate::audio::CapturedFrame;
use crate::net::NetCommand;
use crate::ring::RingState;
use esp_idf_hal::cpu::Core;
use esp_idf_hal::task::thread::ThreadSpawnConfiguration;
use luka_board::audio;
use luka_state::Event;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::mpsc::{Receiver, SyncSender};
use std::sync::Arc;

/// Segundos de audio que se guardan para mandarlos al despertar.
const PRE_ROLL_S: usize = 1;
const PRE_ROLL_FRAMES: usize = PRE_ROLL_S * 1000 / audio::FRAME_MS as usize;

/// Cuánto tiene que destacar la voz **sobre el ruido de la sala** para que
/// cuente como voz.
///
/// Antes esto era un nivel absoluto (40, unos -51 dBFS) y resultó ser frágil de
/// la peor manera: al subir el PGA del ES7210 al tope, el suelo de ruido de una
/// sala normal se puso por encima de 40 y **el detector de silencio dejó de
/// dispararse por completo**. Ningún turno se cerraba por callarse; todos
/// agotaban los 15 s de `LISTENING_TIMEOUT_MS`, así que entre que terminabas de
/// hablar y Luka empezaba a pensar pasaban quince segundos. No hubo ningún
/// error en el log: simplemente una condición que ya no se cumplía nunca.
///
/// Medirlo contra el suelo real de la sala lo hace inmune a eso: cambiar la
/// ganancia, el micro o la habitación mueve el suelo y el umbral se mueve con
/// él.
///
/// El nivel es logarítmico entre -60 y -6 dBFS, así que un punto son ~0,21 dB y
/// estos 40 puntos son **unos 8,5 dB por encima del ruido de fondo**: suficiente
/// para no confundir el silencio entre frases con voz, y poco para no exigir que
/// grites.
const MARGEN_VOZ: u8 = 40;

/// Suelo mínimo, por si la sala está tan callada que el piso medido es casi
/// cero: por debajo de esto no se baja, o cualquier roce cerraría el turno.
const SILENCIO_MINIMO: u8 = 25;

/// Techo del umbral. Una sala muy ruidosa no puede empujarlo tan arriba que
/// haga falta gritar para que el turno siga abierto: antes que eso, que se
/// cierre por el plazo de 15 s, que al menos es un comportamiento conocido.
const SILENCIO_MAXIMO: u8 = 140;

/// Cada cuántas tramas sube un punto el suelo estimado (~0,2 s con tramas de
/// 20 ms). Baja al instante y sube despacio a propósito: así una voz de fondo
/// no eleva el suelo de forma permanente —lo bajaría el primer hueco—, pero un
/// ventilador que arranca queda incorporado en unos segundos.
const PISO_SUBIDA_CADA: u32 = 10;

/// Cuánto silencio seguido cierra un turno de manos libres.
///
/// 1,2 s es el compromiso: más corto corta a quien piensa a media frase, más
/// largo hace que Luka parezca lenta en contestar.
const SILENCIO_MS: u64 = 600;
const SILENCIO_FRAMES: u32 = (SILENCIO_MS / audio::FRAME_MS as u64) as u32;

/// Confianza por encima de la cual se da por empezado un "intento": alguien ha
/// dicho algo que al detector le suena a "Luka", aunque no llegue al umbral.
///
/// El anillo de calibración son 7 LEDs, o sea ~36 unidades por LED: sirve para
/// ver que el detector reacciona, no para elegir un umbral. Esto es lo que da el
/// número exacto. 60 deja fuera el fondo de sala y el habla normal.
const CASI_MINIMO: u8 = 60;

/// Nivel por debajo del cual se considera que ya no hay voz, dado el suelo de
/// ruido medido en la sala.
fn umbral_silencio(piso_ruido: u8) -> u8 {
    piso_ruido.saturating_add(MARGEN_VOZ).clamp(SILENCIO_MINIMO, SILENCIO_MAXIMO)
}

/// Qué hace el hilo con las tramas que le llegan.
#[derive(Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Modo {
    /// Ni detectar ni enviar: el altavoz está sonando o no hay enlace. La
    /// captura sigue llegando (el I²S no para), pero se tira.
    Parado = 0,
    /// En reposo: se busca la palabra.
    Detectando = 1,
    /// En turno: las tramas van a la red y se vigila el silencio.
    Enviando = 2,
    /// Luka está hablando y se escucha por si la interrumpen.
    ///
    /// Se busca la palabra igual que en [`Modo::Detectando`], pero con el umbral
    /// de barge-in —más alto— y sin guardar pre-roll. Las tramas se tiran tras
    /// pasar por el detector: aquí el micro no va a ninguna parte.
    Interrumpiendo = 3,
}

/// Palanca compartida con el supervisor. Es un atómico y no un canal porque el
/// modo lo dicta el estado —no es un evento— y lo que importa es el valor
/// actual, no la historia.
pub struct ModoCompartido(AtomicU8);

impl ModoCompartido {
    pub fn new() -> Self {
        Self(AtomicU8::new(Modo::Parado as u8))
    }

    pub fn set(&self, modo: Modo) {
        self.0.store(modo as u8, Ordering::Relaxed);
    }

    fn get(&self) -> Modo {
        match self.0.load(Ordering::Relaxed) {
            1 => Modo::Detectando,
            2 => Modo::Enviando,
            3 => Modo::Interrumpiendo,
            _ => Modo::Parado,
        }
    }
}

pub struct Detect {
    capture_rx: Receiver<CapturedFrame>,
    net_tx: SyncSender<NetCommand>,
    event_tx: SyncSender<Event>,
    ring: Arc<RingState>,
    modo: Arc<ModoCompartido>,
}

impl Detect {
    pub fn new(
        capture_rx: Receiver<CapturedFrame>,
        net_tx: SyncSender<NetCommand>,
        event_tx: SyncSender<Event>,
        ring: Arc<RingState>,
        modo: Arc<ModoCompartido>,
    ) -> Self {
        Self { capture_rx, net_tx, event_tx, ring, modo }
    }

    /// Pila holgada: el intérprete de TFLite tiene pila propia de trabajo y
    /// quedarse corto aquí se manifiesta como un reinicio por desbordamiento,
    /// que no dice de quién es la culpa.
    const PILA: usize = 16384;

    /// Prioridad del hilo, **deliberadamente por debajo** de la de la red.
    ///
    /// El defecto de pthreads en el ESP-IDF es 5, que es exactamente la misma
    /// que la tarea del cliente WebSocket. Mientras el detector solo corría en
    /// reposo daba igual, pero al ponerlo a escuchar también durante la
    /// reproducción (barge-in), sus ráfagas de ~10 ms cada 30 dejaron a la red
    /// sin llegar a tiempo:
    ///
    /// ```text
    /// E websocket_client: Could not lock ws-client within 1000 timeout
    /// E transport_base: esp_tls_conn_read error, Connection reset by peer
    /// ```
    ///
    /// El enlace se caía a los ~25 s de cualquier respuesta larga, y desde
    /// fuera se oía como que Luka se cortaba a sí misma a media frase. Nada en
    /// ese log dice "CPU": parece un problema de red y no lo es.
    ///
    /// Con 4, cualquier trama que llegue expulsa a la inferencia. Reconocer la
    /// palabra unos milisegundos más tarde no lo nota nadie; perder el enlace,
    /// sí.
    const PRIORIDAD: u8 = 4;

    pub fn spawn(mut self) -> anyhow::Result<()> {
        // Segunda medida, independiente de la prioridad: anclar al núcleo 1. La
        // pila de WiFi y lwIP vive en el 0, así que la inferencia deja de
        // competir con ella en vez de solo perder la carrera educadamente.
        ThreadSpawnConfiguration {
            name: Some(c"detect"),
            stack_size: Self::PILA,
            priority: Self::PRIORIDAD,
            pin_to_core: Some(Core::Core1),
            ..Default::default()
        }
        .set()?;

        let spawned = std::thread::Builder::new()
            .name("detect".into())
            .stack_size(Self::PILA)
            .spawn(move || self.run_loop());

        // Restaurar SIEMPRE, aunque el spawn haya fallado: esta configuración es
        // global al proceso y la heredaría el siguiente hilo que se cree, que no
        // tiene por qué querer ni esta prioridad ni este núcleo.
        ThreadSpawnConfiguration::default().set()?;
        spawned?;
        Ok(())
    }

    fn run_loop(&mut self) {
        let mut pre_roll: VecDeque<Vec<i16>> = VecDeque::with_capacity(PRE_ROLL_FRAMES);
        let mut frames_en_silencio: u32 = 0;
        let mut modo_anterior = Modo::Parado;
        // Arranca alto para que la primera trama de reposo lo baje de golpe al
        // valor real, en vez de tardar en converger desde abajo.
        let mut piso_ruido: u8 = 255;
        let mut piso_subida: u32 = 0;

        #[cfg(feature = "wakeword")]
        let mut detector = self.abrir_detector();
        #[cfg(feature = "wakeword")]
        let mut politica = luka_wakeword::Politica::new(luka_config::device::WAKE_THRESHOLD);
        #[cfg(feature = "wakeword")]
        let mut probabilidades = [0u8; luka_wakeword::MAX_PROBABILIDADES];
        // Pico de confianza del intento en curso (solo en modo calibración).
        #[cfg(feature = "wakeword")]
        let mut pico_intento: u8 = 0;

        crate::watchdog::subscribe("detect");

        loop {
            crate::watchdog::feed();

            // Bloquea hasta que haya trama: el ritmo lo marca el micro, no un
            // `sleep`. Si el hilo de audio muriera, el watchdog lo cazaría.
            let Ok(frame) = self.capture_rx.recv() else {
                log::error!("el canal de captura se cerró; el detector se para");
                return;
            };

            let modo = self.modo.get();
            if modo != modo_anterior {
                // Cada cambio de modo limpia el estado acumulado: al volver de
                // hablar, el detector arrastraría el eco de la voz de Luka, y
                // el contador de silencio, el de la frase anterior.
                #[cfg(feature = "wakeword")]
                if let Some(d) = detector.as_mut() {
                    d.reset();
                    politica.reset();
                    pico_intento = 0;
                    // Escuchar con el altavoz sonando es más difícil, así que el
                    // listón sube. Se cambia aquí y no en cada trama porque el
                    // reset acaba de dejar la ventana limpia: es el único
                    // instante en el que cambiar el umbral no puede hacer que
                    // una media a medio llenar cruce el listón nuevo de golpe.
                    politica.set_umbral(match modo {
                        Modo::Interrumpiendo => luka_config::device::WAKE_THRESHOLD_BARGE,
                        _ => luka_config::device::WAKE_THRESHOLD,
                    });
                }
                frames_en_silencio = 0;
                if modo != Modo::Enviando {
                    pre_roll.clear();
                } else {
                    // Al abrir turno se dice con qué números se va a decidir el
                    // final. Sin esto, un umbral que ya no se cumple nunca no
                    // produce ningún error: solo turnos que tardan quince
                    // segundos en cerrarse, que es como se manifestó el fallo.
                    log::info!(
                        "turno abierto: piso de ruido {piso_ruido}, silencio por debajo de {}",
                        umbral_silencio(piso_ruido)
                    );
                }
                modo_anterior = modo;
            }

            match modo {
                Modo::Parado => {
                    pre_roll.clear();
                }

                Modo::Detectando => {
                    // El suelo de ruido se mide SOLO aquí: en reposo el altavoz
                    // está callado y, casi siempre, nadie está hablando. Medirlo
                    // durante el turno lo contaminaría con la propia voz que se
                    // intenta distinguir de él.
                    if frame.level < piso_ruido {
                        piso_ruido = frame.level;
                        piso_subida = 0;
                    } else {
                        piso_subida += 1;
                        if piso_subida >= PISO_SUBIDA_CADA {
                            piso_subida = 0;
                            piso_ruido = piso_ruido.saturating_add(1);
                        }
                    }

                    // El anillo en reposo no enseña el nivel del micro, así que
                    // aquí no se toca: lo que se enseña, si acaso, es la
                    // confianza del detector (modo calibración).
                    if pre_roll.len() == PRE_ROLL_FRAMES {
                        pre_roll.pop_front();
                    }

                    #[cfg(feature = "wakeword")]
                    if let Some(d) = detector.as_mut() {
                        for &p in d.procesar(&frame.pcm, &mut probabilidades) {
                            let desperto = politica.empujar(p);
                            let confianza = politica.confianza();
                            self.ring.set_confianza(confianza);

                            // Se sigue el pico de cada intento y se loguea cuando
                            // decae. Es lo que convierte "está sordo" en un número:
                            // dices "Luka" desde donde sueles, y el log te dice a
                            // cuánto llegó de verdad y cuánto le faltó al umbral.
                            // Se salta la inferencia que dispara —ahí la ventana
                            // ya está limpia y contarla como decaída pondría un
                            // "casi" fantasma delante de cada despertar— y todo
                            // el periodo de gracia, donde nada puede disparar.
                            if luka_config::device::WAKE_CALIBRATION
                                && !desperto
                                && !politica.calentando()
                                && !politica.en_refractario()
                            {
                                if confianza >= CASI_MINIMO {
                                    pico_intento = pico_intento.max(confianza);
                                } else if pico_intento > 0 {
                                    log::info!(
                                        "casi: pico {pico_intento} (umbral {})",
                                        politica.umbral()
                                    );
                                    pico_intento = 0;
                                }
                            }

                            if desperto {
                                pico_intento = 0;
                                log::info!(
                                    "wake word: confianza {} (umbral {})",
                                    politica.confianza_disparo(),
                                    politica.umbral()
                                );
                                let _ = self.event_tx.try_send(Event::WakeDetected);
                                // El turno empieza por el pre-roll: la palabra
                                // ya se dijo, y sin esto el servidor recibiría
                                // la frase empezada.
                                self.volcar_pre_roll(&mut pre_roll);
                            }
                        }
                    }

                    pre_roll.push_back(frame.pcm);
                }

                Modo::Interrumpiendo => {
                    // **Sin pre-roll, a propósito.** Durante la reproducción el
                    // anillo se habría llenado con la voz de Luka; volcárselo al
                    // servidor al interrumpir sería darle de comer su propia
                    // respuesta como si fuera la pregunta. El turno empieza
                    // limpio, ya con el altavoz callado.
                    #[cfg(feature = "wakeword")]
                    if let Some(d) = detector.as_mut() {
                        for &p in d.procesar(&frame.pcm, &mut probabilidades) {
                            if politica.empujar(p) {
                                let activo = luka_config::device::BARGE_IN >= 2;
                                log::info!(
                                    "barge-in: {} (confianza {}, umbral {})",
                                    if activo { "INTERRUMPE" } else { "solo medición, NO interrumpe" },
                                    politica.confianza_disparo(),
                                    politica.umbral()
                                );
                                pico_intento = 0;
                                if activo {
                                    let _ = self.event_tx.try_send(Event::WakeDetected);
                                }
                                continue;
                            }

                            // En modo medición se registra hasta dónde llega la
                            // confianza con Luka hablando. Casi siempre es su
                            // propio eco, y es justo el número que decide si
                            // WAKE_THRESHOLD_BARGE está bien puesto.
                            if luka_config::device::BARGE_IN == 1
                                && !politica.calentando()
                                && !politica.en_refractario()
                            {
                                let confianza = politica.confianza();
                                if confianza >= CASI_MINIMO {
                                    pico_intento = pico_intento.max(confianza);
                                } else if pico_intento > 0 {
                                    log::info!(
                                        "eco: pico {pico_intento} (umbral {})",
                                        politica.umbral()
                                    );
                                    pico_intento = 0;
                                }
                            }
                        }
                    }
                }

                Modo::Enviando => {
                    self.ring.set_level(frame.level);

                    if frame.level < umbral_silencio(piso_ruido) {
                        frames_en_silencio += 1;
                        if frames_en_silencio == SILENCIO_FRAMES {
                            // `==` y no `>=`: se manda una sola vez. Con `>=`
                            // el evento se repetiría 50 veces por segundo
                            // mientras durase el silencio.
                            let _ = self.event_tx.try_send(Event::SilenceDetected);
                        }
                    } else {
                        frames_en_silencio = 0;
                    }

                    if self.net_tx.try_send(NetCommand::SendAudio(frame.pcm)).is_err() {
                        log::warn!("trama de micro descartada: la red no da abasto");
                    }
                }
            }
        }
    }

    fn volcar_pre_roll(&self, pre_roll: &mut VecDeque<Vec<i16>>) {
        let tramas = pre_roll.len();
        for pcm in pre_roll.drain(..) {
            if self.net_tx.try_send(NetCommand::SendAudio(pcm)).is_err() {
                log::warn!("pre-roll incompleto: la red no da abasto");
                break;
            }
        }
        log::debug!("pre-roll enviado: {tramas} tramas");
    }

    /// Carga el modelo. Un fallo aquí no puede tirar el dispositivo: sin
    /// detector el botón sigue funcionando, que es exactamente lo que entregaba
    /// la Fase 1.
    #[cfg(feature = "wakeword")]
    fn abrir_detector(&self) -> Option<luka_wakeword::Detector> {
        match luka_wakeword::Detector::new(luka_wakeword::ARENA_POR_DEFECTO) {
            Some(d) => {
                log::info!("wake word activa (arena usada: {} B)", d.arena_usada());
                Some(d)
            }
            None => {
                log::error!("no se pudo cargar el modelo de wake word; solo botón");
                let _ = self.event_tx.try_send(Event::Faulted(luka_state::Fault::WakeWord));
                None
            }
        }
    }
}
