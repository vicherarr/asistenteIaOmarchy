//! Satélite de voz de Luka — **binario incompleto (Fase 1 en curso)**.
//!
//! Ahora mismo solo hace la puesta en marcha del hardware: I²C, expansor (con el
//! amplificador apagado) y los dos codecs. Sirve para comprobar que la cadena de
//! inicialización funciona fuera de los spikes, y es el esqueleto sobre el que
//! van los cuatro módulos que faltan.
//!
//! Pendiente, por este orden (ver `PROGRESO.md`):
//!   1. `audio.rs` — hilo dueño del I²S, half-duplex.
//!   2. `net.rs`   — WiFi + cliente WebSocket con el certificado fijado.
//!   3. `ring.rs`  — hilo del anillo de LEDs a 50 fps.
//!   4. el supervisor que une `luka_state::next` con las acciones.
//!
//! # Seguridad
//!
//! Lo primero que se hace tras abrir el I²C es **forzar el amplificador a
//! apagado**, y no hay ninguna ruta que lo encienda todavía. Mientras el hilo de
//! audio no exista, este binario no puede producir sonido.

mod audio;
mod board;
mod detect;
mod net;
mod ring;
mod watchdog;

use anyhow::{Context, Result};
use esp_idf_hal::i2c::{I2cConfig, I2cDriver};
use esp_idf_hal::peripherals::Peripherals;
use esp_idf_hal::units::FromValueType;
use luka_board::{i2c as bi2c, PINOUT_VERIFIED};
use std::sync::{Arc, Mutex};
use std::sync::mpsc;
use std::time::Instant;
use esp_idf_hal::i2s::config::{
    Config, DataBitWidth, MclkMultiple, SlotMode, StdClkConfig, StdConfig, StdGpioConfig,
    StdSlotConfig,
};
use esp_idf_hal::i2s::I2sDriver;

const _: () = assert!(bi2c::SDA == 11 && bi2c::SCL == 10);
/// MCLK = 256 x 16 kHz = 4.096 MHz. Es la relación para la que están calculados
/// los coeficientes de reloj de AMBOS codecs; cambiarla obliga a recalcularlos.
const MCLK_MULTIPLE: MclkMultiple = MclkMultiple::M256;

