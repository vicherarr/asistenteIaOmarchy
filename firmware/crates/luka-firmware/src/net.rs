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
use esp_idf_svc::hal::sys::{
    esp, esp_get_free_heap_size, esp_websocket_client_start, esp_websocket_client_stop,
    esp_wifi_sta_get_rssi,
};
use esp_idf_svc::ws::FrameType;
use esp_idf_svc::handle::RawHandle;
use luka_proto::kind;
use luka_state::{Event, Fault, Reported};
use std::ffi::CStr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, SyncSender};
use std::sync::Arc;
use std::time::{Duration, Instant};

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
const MAX_CMDS_PER_LOOP: usize = 64;

/// Muestras que se acumulan antes de subir una trama `AUDIO` (100 ms a 16 kHz).
///
/// El micro entrega tramas de 20 ms, pero subirlas una a una son **50 escrituras
/// TLS por segundo de 640 bytes**, y cada una arrastra su cabecera de WebSocket,
/// su cifrado y su ida y vuelta por la pila de red. Con eso el enlace no daba
/// abasto y se perdía el principio de las frases.
///
/// Agrupando de cinco en cinco baja a 10 escrituras por segundo de ~3,2 KB, que
/// es justo el tamaño que usa el servidor para la bajada. Los 100 ms de latencia
/// que añade son irrelevantes al lado de lo que tarda el STT.
const UPLINK_BATCH_SAMPLES: usize = 1_600;

/// Cada cuánto se manda la telemetría, que hace también de latido.
///
/// Un WebSocket sobre TCP puede quedarse *aparentemente* vivo mucho rato después
/// de que el otro extremo haya desaparecido (router reiniciado, PC suspendido).
/// El latido es lo que convierte eso en una desconexión detectable, y de paso
/// lleva las métricas para saber si el dispositivo aguanta horas.
const TELEMETRY_EVERY: Duration = Duration::from_secs(10);

pub enum NetCommand {
    ConnectWifi,
    ConnectServer,
    DropServer,
    SendEnd,
    SendCancel,
    SendAudio(Vec<i16>),
    /// JPEG de la cámara, ya comprimido y listo para salir tal cual.
    SendImage(Vec<u8>),
}

pub struct NetTask {
    cmd_rx: Receiver<NetCommand>,
    event_tx: SyncSender<Event>,
    playback_tx: SyncSender<Vec<i16>>,
    reproduccion: Arc<crate::audio::Reproduccion>,
    camara_tx: SyncSender<()>,
    modem: esp_idf_hal::modem::Modem<'static>,
}

