//! WiFi y cliente WebSocket contra el asistente.
//!
//! Un solo hilo posee la radio y el cliente. Recibe órdenes por canal y publica
//! eventos hacia el supervisor, que es quien decide; aquí no se toma ninguna
//! decisión de estado.
//!
//! # El certificado va fijado
//!
//! El firmware **no confía en ninguna autoridad certificadora**: lleva empotrado
//! el certificado del asistente y solo acepta ese. Es más fuerte que la
//! validación normal —no basta un certificado válido, tiene que ser *este*— y
//! además es la única opción viable, porque el del asistente es autofirmado y con
//! `CN=localhost`, que jamás pasaría una validación de nombre contra una IP de la
//! LAN. Por eso se activa `skip_cert_common_name_check`: el nombre no aporta nada
//! cuando ya se exige el certificado exacto.

use anyhow::{Context, Result};
use esp_idf_svc::eventloop::EspSystemEventLoop;
use esp_idf_svc::nvs::EspDefaultNvsPartition;
use esp_idf_svc::tls::X509;
use esp_idf_svc::wifi::{AuthMethod, BlockingWifi, ClientConfiguration, Configuration, EspWifi};
use esp_idf_svc::ws::client::{
    EspWebSocketClient, EspWebSocketClientConfig, WebSocketClosingReason, WebSocketEventType,
};
use esp_idf_svc::ws::FrameType;
use luka_proto::kind;
use luka_state::{Event, Fault, Reported};
use std::ffi::CStr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, SyncSender};
use std::sync::Arc;
use std::time::Duration;

/// Búfer de recepción del cliente WebSocket.
///
/// **Acoplado al servidor a propósito:** `DOWNLINK_CHUNK_SAMPLES` de
/// `device_gateway.py` son 1600 muestras, o sea tramas `TTS_AUDIO` de 3201 bytes.
/// Si este búfer fuera menor, el ESP-IDF partiría las tramas y habría que
/// reensamblarlas. Con margen de sobra y así el reensamblado no hace falta.
const WS_BUFFER_BYTES: usize = 8192;

/// Tope de tramas de audio que se procesan por vuelta del bucle.
///
/// El bucle **no puede** atender una sola orden por espera: a 16 kHz salen 50
/// tramas por segundo, y atenderlas de una en una con pausa dejaría el canal
/// lleno y el audio se perdería. Se vacía en bloque, con un tope para que subir
/// audio no impida nunca atender un `SendEnd`.
const MAX_CMDS_PER_LOOP: usize = 32;

pub enum NetCommand {
    ConnectWifi,
    ConnectServer,
    DropServer,
    SendEnd,
    SendCancel,
    SendAudio(Vec<i16>),
}

pub struct NetTask {
    cmd_rx: Receiver<NetCommand>,
    event_tx: SyncSender<Event>,
    playback_tx: SyncSender<Vec<i16>>,
    modem: esp_idf_hal::modem::Modem<'static>,
}

impl NetTask {
    pub fn new(
        cmd_rx: Receiver<NetCommand>,
        event_tx: SyncSender<Event>,
        playback_tx: SyncSender<Vec<i16>>,
        modem: esp_idf_hal::modem::Modem<'static>,
    ) -> Self {
        Self { cmd_rx, event_tx, playback_tx, modem }
    }

    pub fn spawn(self) -> Result<()> {
        std::thread::Builder::new()
            .name("net_io".into())
            // El handshake TLS con un certificado RSA-4096 come pila; con menos
            // de esto el hilo se desborda justo al conectar.
            .stack_size(16384)
            .spawn(move || self.run_loop())
            .context("no se pudo crear el hilo net_io")?;
        Ok(())
    }

