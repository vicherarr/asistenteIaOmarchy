//! Hilo dueño del I²S.
//!
//! El I²S de esta placa es **full-duplex sobre el mismo periférico** (el ES7210 y
//! el ES8311 comparten BCLK y WS), así que un solo hilo lo posee y hace lectura y
//! escritura en el mismo bucle. Repartirlo entre dos hilos es la vía rápida a los
//! *glitches* y a meter un `Mutex` en la ruta de tiempo real.
//!
//! # Half-duplex por seguridad, no por simplicidad
//!
//! El altavoz y los micros están a centímetros: si se abre el amplificador
//! mientras se captura, se realimenta y suelta un pitido ensordecedor en
//! segundos (pasó en la Fase 0 y hubo que desenchufar la placa a la fuerza).
//!
//! Por eso este hilo **nunca captura y reproduce a la vez**, y no se limita a
//! confiar en que la máquina de estados lo respete: al entrar en captura fuerza
//! el fin de la reproducción y apaga el amplificador él mismo. Son dos cerrojos
//! independientes sobre la misma puerta, a propósito.

use anyhow::{Context, Result};
use esp_idf_hal::delay::BLOCK;
use esp_idf_hal::i2s::{I2sBiDir, I2sDriver};
use luka_board::audio;
use std::collections::VecDeque;
use std::sync::mpsc::{Receiver, SyncSender};

/// Ganancia digital sobre lo capturado.
///
/// Modesta a propósito: la ganancia de verdad se aplica en el PGA del ES7210
/// (`board::MIC_GAIN`), que es analógica y no amplifica el ruido de cuantización.
/// Esto es solo el último empujón, y va con recorte para que un grito no dé la
/// vuelta al rango y suene a distorsión brutal en vez de a saturación.
const CAPTURE_GAIN: i32 = 2;

/// Tope del búfer de reproducción, en muestras (60 s a 16 kHz ≈ 1,9 MB).
///
/// Enorme a propósito. El servidor **no manda la voz en tiempo real**: la suelta
/// tan rápido como la sintetiza, así que una respuesta de 15 s llega en un par de
/// segundos mientras el altavoz solo puede consumirla a 16 kHz. Con el búfer de
/// 2 s que había, toda respuesta medianamente larga desbordaba y se oía a trozos.
///
/// Cabe porque `CONFIG_SPIRAM_USE_MALLOC` manda las asignaciones grandes a la
/// PSRAM, de la que hay 8 MB.
const PLAYBACK_MAX_SAMPLES: usize = audio::SAMPLE_RATE_HZ as usize * 60;

pub enum AudioCommand {
    StartCapture,
    StopCapture,
    StartPlayback,
    StopPlayback,
}

/// Una trama capturada, con su nivel ya calculado.
///
/// El nivel viaja con la trama porque el RMS se calcula aquí de todos modos —hay
/// que recorrer las muestras— y así el vúmetro del anillo sale gratis, sin que
/// nadie más tenga que volver a mirar el audio.
pub struct CapturedFrame {
    pub pcm: Vec<i16>,
    /// 0-255 en escala logarítmica, listo para el anillo.
    pub level: u8,
}

pub struct AudioIO {
    cmd_rx: Receiver<AudioCommand>,
    capture_tx: SyncSender<CapturedFrame>,
    playback_rx: Receiver<Vec<i16>>,
    i2s: I2sDriver<'static, I2sBiDir>,
    shared_i2c: crate::board::SharedI2c,
}

impl AudioIO {
    pub fn new(
        cmd_rx: Receiver<AudioCommand>,
        capture_tx: SyncSender<CapturedFrame>,
        playback_rx: Receiver<Vec<i16>>,
        i2s: I2sDriver<'static, I2sBiDir>,
        shared_i2c: crate::board::SharedI2c,
    ) -> Self {
        Self { cmd_rx, capture_tx, playback_rx, i2s, shared_i2c }
    }

    pub fn spawn(mut self) -> Result<()> {
        std::thread::Builder::new()
            .name("audio_io".into())
            .stack_size(8192)
            .spawn(move || self.run_loop())
            .context("no se pudo crear el hilo audio_io")?;
        Ok(())
    }

    /// Abre el amplificador. Para cerrarlo se usa siempre [`crate::board::silence`],
    /// que no puede fallar hacia arriba.
    fn open_amplifier(&self) {
        match self.shared_i2c.lock() {
            Ok(mut guard) => {
                if let Err(e) = crate::board::set_amplifier(&mut guard, true) {
                    log::error!("no se pudo abrir el amplificador: {e:#}");
                }
            }
            Err(_) => log::error!("mutex del I2C envenenado; no se abre el amplificador"),
        }
    }

