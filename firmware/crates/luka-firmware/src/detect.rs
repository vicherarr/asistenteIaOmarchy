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
use luka_board::audio;
use luka_state::Event;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::mpsc::{Receiver, SyncSender};
use std::sync::Arc;

/// Segundos de audio que se guardan para mandarlos al despertar.
const PRE_ROLL_S: usize = 1;
const PRE_ROLL_FRAMES: usize = PRE_ROLL_S * 1000 / audio::FRAME_MS as usize;

/// Nivel (0-255, el mismo que alimenta el vúmetro) por debajo del cual se
/// considera que no hay voz.
///
/// El nivel es logarítmico entre -60 y -6 dBFS, así que 40 son unos -51 dBFS:
/// por encima del ruido de sala de estos micros y por debajo de cualquiera
/// hablando, aunque sea desde lejos.
const SILENCIO_NIVEL: u8 = 40;

/// Cuánto silencio seguido cierra un turno de manos libres.
///
/// 1,2 s es el compromiso: más corto corta a quien piensa a media frase, más
/// largo hace que Luka parezca lenta en contestar.
const SILENCIO_MS: u64 = 1_200;
const SILENCIO_FRAMES: u32 = (SILENCIO_MS / audio::FRAME_MS as u64) as u32;

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

    pub fn spawn(mut self) -> anyhow::Result<()> {
        std::thread::Builder::new()
            .name("detect".into())
            // Holgada: el intérprete de TFLite tiene pila propia de trabajo y
            // quedarse corto aquí se manifiesta como un reinicio por
            // desbordamiento, que no dice de quién es la culpa.
            .stack_size(16384)
            .spawn(move || self.run_loop())?;
        Ok(())
    }

    fn run_loop(&mut self) {
        let mut pre_roll: VecDeque<Vec<i16>> = VecDeque::with_capacity(PRE_ROLL_FRAMES);
        let mut frames_en_silencio: u32 = 0;
        let mut modo_anterior = Modo::Parado;

        #[cfg(feature = "wakeword")]
        let mut detector = self.abrir_detector();
        #[cfg(feature = "wakeword")]
        let mut politica = luka_wakeword::Politica::new(luka_config::device::WAKE_THRESHOLD);
        #[cfg(feature = "wakeword")]
        let mut probabilidades = [0u8; luka_wakeword::MAX_PROBABILIDADES];

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
                }
                frames_en_silencio = 0;
                if modo != Modo::Enviando {
                    pre_roll.clear();
                }
                modo_anterior = modo;
            }

            match modo {
                Modo::Parado => {
                    pre_roll.clear();
                }

                Modo::Detectando => {
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
                            self.ring.set_confianza(politica.confianza());
                            if desperto {
                                log::info!("wake word: confianza {}", politica.confianza());
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

                Modo::Enviando => {
                    self.ring.set_level(frame.level);

                    if frame.level < SILENCIO_NIVEL {
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
