use luka_board::leds;
use luka_state::{Fault, State};
use smart_leds::{SmartLedsWrite, RGB8};
use std::sync::atomic::{AtomicU32, AtomicU8, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use ws2812_esp32_rmt_driver::Ws2812Esp32Rmt;

/// Frecuencia de actualización del anillo (50 fps = 20 ms).
const FPS_INTERVAL: Duration = Duration::from_millis(20);

pub struct RingState {
    state: AtomicU32,
    fault: AtomicU32,
    /// Milisegundos, en `u32` porque **el Xtensa no tiene atómicos de 64 bits**.
    /// Se da la vuelta a los 49 días de encendido; lo único que provoca es un
    /// salto en una animación, así que no compensa un mutex para evitarlo.
    since_ms: AtomicU32,
    level: AtomicU8,
    brightness: AtomicU8,
}

impl RingState {
    pub fn new() -> Self {
        Self {
            state: AtomicU32::new(encode_state(State::Booting)),
            fault: AtomicU32::new(0),
            since_ms: AtomicU32::new(0),
            level: AtomicU8::new(0),
            // Brillo de `cfg.toml`; `set_brightness` puede cambiarlo en caliente.
            brightness: AtomicU8::new(luka_config::device::LED_BRIGHTNESS),
        }
    }

    pub fn set_state(&self, state: State, since_ms: u64) {
        self.state.store(encode_state(state), Ordering::Relaxed);
        if let State::Fault { kind, .. } = state {
            self.fault.store(kind as u32, Ordering::Relaxed);
        }
        self.since_ms.store(since_ms as u32, Ordering::Relaxed);
        self.level.store(0, Ordering::Relaxed);
    }

    pub fn set_level(&self, level: u8) {
        self.level.store(level, Ordering::Relaxed);
    }

    /// Todavía no lo llama nadie: es la palanca del **modo nocturno** del plan
    /// (§5.5), que bajará el techo de brillo por horas leyendo el RTC PCF85063.
    #[allow(dead_code)]
    pub fn set_brightness(&self, brightness: u8) {
        self.brightness.store(brightness, Ordering::Relaxed);
    }
}

pub fn spawn(
    mut driver: Ws2812Esp32Rmt<'static>,
    shared_state: Arc<RingState>,
) -> anyhow::Result<()> {
    let start_time = Instant::now();

    std::thread::Builder::new()
        .name("ring_ui".into())
        .stack_size(4096)
        .spawn(move || {
            loop {
                let now_ms = start_time.elapsed().as_millis() as u64;
                let raw_state = shared_state.state.load(Ordering::Relaxed);
                let fault = shared_state.fault.load(Ordering::Relaxed);
                let since_ms = shared_state.since_ms.load(Ordering::Relaxed) as u64;
                let state = decode_state(raw_state, fault, since_ms);
                let level = shared_state.level.load(Ordering::Relaxed);
                let brightness = shared_state.brightness.load(Ordering::Relaxed);

                let t_en_estado = now_ms.saturating_sub(since_ms);
                let raw_frame = luka_ui::frame(state, t_en_estado, level);
                let finished = luka_ui::finish(raw_frame, brightness);

                let pixels = finished.iter().map(|rgb| {
                    let (r, g, b) = leds::to_wire(rgb.r, rgb.g, rgb.b);
                    RGB8 { r, g, b }
                });

                let _ = driver.write(pixels);

                std::thread::sleep(FPS_INTERVAL);
            }
        })?;

    Ok(())
}

fn encode_state(state: State) -> u32 {
    match state {
        State::Booting => 0,
        State::WifiConnecting { .. } => 1,
        State::ServerConnecting { .. } => 2,
        State::Disconnected { .. } => 3,
        State::Idle => 4,
        State::Listening { .. } => 5,
        State::Thinking { .. } => 6,
        State::Speaking { .. } => 7,
        State::Fault { .. } => 8,
    }
}

fn decode_state(encoded: u32, fault_encoded: u32, since_ms: u64) -> State {
    match encoded {
        0 => State::Booting,
        1 => State::WifiConnecting { since_ms },
        2 => State::ServerConnecting { since_ms, attempt: 0 }, // attempt es irrelevante para UI
        3 => State::Disconnected { retry_at_ms: 0, attempt: 0 },
        4 => State::Idle,
        5 => State::Listening { since_ms },
        6 => State::Thinking { since_ms },
        7 => State::Speaking { since_ms },
        8 => {
            let kind = match fault_encoded {
                1 => Fault::WifiAssoc,
                2 => Fault::ServerUnreachable,
                3 => Fault::AuthRejected,
                4 => Fault::Audio,
                5 => Fault::WakeWord,
                6 => Fault::Panic,
                _ => Fault::Audio,
            };
            State::Fault { kind, since_ms }
        }
        _ => State::Idle,
    }
}