impl NetTask {
    pub fn new(
        cmd_rx: Receiver<NetCommand>,
        event_tx: SyncSender<Event>,
        playback_tx: SyncSender<Vec<i16>>,
        reproduccion: Arc<crate::audio::Reproduccion>,
        camara_tx: SyncSender<()>,
        modem: esp_idf_hal::modem::Modem<'static>,
    ) -> Self {
        Self { cmd_rx, event_tx, playback_tx, reproduccion, camara_tx, modem }
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
        let Self { cmd_rx, event_tx, playback_tx, reproduccion, camara_tx, modem } = self;

        log::info!("net_io: arrancando");
        let sys_loop = match EspSystemEventLoop::take() {
            Ok(l) => l,
            Err(e) => {
                log::error!("no hay bucle de eventos del sistema: {e:?}");
                let _ = event_tx.try_send(Event::Faulted(Fault::WifiAssoc));
                return;
            }
        };
        log::info!("net_io: bucle de eventos listo");
        // Sin NVS la WiFi funciona igual, solo pierde la caché de calibración.
        let nvs = EspDefaultNvsPartition::take().ok();
        log::info!("net_io: NVS {}", if nvs.is_some() { "listo" } else { "no disponible" });

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

        log::info!("net_io: WiFi inicializada; esperando órdenes");
        // **Este cliente se crea una vez y NO se suelta nunca.**
        //
        // Su `Drop` llama a `esp_websocket_client_close`, que intenta mandar la
        // trama de cierre; si no hay conexión abierta el componente responde
        // "Websocket client is not connected" y devuelve ESP_FAIL… sobre el que
        // esp-idf-svc hace `.unwrap()`. Resultado: **soltar un cliente que no
        // está conectado aborta el dispositivo entero**. Verificado dos veces en
        // la placa (`panic_abort` en ws/client.rs:623), y la reconexión
        // automática no lo evita, porque el problema es "no conectado", no "no
        // arrancado".
        //
        // Así que la reconexión la lleva el propio cliente del ESP-IDF, y aquí
        // solo se para y se arranca su tarea, que sí es seguro. La máquina de
        // estados sigue mandando sobre el anillo y sobre cuándo se considera
        // caído el enlace; lo único que cede es la cadencia exacta del reintento.
        let mut client: Option<EspWebSocketClient<'static>> = None;
        // Lo pone el callback, que corre en el hilo del cliente. Sirve para
        // mandar el HELLO en cuanto hay enlace, cosa que el callback no puede
        // hacer porque el cliente todavía se está construyendo cuando se define.
        let connected = Arc::new(AtomicBool::new(false));
        let mut hello_sent = false;
        // Audio del micro pendiente de agrupar. Ver `UPLINK_BATCH_SAMPLES`.
        let mut uplink: Vec<i16> = Vec::with_capacity(UPLINK_BATCH_SAMPLES * 2);
        let arranque = Instant::now();
        let mut ultima_telemetria = Instant::now();

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

            // **Se BLOQUEA en el canal, no se duerme.**
            //
            // Aquí había un `try_recv` en bucle con `sleep(5 ms)`, y eso colgaba
            // el dispositivo entero: el tick de FreeRTOS es de 10 ms
            // (`CONFIG_FREERTOS_HZ=100`) y **`usleep` por debajo de un tick hace
            // espera activa en vez de ceder la CPU**. Como los hilos `pthread`
            // corren a prioridad 5 y la tarea `main` a 1, este hilo se quedaba
            // girando y el supervisor no volvía a ejecutarse nunca: el watchdog
            // saltaba señalando a `main`, que era la víctima y no el culpable.
            //
            // `recv_timeout` sí bloquea de verdad, así que cede la CPU y además
            // reacciona al instante en vez de esperar al siguiente ciclo.
            let primera = match cmd_rx.recv_timeout(Duration::from_millis(50)) {
                Ok(c) => Some(c),
                Err(RecvTimeoutError::Timeout) => None,
                // El otro extremo ha desaparecido: el supervisor ha muerto y este
                // hilo ya no pinta nada.
                Err(RecvTimeoutError::Disconnected) => {
                    log::error!("net_io: el supervisor cerró el canal; se termina");
                    return;
                }
            };

            if connected.load(Ordering::Relaxed)
                && ultima_telemetria.elapsed() >= TELEMETRY_EVERY
            {
                send_telemetry(&mut client, arranque);
                ultima_telemetria = Instant::now();
            }

            let mut pendiente = primera;
            let mut atendidas = 0;
            while atendidas < MAX_CMDS_PER_LOOP {
                // La primera es la que desbloqueó la espera; el resto se vacían
                // sin bloquear, para no dejar el canal lleno cuando llega una
                // ráfaga de audio.
                let cmd = match pendiente.take().or_else(|| cmd_rx.try_recv().ok()) {
                    Some(c) => c,
                    None => break,
                };
                atendidas += 1;

                match cmd {
                    NetCommand::ConnectWifi => {
                        connect_wifi(&mut wifi, &event_tx);
                    }
                    NetCommand::ConnectServer => {
                        hello_sent = false;
                        match client.as_ref() {
                            // El cliente se crea UNA vez y no se suelta jamás (ver
                            // la nota de `client`); reconectar es rearrancar su
                            // tarea. Tras un `stop` el estado queda en
                            // `WEBSOCKET_STATE_UNKNOW`, así que `start` vuelve a
                            // pasar su comprobación.
                            Some(c) => {
                                if let Err(e) = esp!(unsafe { esp_websocket_client_start(c.handle()) }) {
                                    log::warn!("WS: no se pudo rearrancar ({e:?})");
                                    let _ = event_tx.try_send(Event::ServerDown);
                                }
                            }
                            None => {
                                client = connect_server(&event_tx, &playback_tx, &reproduccion, &camara_tx, &connected);
                                if client.is_none() {
                                    let _ = event_tx.try_send(Event::ServerDown);
                                }
                            }
                        }
                    }
                    NetCommand::DropServer => {
                        // **No se suelta el cliente**: hacerlo aborta el
                        // dispositivo. Se para su tarea, que sí es seguro, y el
                        // objeto se queda vivo para reutilizarlo.
                        //
                        // `stop` espera a que la tarea termine de verdad, así que
                        // al volver de aquí el enlace está cerrado y el siguiente
                        // `start` arranca limpio.
                        hello_sent = false;
                        uplink.clear();
                        if let Some(c) = client.as_ref() {
                            if let Err(e) = esp!(unsafe { esp_websocket_client_stop(c.handle()) }) {
                                log::debug!("WS: stop devolvió {e:?} (ya estaba parado)");
                            }
                        }
                        connected.store(false, Ordering::Relaxed);
                    }
                    NetCommand::SendEnd => {
                        // Vaciar ANTES del END: si no, el último trozo de voz
                        // llegaría después de que el servidor haya cerrado el
                        // turno y se mezclaría con el siguiente.
                        flush_uplink(&mut client, &mut uplink);
                        send_bare(&mut client, kind::END);
                    }
                    NetCommand::SendCancel => {
                        // Aquí al revés: lo pendiente se tira, que es lo que
                        // significa cancelar.
                        uplink.clear();
                        send_bare(&mut client, kind::CANCEL);
                    }
                    NetCommand::SendAudio(pcm) => {
                        uplink.extend_from_slice(&pcm);
                        if uplink.len() >= UPLINK_BATCH_SAMPLES {
                            flush_uplink(&mut client, &mut uplink);
                        }
                    }
                    NetCommand::SendImage(jpeg) => {
                        // Va entera: a 320x240 son ~6 kB contra un tope de 64.
                        // La cámara ya comprobó que cabe antes de mandarla aquí.
                        let mut trama = Vec::with_capacity(1 + jpeg.len());
                        trama.push(kind::IMAGE);
                        trama.extend_from_slice(&jpeg);
                        if let Some(c) = client.as_mut() {
                            if let Err(e) = c.send(FrameType::Binary(false), &trama) {
                                log::warn!("no se pudo subir la imagen: {e:?}");
                            } else {
                                log::info!("imagen enviada ({} B)", jpeg.len());
                            }
                        } else {
                            log::warn!("imagen descartada: no hay enlace");
                        }
                    }
                }
            }
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
    reproduccion: &Arc<crate::audio::Reproduccion>,
    camara_tx: &SyncSender<()>,
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
        // La política de reintentos es **de la máquina de estados y de nadie
        // más**: es donde está escrita, donde se entiende y donde hay tests.
        //
        // Se puede desactivar la reconexión automática porque el cliente ya no se
        // suelta nunca (ver la nota de `client`). Lo que abortaba el dispositivo
        // era el `Drop`, no este ajuste; mientras el objeto viva, pararlo y
        // arrancarlo es seguro.
        disable_auto_reconnect: true,
        ..Default::default()
    };