fn main() -> Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    log::info!("luka-firmware {} arrancando", env!("CARGO_PKG_VERSION"));
    log::info!("config: {}", luka_config::summary());
    if !PINOUT_VERIFIED {
        log::warn!("El mapa de pines NO está verificado contra esta placa.");
    }

    let peripherals = Peripherals::take()?;

    let i2c = I2cDriver::new(
        peripherals.i2c0,
        peripherals.pins.gpio11,
        peripherals.pins.gpio10,
        &I2cConfig::new().baudrate(bi2c::FREQ_HZ.Hz()),
    )
    .context("abriendo el bus I2C")?;
    let i2c: board::SharedI2c = Arc::new(Mutex::new(i2c));

    {
        let mut guard = i2c.lock().expect("el mutex del I2C no debería estar envenenado");
        board::init_expander(&mut guard).context("inicializando el TCA9555")?;
        board::es7210_init(&mut guard).context("inicializando el ES7210")?;
        board::es8311_init(&mut guard).context("inicializando el ES8311")?;

        let botones = board::read_buttons(&mut guard).context("leyendo los botones")?;
        log::info!("Botones en reposo: {botones:?} (ninguno debería estar pulsado)");
    }
    
    let std_config = StdConfig::new(
        Config::default(),
        StdClkConfig::from_sample_rate_hz(luka_board::audio::SAMPLE_RATE_HZ)
            .mclk_multiple(MCLK_MULTIPLE),
        StdSlotConfig::philips_slot_default(DataBitWidth::Bits16, SlotMode::Stereo),
        StdGpioConfig::default(),
    );

    let mut i2s = I2sDriver::new_std_bidir(
        peripherals.i2s0,
        &std_config,
        peripherals.pins.gpio13,
        peripherals.pins.gpio15,
        peripherals.pins.gpio16,
        Some(peripherals.pins.gpio12),
        peripherals.pins.gpio14,
    )
    .context("no se pudo abrir el I2S")?;

    i2s.tx_enable().context("no se pudo habilitar el TX del I2S")?;
    i2s.rx_enable().context("no se pudo habilitar el RX del I2S")?;

    let driver = ws2812_esp32_rmt_driver::Ws2812Esp32Rmt::new(peripherals.rmt.channel0, peripherals.pins.gpio38)?;
    let shared_ring = Arc::new(ring::RingState::new());
    ring::spawn(driver, shared_ring.clone())?;

    // Los canales de audio necesitan fondo: a 16 kHz salen 50 tramas por segundo
    // y cualquier hipo momentáneo de la red llenaba los de 5 huecos que había
    // aquí, tirando el principio de las frases (se veían 89 descartes en un solo
    // turno). Estas capacidades son ~1 s de margen, que en RAM no es nada.
    let (audio_cmd_tx, audio_cmd_rx) = mpsc::sync_channel(8);
    let (net_cmd_tx, net_cmd_rx) = mpsc::sync_channel(64);
    let (event_tx, event_rx) = mpsc::sync_channel(32);

    let (capture_tx, capture_rx) = mpsc::sync_channel(64);
    let (playback_tx, playback_rx) = mpsc::sync_channel(64);

    // Estado de la reproducción, compartido por los tres hilos: el de audio
    // publica cuánto queda por sonar, el de red marca cuándo el servidor deja de
    // mandar, y el supervisor junta las dos cosas para cerrar el turno.
    let reproduccion = Arc::new(audio::Reproduccion::new());

    let audio_io = audio::AudioIO::new(
        audio_cmd_rx, capture_tx, playback_rx, i2s, i2c.clone(), reproduccion.clone(),
    );
    audio_io.spawn()?;

    let net_task = net::NetTask::new(
        net_cmd_rx, event_tx.clone(), playback_tx, reproduccion.clone(), peripherals.modem,
    );
    net_task.spawn()?;

    // El detector se pone EN MEDIO de audio y red: es quien decide, trama a
    // trama, si lo que llega del micro va al modelo de wake word o al servidor.
    // El supervisor ya no toca audio; solo le dice en qué modo está.
    let modo_detect = Arc::new(detect::ModoCompartido::new());
    let detect_task = detect::Detect::new(
        capture_rx,
        net_cmd_tx.clone(),
        event_tx.clone(),
        shared_ring.clone(),
        modo_detect.clone(),
    );
    detect_task.spawn()?;
    
    // Iniciar
    let _ = event_tx.try_send(luka_state::Event::Booted);
    
    // A partir de aquí el supervisor tiene que dar señales de vida cada 10 s o el
    // watchdog reinicia. Se suscribe DESPUÉS de arrancar los hilos, porque crear
    // los drivers puede tardar y no queremos reiniciar durante el arranque.
    watchdog::subscribe("supervisor");

    let start_time = Instant::now();
    let mut current_state = luka_state::State::Booting;
    
    let mut last_buttons = crate::board::Buttons::default();

    loop {
        watchdog::feed();
        let now_ms = start_time.elapsed().as_millis() as u64;
        
        let mut events = Vec::new();
        
        // Sondeo de botones (cada 20ms en teoría, aquí lo hacemos en el loop)
        if let Ok(mut guard) = i2c.lock() {
            if let Ok(botones) = board::read_buttons(&mut guard) {
                if botones.any() && !last_buttons.any() {
                    events.push(luka_state::Event::ButtonPressed);
                } else if !botones.any() && last_buttons.any() {
                    events.push(luka_state::Event::ButtonReleased);
                }
                last_buttons = botones;
            }
        }
        
        while let Ok(ev) = event_rx.try_recv() {
            events.push(ev);
        }

        // El turno de voz se acaba cuando NO QUEDA AUDIO QUE SONAR, no cuando el
        // servidor dice que dejó de mandar. Con el colchón el dispositivo va por
        // detrás, así que fiarse del `TTS_END` cortaría el final de cada
        // respuesta —ya pasaba antes, tirando segundos de audio ya recibido.
        if reproduccion.turno_agotado() {
            events.push(luka_state::Event::TtsEnded);
        }

        events.push(luka_state::Event::Tick);
        
        for event in events {
            let (next_state, actions) = luka_state::next(current_state, event, now_ms);
            
            if next_state != current_state {
                log::info!("State transition: {:?} -> {:?}", current_state, next_state);
                current_state = next_state;
                shared_ring.set_state(current_state, now_ms);

                // El modo del detector se DERIVA del estado en vez de mandarse
                // como acción: así no hay forma de que se queden desincronizados
                // (que es como se acaba escuchando con el altavoz abierto).
                modo_detect.set(match current_state {
                    luka_state::State::Idle => detect::Modo::Detectando,
                    luka_state::State::Listening { .. } => detect::Modo::Enviando,
                    // Ventana de seguimiento: el micro escucha, pero solo el
                    // nivel. Ni wake word ni envío a la red.
                    luka_state::State::FollowUp { .. } => detect::Modo::Esperando,
                    // Hablando se escucha solo si barge-in está compilado; con
                    // 0 se para el detector igual que antes de la Fase 4.
                    luka_state::State::Speaking { .. }
                        if luka_config::device::BARGE_IN != 0 =>
                    {
                        detect::Modo::Interrumpiendo
                    }
                    _ => detect::Modo::Parado,
                });
            }
            
            for action in actions.iter() {
                use luka_state::Action::*;
                match action {
                    ConnectWifi => { let _ = net_cmd_tx.try_send(net::NetCommand::ConnectWifi); }
                    ConnectServer => { let _ = net_cmd_tx.try_send(net::NetCommand::ConnectServer); }
                    DropServer => { let _ = net_cmd_tx.try_send(net::NetCommand::DropServer); }
                    StartCapture => { let _ = audio_cmd_tx.try_send(audio::AudioCommand::StartCapture); }
                    StopCapture => { let _ = audio_cmd_tx.try_send(audio::AudioCommand::StopCapture); }
                    SendEnd => { let _ = net_cmd_tx.try_send(net::NetCommand::SendEnd); }
                    SendCancel => { let _ = net_cmd_tx.try_send(net::NetCommand::SendCancel); }
                    StartPlayback => { let _ = audio_cmd_tx.try_send(audio::AudioCommand::StartPlayback); }
                    StopPlayback => { let _ = audio_cmd_tx.try_send(audio::AudioCommand::StopPlayback); }
                }
            }
        }

        std::thread::sleep(std::time::Duration::from_millis(20));
    }
}