    fn run_loop(&mut self) {
        // Estéreo: el ES7210 entrega dos canales y el ES8311 espera dos.
        let mut rx_bytes = vec![0u8; audio::FRAME_BYTES * 2];
        let mut tx_bytes = vec![0u8; audio::FRAME_BYTES * 2];
        let mut cola: VecDeque<i16> = VecDeque::with_capacity(PLAYBACK_MAX_SAMPLES);

        let mut capturing = false;
        let mut playing = false;

        // El amplificador arranca cerrado pase lo que pase.
        crate::board::silence(&self.shared_i2c);

        loop {
            while let Ok(cmd) = self.cmd_rx.try_recv() {
                match cmd {
                    AudioCommand::StartCapture => {
                        // Interlock: abrir el micro implica callar el altavoz,
                        // aquí y ahora, sin depender de que alguien haya mandado
                        // antes el StopPlayback.
                        if playing {
                            log::warn!("captura pedida con la reproducción abierta; se calla primero");
                        }
                        playing = false;
                        cola.clear();
                        crate::board::silence(&self.shared_i2c);
                        capturing = true;
                    }
                    AudioCommand::StopCapture => capturing = false,
                    AudioCommand::StartPlayback => {
                        if capturing {
                            // No debería pasar nunca: la máquina de estados lo
                            // impide y hay un test que lo fija. Si pasa, gana el
                            // silencio.
                            log::error!("reproducción pedida capturando; se ignora");
                        } else {
                            playing = true;
                            self.open_amplifier();
                        }
                    }
                    AudioCommand::StopPlayback => {
                        playing = false;
                        cola.clear();
                        crate::board::silence(&self.shared_i2c);
                    }
                }
            }

            // --- Bajada: rellenar la cola con lo que haya llegado por red ---
            while let Ok(pcm) = self.playback_rx.try_recv() {
                // Si aun así no cabe, se descarta lo NUEVO, no lo viejo. Tirar
                // audio ya encolado abriría un hueco en mitad de una frase que ya
                // está sonando; perder la cola de una respuesta kilométrica solo
                // la corta al final, que se entiende mucho mejor.
                if cola.len() + pcm.len() > PLAYBACK_MAX_SAMPLES {
                    log::warn!(
                        "búfer de reproducción lleno ({} s encolados); se corta el final",
                        cola.len() / audio::SAMPLE_RATE_HZ as usize
                    );
                    continue;
                }
                cola.extend(pcm);
            }

            // --- Escritura ---
            if playing {
                // Lo que falte se rellena con silencio: un hueco se oye como una
                // pausa; no escribir nada produce un chasquido por *underrun*.
                for i in 0..audio::FRAME_SAMPLES {
                    let sample = cola.pop_front().unwrap_or(0);
                    let bytes = sample.to_le_bytes();
                    // Mono -> estéreo: la misma muestra en los dos canales.
                    tx_bytes[i * 4..i * 4 + 2].copy_from_slice(&bytes);
                    tx_bytes[i * 4 + 2..i * 4 + 4].copy_from_slice(&bytes);
                }
                if let Err(e) = self.i2s.write_all(&tx_bytes, BLOCK) {
                    log::warn!("I2S: fallo escribiendo ({e:?})");
                }
            }

            // --- Lectura ---
            //
            // Se lee SIEMPRE, aunque no se esté capturando: el RX está activo y si
            // nadie vacía sus búferes DMA se desbordan y ensucian el log. Además
            // es lo que marca el ritmo del bucle, porque bloquea justo una trama.
            match self.i2s.read(&mut rx_bytes, BLOCK) {
                Ok(_) => {
                    if capturing {
                        let (pcm, level) = mono_and_level(&rx_bytes);
                        // Si el consumidor no da abasto se descarta la trama en vez
                        // de bloquear: bloquear aquí pararía también la
                        // reproducción, que comparte hilo.
                        if self.capture_tx.try_send(CapturedFrame { pcm, level }).is_err() {
                            log::warn!("trama de micro descartada: el canal va lleno");
                        }
                    }
                }
                Err(e) => {
                    log::warn!("I2S: fallo leyendo ({e:?})");
                    // Sin la lectura el bucle no tiene ritmo y giraría en vacío.
                    std::thread::sleep(std::time::Duration::from_millis(audio::FRAME_MS as u64));
                }
            }
        }
    }
}

/// Convierte la trama estéreo cruda a mono y calcula su nivel.
///
/// Se queda con el canal izquierdo en vez de promediar los dos micros: promediar
/// dos cápsulas separadas atenúa las frecuencias según de dónde venga la voz
/// (filtro de peine), y eso es justo lo que no conviene antes de un STT.
fn mono_and_level(rx_bytes: &[u8]) -> (Vec<i16>, u8) {
    let mut pcm = Vec::with_capacity(audio::FRAME_SAMPLES);
    let mut suma_cuadrados: u64 = 0;

    // 4 bytes por muestra estéreo; los 2 primeros son el canal izquierdo.
    for cuadro in rx_bytes.chunks_exact(4) {
        let izquierdo = i16::from_le_bytes([cuadro[0], cuadro[1]]) as i32;
        let amplificado = (izquierdo * CAPTURE_GAIN).clamp(i16::MIN as i32, i16::MAX as i32) as i16;
        suma_cuadrados += (amplificado as i64 * amplificado as i64) as u64;
        pcm.push(amplificado);
    }

    let rms = if pcm.is_empty() { 0 } else { (suma_cuadrados / pcm.len() as u64).isqrt() as u32 };
    (pcm, level_from_rms(rms))
}

/// Pasa un RMS lineal (0-32767) a los 0-255 logarítmicos que quiere el anillo.
///
/// Logarítmico porque el oído lo es y el habla vive muy abajo en la escala: en la
/// Fase 0 se midió sobre -45 dBFS, que en lineal es un 0,5 % del fondo de escala
/// y dejaría el vúmetro plano y aparentemente roto. La ventana útil se fija entre
/// -60 dBFS (silencio) y -6 dBFS (a tope).
fn level_from_rms(rms: u32) -> u8 {
    const MIN_DBFS: f32 = -60.0;
    const MAX_DBFS: f32 = -6.0;

    if rms == 0 {
        return 0;
    }
    let dbfs = 20.0 * (rms as f32 / 32767.0).log10();
    let normalizado = (dbfs - MIN_DBFS) / (MAX_DBFS - MIN_DBFS);
    (normalizado.clamp(0.0, 1.0) * 255.0) as u8
}