    let event_tx = event_tx.clone();
    let playback_tx = playback_tx.clone();
    let reproduccion = reproduccion.clone();
    let camara_tx = camara_tx.clone();
    let connected = connected.clone();
    // Se pone a true con la primera trama de audio de cada respuesta, para
    // mandar `TtsStarted` una sola vez por turno.
    let hablando = Arc::new(AtomicBool::new(false));

    log::info!("WS: conectando a {uri}");
    let resultado = EspWebSocketClient::new(&uri, &config, Duration::from_secs(10), move |event| {
        // El callback recibe un `Result`, y un `Err` aquí **no significa que el
        // enlace se haya caído**.
        //
        // esp-idf-svc 0.52 traduce con `_ => Err(ESP_ERR_INVALID_ARG)` cualquier
        // id de evento que no conozca, y el componente gestionado que se compila
        // (esp_websocket_client 1.8.0) es bastante más nuevo: emite `BEGIN`,
        // `FINISH` y `HEADER_RECEIVED`, que esa versión no contempla. O sea que
        // el primer evento de toda conexión llega como "error".
        //
        // Tratarlo como caída costó una tarde: el dispositivo conectaba, se
        // declaraba caído a sí mismo, paraba el cliente y volvía a empezar, en
        // bucle. El servidor lo veía conectar y desconectar cada segundo.
        //
        // Así que un `Err` solo se registra. Las caídas de verdad llegan como
        // `Disconnected` o `Closed`, que se manejan abajo.
        let event = match event {
            Ok(e) => e,
            Err(e) => {
                log::debug!("WS: evento no reconocido por esp-idf-svc ({e:?}); se ignora");
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
                handle_frame(data, &event_tx, &playback_tx, &hablando, &reproduccion, &camara_tx);
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
    reproduccion: &Arc<crate::audio::Reproduccion>,
    camara_tx: &SyncSender<()>,
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
            reproduccion.limpiar_fin();
            let pcm: Vec<i16> = frame
                .payload
                .chunks_exact(2)
                .map(|b| i16::from_le_bytes([b[0], b[1]]))
                .collect();

            // El turno de voz NO empieza con la primera trama: se deja que se
            // acumule el colchón. Arrancar con 20 ms de audio y luego consumir a
            // 16 kHz sin pausa es lo que hacía que cualquier hipo del servidor
            // se oyera como un corte.
            if !hablando.load(Ordering::Relaxed)
                && reproduccion.pendientes() >= crate::audio::COLCHON_SAMPLES
            {
                hablando.store(true, Ordering::Relaxed);
                let _ = event_tx.try_send(Event::TtsStarted);
            }
            // Si el búfer de reproducción está lleno se descarta: más vale un
            // hueco corto que acumular retraso hasta que Luka hable con segundos
            // de desfase respecto al anillo.
            if playback_tx.try_send(pcm).is_err() {
                log::warn!("audio de bajada descartado: el búfer va lleno");
            }
        }

        kind::CAPTURE => {
            // No se captura aquí: esto corre DENTRO del callback del cliente
            // WebSocket, y bloquearlo 100 ms comprimiendo un JPEG es exactamente
            // como se tumbó el enlace con el barge-in. Solo se avisa al hilo de
            // la cámara, que tiene su propia prioridad.
            log::info!("← CAPTURE: se pide una foto");
            if camara_tx.try_send(()).is_err() {
                log::warn!("petición de foto descartada: la cámara está ocupada");
            }
        }

        kind::TTS_END => {
            // Una respuesta corta puede no haber llegado nunca al colchón. Si no
            // se anuncia aquí, ese audio se quedaría en la cola sin sonar jamás.
            if !hablando.swap(false, Ordering::Relaxed) {
                let _ = event_tx.try_send(Event::TtsStarted);
            }
            // **No se manda TtsEnded aquí.** `TTS_END` significa "no viene más
            // audio", no "ya ha sonado todo": con el colchón el dispositivo va
            // por detrás del servidor. Lo manda el supervisor cuando la cola se
            // vacía de verdad; si no, se cortaría el final de cada respuesta.
            reproduccion.marcar_fin();
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

/// Manda el latido con diagnóstico.
fn send_telemetry(client: &mut Option<EspWebSocketClient<'static>>, arranque: Instant) {
    let Some(c) = client.as_mut() else { return };

    // El RSSI se lee en crudo del driver: esp-idf-svc 0.52 no lo expone para la
    // estación asociada, solo dentro de los resultados de un escaneo, y escanear
    // para saber la potencia del enlace sería absurdo (corta el tráfico).
    let mut rssi: core::ffi::c_int = 0;
    let rssi = match esp!(unsafe { esp_wifi_sta_get_rssi(&mut rssi) }) {
        Ok(()) => rssi as i32,
        // -127 es el "sin dato" convencional del propio ESP-IDF.
        Err(_) => -127,
    };

    let free_heap = unsafe { esp_get_free_heap_size() };
    let buf = luka_proto::telemetry(rssi, arranque.elapsed().as_secs() as u32, free_heap);

    if let Some(bytes) = buf.as_frame() {
        if let Err(e) = c.send(FrameType::Binary(false), bytes) {
            log::warn!("no se pudo mandar la telemetría: {e:?}");
        }
    }
}

/// Sube lo acumulado y vacía el acumulador.
fn flush_uplink(client: &mut Option<EspWebSocketClient<'static>>, uplink: &mut Vec<i16>) {
    if !uplink.is_empty() {
        send_audio(client, uplink);
        uplink.clear();
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