    fn run_loop(self) {
        let Self { cmd_rx, event_tx, playback_tx, modem } = self;

        let sys_loop = match EspSystemEventLoop::take() {
            Ok(l) => l,
            Err(e) => {
                log::error!("no hay bucle de eventos del sistema: {e:?}");
                let _ = event_tx.try_send(Event::Faulted(Fault::WifiAssoc));
                return;
            }
        };
        // Sin NVS la WiFi funciona igual, solo pierde la caché de calibración.
        let nvs = EspDefaultNvsPartition::take().ok();

        let mut wifi = match EspWifi::new(modem, sys_loop.clone(), nvs)
            .and_then(|w| BlockingWifi::wrap(w, sys_loop))
        {
            Ok(w) => w,
            Err(e) => {
                log::error!("no se pudo inicializar la WiFi: {e:?}");
                let _ = event_tx.try_send(Event::Faulted(Fault::WifiAssoc));
                return;
            }
        };

        let mut client: Option<EspWebSocketClient<'static>> = None;
        // Lo pone el callback, que corre en el hilo del cliente. Sirve para
        // mandar el HELLO en cuanto hay enlace, cosa que el callback no puede
        // hacer porque el cliente todavía se está construyendo cuando se define.
        let connected = Arc::new(AtomicBool::new(false));
        let mut hello_sent = false;

        loop {
            if connected.load(Ordering::Relaxed) && !hello_sent {
                if let Some(c) = client.as_mut() {
                    let hello = luka_proto::hello(
                        luka_config::device::NAME,
                        env!("CARGO_PKG_VERSION"),
                        luka_board::audio::SAMPLE_RATE_HZ,
                        luka_board::audio::CHANNELS,
                    );
                    match hello.as_frame() {
                        Some(bytes) => {
                            if let Err(e) = c.send(FrameType::Binary(false), bytes) {
                                log::warn!("no se pudo mandar el HELLO: {e:?}");
                            }
                            hello_sent = true;
                        }
                        // Solo pasa si el nombre del dispositivo es absurdamente
                        // largo. La sesión sigue: el HELLO es informativo.
                        None => {
                            log::warn!("el HELLO no cabe en su búfer; se omite");
                            hello_sent = true;
                        }
                    }
                }
            }

            let mut atendidas = 0;
            while atendidas < MAX_CMDS_PER_LOOP {
                let cmd = match cmd_rx.try_recv() {
                    Ok(c) => c,
                    Err(_) => break,
                };
                atendidas += 1;

                match cmd {
                    NetCommand::ConnectWifi => {
                        connect_wifi(&mut wifi, &event_tx);
                    }
                    NetCommand::ConnectServer => {
                        connected.store(false, Ordering::Relaxed);
                        hello_sent = false;
                        client = connect_server(&event_tx, &playback_tx, &connected);
                        if client.is_none() {
                            let _ = event_tx.try_send(Event::ServerDown);
                        }
                    }
                    NetCommand::DropServer => {
                        // Soltar el cliente es la única forma de cerrar: el
                        // envoltorio hace `panic!` si se intenta cerrar a mano.
                        client = None;
                        connected.store(false, Ordering::Relaxed);
                        hello_sent = false;
                    }
                    NetCommand::SendEnd => send_bare(&mut client, kind::END),
                    NetCommand::SendCancel => send_bare(&mut client, kind::CANCEL),
                    NetCommand::SendAudio(pcm) => send_audio(&mut client, &pcm),
                }
            }

            // Espera corta: mantiene la latencia del audio baja sin quemar CPU
            // cuando no hay nada que hacer.
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

/// Asocia a la WiFi **y espera a tener IP**.
///
/// Esperar el `netif` es lo que separa "asociado" de "utilizable": sin IP no hay
/// forma de abrir el WebSocket, y anunciar `WifiUp` antes de tenerla produce un
/// fallo de conexión al servidor que parece un problema del servidor.
fn connect_wifi(wifi: &mut BlockingWifi<EspWifi<'static>>, event_tx: &SyncSender<Event>) {
    log::info!("WiFi: asociando a «{}»…", luka_config::wifi::SSID);

    let ssid = match luka_config::wifi::SSID.try_into() {
        Ok(s) => s,
        Err(_) => {
            log::error!("el SSID de cfg.toml pasa de 32 caracteres");
            let _ = event_tx.try_send(Event::Faulted(Fault::WifiAssoc));
            return;
        }
    };
    let password = match luka_config::wifi::PASSWORD.try_into() {
        Ok(p) => p,
        Err(_) => {
            log::error!("la contraseña de cfg.toml pasa de 64 caracteres");
            let _ = event_tx.try_send(Event::Faulted(Fault::WifiAssoc));
            return;
        }
    };

    let config = Configuration::Client(ClientConfiguration {
        ssid,
        password,
        auth_method: AuthMethod::WPA2Personal,
        ..Default::default()
    });

    let resultado = wifi
        .set_configuration(&config)
        .and_then(|_| wifi.start())
        .and_then(|_| wifi.connect())
        .and_then(|_| wifi.wait_netif_up());

    match resultado {
        Ok(()) => {
            match wifi.wifi().sta_netif().get_ip_info() {
                Ok(ip) => log::info!("WiFi: lista, IP {}", ip.ip),
                Err(e) => log::warn!("WiFi arriba pero sin poder leer la IP: {e:?}"),
            }
            let _ = event_tx.try_send(Event::WifiUp);
        }
        Err(e) => {
            // El anillo lo deletrea con 1 parpadeo: SSID/PSK, o el router no da 2,4 GHz.
            log::error!("WiFi: no se pudo asociar ({e:?})");
            let _ = event_tx.try_send(Event::Faulted(Fault::WifiAssoc));
        }
    }
}

/// Abre el WebSocket con el certificado fijado.
fn connect_server(
    event_tx: &SyncSender<Event>,
    playback_tx: &SyncSender<Vec<i16>>,
    connected: &Arc<AtomicBool>,
) -> Option<EspWebSocketClient<'static>> {
    let cert = match CStr::from_bytes_with_nul(luka_config::server::TLS_CERT_PEM.as_bytes()) {
        Ok(c) => c,
        Err(e) => {
            // Lo cazaría antes el test de `luka-config`, pero un certificado mal
            // formado aquí sería un fallo de handshake sin explicación.
            log::error!("el certificado fijado no es un C-string válido: {e}");
            return None;
        }
    };

    // El token va **solo en la cabecera**, nunca en la URL: sobre TLS el query
    // string no viaja en claro, pero sí acaba en el log de accesos del servidor.
    let headers = format!("X-API-Token: {}\r\n", luka_config::server::API_TOKEN);
    let uri = format!(
        "wss://{}:{}{}",
        luka_config::server::HOST,
        luka_config::server::PORT,
        luka_config::server::WS_PATH
    );

    let config = EspWebSocketClientConfig {
        server_cert: Some(X509::pem(cert)),
        skip_cert_common_name_check: true,
        headers: Some(&headers),
        buffer_size: WS_BUFFER_BYTES,
        // La reconexión la gobierna la máquina de estados, con su backoff y su
        // anillo. Dos lógicas de reintento a la vez solo se estorbarían.
        disable_auto_reconnect: true,
        ..Default::default()
    };

    let event_tx = event_tx.clone();
    let playback_tx = playback_tx.clone();
    let connected = connected.clone();
    // Se pone a true con la primera trama de audio de cada respuesta, para
    // mandar `TtsStarted` una sola vez por turno.
    let hablando = Arc::new(AtomicBool::new(false));

    log::info!("WS: conectando a {uri}");
    let resultado = EspWebSocketClient::new(&uri, &config, Duration::from_secs(10), move |event| {
        // El callback recibe un `Result`: un error de la capa de transporte llega
        // por aquí igual que un evento. Se trata como caída del enlace, que es lo
        // que es, para que el supervisor reintente en vez de quedarse esperando.
        let event = match event {
            Ok(e) => e,
            Err(e) => {
                log::warn!("WS: error de transporte ({e:?})");
                connected.store(false, Ordering::Relaxed);
                let _ = event_tx.try_send(Event::ServerDown);
                return;
            }
        };

        match &event.event_type {
            WebSocketEventType::Connected => {
                log::info!("WS: conectado");
                connected.store(true, Ordering::Relaxed);
                let _ = event_tx.try_send(Event::ServerUp);
            }
            WebSocketEventType::Disconnected => {
                log::info!("WS: desconectado");
                connected.store(false, Ordering::Relaxed);
                let _ = event_tx.try_send(Event::ServerDown);
            }
            // 1008 es lo que manda el servidor cuando el token no cuadra. Es un
            // fallo de configuración, no de red, así que se distingue: la máquina
            // de estados no lo reintenta y el anillo lo deletrea con 3 parpadeos.
            WebSocketEventType::Close(Some(WebSocketClosingReason::PolicyViolated)) => {
                log::error!("WS: el servidor rechazó el token (1008)");
                connected.store(false, Ordering::Relaxed);
                let _ = event_tx.try_send(Event::AuthRejected);
            }
            WebSocketEventType::Close(_) | WebSocketEventType::Closed => {
                connected.store(false, Ordering::Relaxed);
                let _ = event_tx.try_send(Event::ServerDown);
            }
            WebSocketEventType::Binary(data) => {
                handle_frame(data, &event_tx, &playback_tx, &hablando);
            }
            WebSocketEventType::Text(text) => {
                // El protocolo es binario; si llega texto, algo no cuadra.
                log::warn!("WS: trama de texto inesperada ({} bytes)", text.len());
            }
            _ => {}
        }
    });

    match resultado {
        Ok(client) => Some(client),
        Err(e) => {
            log::error!("WS: no se pudo conectar ({e:?})");
            None
        }
    }
}

/// Procesa una trama recibida del servidor.
fn handle_frame(
    data: &[u8],
    event_tx: &SyncSender<Event>,
    playback_tx: &SyncSender<Vec<i16>>,
    hablando: &Arc<AtomicBool>,
) {
    let frame = match luka_proto::decode(data) {
        Ok(f) => f,
        Err(e) => {
            log::warn!("WS: trama inválida ({e})");
            return;
        }
    };

    match frame.kind {
        kind::STATE => {
            if let Some(estado) = frame.str_field("state") {
                let reported = match estado {
                    luka_proto::state::LISTENING => Reported::Listening,
                    luka_proto::state::THINKING => Reported::Thinking,
                    luka_proto::state::SPEAKING => Reported::Speaking,
                    _ => Reported::Idle,
                };
                if reported == Reported::Idle {
                    hablando.store(false, Ordering::Relaxed);
                }
                let _ = event_tx.try_send(Event::ServerSaid(reported));
            }
        }

        kind::TTS_AUDIO => {
            // La carga útil empieza en el byte 1 del búfer, así que **no se puede
            // reinterpretar como `*const i16`**: estaría desalineada, y en Xtensa
            // una lectura de 16 bits desalineada es comportamiento indefinido.
            // Se decodifica byte a byte, que además fija el orden little-endian
            // del protocolo en vez de heredar el de la máquina.
            let pcm: Vec<i16> = frame
                .payload
                .chunks_exact(2)
                .map(|b| i16::from_le_bytes([b[0], b[1]]))
                .collect();

            if !hablando.swap(true, Ordering::Relaxed) {
                let _ = event_tx.try_send(Event::TtsStarted);
            }
            // Si el búfer de reproducción está lleno se descarta: más vale un
            // hueco corto que acumular retraso hasta que Luka hable con segundos
            // de desfase respecto al anillo.
            if playback_tx.try_send(pcm).is_err() {
                log::warn!("audio de bajada descartado: el búfer va lleno");
            }
        }

        kind::TTS_END => {
            hablando.store(false, Ordering::Relaxed);
            let _ = event_tx.try_send(Event::TtsEnded);
        }

        kind::TRANSCRIPT => {
            log::info!("→ {}", frame.as_str().unwrap_or("<no es UTF-8>"));
        }

        kind::REPLY => {
            log::info!("← {}", frame.as_str().unwrap_or("<no es UTF-8>"));
        }

        kind::ERROR => {
            // El código es informativo (`no_speech`, `turn_failed`…). El rechazo
            // de credenciales NO llega por aquí: llega como cierre 1008.
            log::warn!(
                "WS: error del servidor: {}",
                frame.as_str().unwrap_or("<no es UTF-8>")
            );
        }

        kind::PONG => {}

        otro => log::warn!("WS: trama inesperada {} ({otro:#04x})", luka_proto::name(otro)),
    }
}

fn send_bare(client: &mut Option<EspWebSocketClient<'static>>, kind: u8) {
    let Some(c) = client.as_mut() else { return };
    let buf = luka_proto::bare(kind);
    // `bare` nunca desborda: es un único byte en un búfer de uno.
    if let Some(bytes) = buf.as_frame() {
        if let Err(e) = c.send(FrameType::Binary(false), bytes) {
            log::warn!("no se pudo mandar {}: {e:?}", luka_proto::name(kind));
        }
    }
}

fn send_audio(client: &mut Option<EspWebSocketClient<'static>>, pcm: &[i16]) {
    let Some(c) = client.as_mut() else { return };

    // Se arma la trama en bytes explícitos en vez de reinterpretar el `&[i16]`:
    // así el little-endian del protocolo queda escrito y no depende de la
    // arquitectura.
    let mut frame = Vec::with_capacity(1 + pcm.len() * 2);
    frame.push(kind::AUDIO);
    for sample in pcm {
        frame.extend_from_slice(&sample.to_le_bytes());
    }

    if let Err(e) = c.send(FrameType::Binary(false), &frame) {
        log::warn!("no se pudo subir audio: {e:?}");
    }
}
