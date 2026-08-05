//! Códec del protocolo binario con el asistente (`/device/ws`).
//!
//! El otro extremo es `src/device_protocol.py`; **los dos ficheros tienen que
//! contar la misma historia**. Formato: **1 byte de tipo + carga útil**. Los tipos
//! con el bit alto puesto (`0x8x`) van del servidor al dispositivo; los demás, al
//! revés. Las tramas de control llevan JSON en UTF-8; las de audio, PCM16
//! little-endian sin cabecera (el formato se acuerda en el HELLO y no cambia
//! durante la sesión).
//!
//! Es `no_std`, sin asignaciones dinámicas y sin ninguna dependencia del ESP-IDF,
//! a propósito: así se prueba entero con `cargo test` en el PC. Un fallo de
//! encuadre visto desde el otro lado del cable cuesta horas; visto desde un test,
//! segundos.

#![no_std]
#![deny(unsafe_code)]

use core::fmt::Write as _;

/// Tipos de trama. Los valores son parte del contrato con el servidor: cambiarlos
/// aquí sin cambiarlos en `device_protocol.py` rompe el enlace en silencio.
pub mod kind {
    // --- Dispositivo -> Servidor ---
    /// `{device, fw, sample_rate, channels}`: abre la sesión.
    pub const HELLO: u8 = 0x01;
    /// PCM16LE del micrófono.
    pub const AUDIO: u8 = 0x02;
    /// Fin del turno de habla: que el servidor procese lo acumulado.
    pub const END: u8 = 0x03;
    /// Aborta el turno en curso.
    pub const CANCEL: u8 = 0x04;
    /// Latido.
    pub const PING: u8 = 0x05;

    // --- Servidor -> Dispositivo ---
    /// `{state}`: idle|listening|thinking|speaking.
    pub const STATE: u8 = 0x81;
    /// `{text}`: lo que el STT entendió.
    pub const TRANSCRIPT: u8 = 0x82;
    /// `{text}`: la respuesta de Luka.
    pub const REPLY: u8 = 0x83;
    /// PCM16LE de la voz de Luka.
    pub const TTS_AUDIO: u8 = 0x84;
    /// Fin de la respuesta hablada.
    pub const TTS_END: u8 = 0x85;
    /// `{code, message}`.
    pub const ERROR: u8 = 0x86;
    /// Respuesta al latido.
    pub const PONG: u8 = 0x87;
}

/// Valores del campo `state` de las tramas [`kind::STATE`].
pub mod state {
    pub const IDLE: &str = "idle";
    pub const LISTENING: &str = "listening";
    pub const THINKING: &str = "thinking";
    pub const SPEAKING: &str = "speaking";
}

/// Tope de una trama, igual que el del servidor. Una trama mayor es un error de
/// encuadre, no un mensaje legítimo.
pub const MAX_FRAME_BYTES: usize = 64 * 1024;

/// Nombre legible de un tipo de trama, para los logs.
///
/// Devuelve `"desconocido"` en vez de un número porque formatear el valor exigiría
/// asignar; el byte crudo ya se loguea aparte donde hace falta.
pub const fn name(kind: u8) -> &'static str {
    match kind {
        kind::HELLO => "HELLO",
        kind::AUDIO => "AUDIO",
        kind::END => "END",
        kind::CANCEL => "CANCEL",
        kind::PING => "PING",
        kind::STATE => "STATE",
        kind::TRANSCRIPT => "TRANSCRIPT",
        kind::REPLY => "REPLY",
        kind::TTS_AUDIO => "TTS_AUDIO",
        kind::TTS_END => "TTS_END",
        kind::ERROR => "ERROR",
        kind::PONG => "PONG",
        _ => "desconocido",
    }
}

/// ¿La trama viaja del servidor al dispositivo? Es el bit alto del tipo.
pub const fn is_from_server(kind: u8) -> bool {
    kind & 0x80 != 0
}

/// Trama mal formada.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProtocolError {
    /// Sin siquiera el byte de tipo.
    Empty,
    /// Más grande que [`MAX_FRAME_BYTES`].
    TooLong(usize),
    /// El servidor mandó un tipo que este firmware no conoce.
    UnknownKind(u8),
}

impl core::fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Empty => write!(f, "trama vacía"),
            Self::TooLong(n) => write!(f, "trama de {n} bytes; el máximo es {MAX_FRAME_BYTES}"),
            Self::UnknownKind(k) => write!(f, "tipo de trama desconocido: {k:#04x}"),
        }
    }
}

/// Una trama decodificada. Presta la carga útil del búfer de recepción en vez de
/// copiarla: el audio del TTS llega en tramas grandes y copiarlas sería tirar
/// PSRAM y ciclos a la basura.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Frame<'a> {
    pub kind: u8,
    pub payload: &'a [u8],
}

impl<'a> Frame<'a> {
    pub const fn name(&self) -> &'static str {
        name(self.kind)
    }

    /// Interpreta la carga útil como texto UTF-8, si lo es.
    pub fn as_str(&self) -> Option<&'a str> {
        core::str::from_utf8(self.payload).ok()
    }

    /// Valor de un campo de texto del JSON de la carga útil. Ver [`json_str_field`].
    pub fn str_field(&self, field: &str) -> Option<&'a str> {
        json_str_field(self.payload, field)
    }
}

/// Decodifica una trama recibida.
pub fn decode(data: &[u8]) -> Result<Frame<'_>, ProtocolError> {
    match data.split_first() {
        None => Err(ProtocolError::Empty),
        Some(_) if data.len() > MAX_FRAME_BYTES => Err(ProtocolError::TooLong(data.len())),
        Some((&kind, payload)) => Ok(Frame { kind, payload }),
    }
}

// ---------------------------------------------------------------- construcción

/// Búfer de tamaño fijo donde se arma una trama antes de enviarla.
///
/// Con capacidad en el tipo en vez de en un `Vec`: el firmware no tiene asignador
/// en la ruta de tiempo real, y así "no cabe" es un `Err` en el sitio donde se
/// escribe, no una reserva de memoria a mitad de un turno de voz.
#[derive(Debug, Clone)]
pub struct Buf<const N: usize> {
    bytes: [u8; N],
    len: usize,
    /// Se pega en cuanto una escritura no cabe. Se consulta al terminar, así que
    /// un truncamiento nunca se cuela como trama válida a medias.
    overflow: bool,
}

impl<const N: usize> Default for Buf<N> {
    fn default() -> Self {
        Self::new()
    }
}

impl<const N: usize> Buf<N> {
    pub const fn new() -> Self {
        Self { bytes: [0; N], len: 0, overflow: false }
    }

    pub fn clear(&mut self) {
        self.len = 0;
        self.overflow = false;
    }

    /// Los bytes escritos, o `None` si algo no cupo.
    pub fn as_frame(&self) -> Option<&[u8]> {
        if self.overflow {
            None
        } else {
            Some(&self.bytes[..self.len])
        }
    }

    fn push(&mut self, b: u8) {
        if self.len < N {
            self.bytes[self.len] = b;
            self.len += 1;
        } else {
            self.overflow = true;
        }
    }

    fn extend(&mut self, data: &[u8]) {
        for &b in data {
            self.push(b);
        }
    }

    /// Escribe una cadena como literal JSON, con comillas y escapes.
    ///
    /// Hace falta porque el nombre del dispositivo sale de `cfg.toml` y podría
    /// llevar comillas o barras; sin escapar, el servidor recibiría un JSON roto
    /// y el HELLO se perdería sin decir por qué.
    fn push_json_string(&mut self, s: &str) {
        self.push(b'"');
        for c in s.chars() {
            match c {
                '"' => self.extend(b"\\\""),
                '\\' => self.extend(b"\\\\"),
                '\n' => self.extend(b"\\n"),
                '\r' => self.extend(b"\\r"),
                '\t' => self.extend(b"\\t"),
                // El resto de caracteres de control van en \u00XX; lo no-ASCII se
                // manda tal cual, que UTF-8 es válido dentro de un string JSON.
                c if (c as u32) < 0x20 => {
                    let _ = write!(EscapeSink(self), "\\u{:04x}", c as u32);
                }
                c => {
                    let mut utf8 = [0u8; 4];
                    self.extend(c.encode_utf8(&mut utf8).as_bytes());
                }
            }
        }
        self.push(b'"');
    }
}

/// Adaptador para poder usar `write!` sobre el búfer sin exponer `fmt::Write` en
/// la API pública (que invitaría a formatear en la ruta de tiempo real).
struct EscapeSink<'a, const N: usize>(&'a mut Buf<N>);

impl<const N: usize> core::fmt::Write for EscapeSink<'_, N> {
    fn write_str(&mut self, s: &str) -> core::fmt::Result {
        self.0.extend(s.as_bytes());
        Ok(())
    }
}

/// Trama sin carga útil: END, CANCEL, PING.
pub fn bare(kind: u8) -> Buf<1> {
    let mut buf = Buf::new();
    buf.push(kind);
    buf
}

/// Capacidad del HELLO: el nombre del dispositivo cabe de sobra en lo que queda.
pub const HELLO_CAP: usize = 192;

/// Trama de apertura de sesión.
pub fn hello(device: &str, fw: &str, sample_rate: u32, channels: u16) -> Buf<HELLO_CAP> {
    let mut buf = Buf::new();
    buf.push(kind::HELLO);
    buf.extend(b"{\"device\":");
    buf.push_json_string(device);
    buf.extend(b",\"fw\":");
    buf.push_json_string(fw);
    let _ = write!(EscapeSink(&mut buf), ",\"sample_rate\":{sample_rate},\"channels\":{channels}}}");
    buf
}

/// Escribe una trama de audio en un búfer prestado, sin copiar el PCM dos veces.
///
/// Devuelve la porción escrita. El llamante reserva el búfer una vez y lo reutiliza
/// en cada trama: a 16 kHz salen 50 tramas por segundo y asignar en ese bucle
/// acabaría fragmentando el montón.
pub fn audio_into<'b>(out: &'b mut [u8], pcm: &[u8]) -> Option<&'b [u8]> {
    let total = 1 + pcm.len();
    if total > out.len() || total > MAX_FRAME_BYTES {
        return None;
    }
    out[0] = kind::AUDIO;
    out[1..total].copy_from_slice(pcm);
    Some(&out[..total])
}

// ------------------------------------------------------------------ JSON mínimo

/// Extrae el valor de un campo **de texto** de un objeto JSON plano.
///
/// Deliberadamente no es un parser de JSON: solo busca `"campo"` seguido de `:` y
/// de un literal de cadena, y devuelve su contenido **sin desescapar**. Es todo lo
/// que el firmware necesita —los campos que consulta (`state`, `code`) son
/// identificadores fijos que el servidor genera, nunca texto libre—, y ahorra
/// meter un parser entero en un dispositivo que no lo va a usar para nada más.
///
/// Por eso mismo devuelve `None` si el valor contiene una barra invertida: antes
/// que entregar una cadena mal desescapada, no entrega nada. El texto libre
/// (`TRANSCRIPT`, `REPLY`) se loguea en crudo con [`Frame::as_str`].
pub fn json_str_field<'a>(payload: &'a [u8], field: &str) -> Option<&'a str> {
    let text = core::str::from_utf8(payload).ok()?;
    let mut rest = text;

    loop {
        // Buscar la clave entrecomillada. Se repite en bucle porque la misma
        // secuencia podría aparecer dentro de un valor anterior.
        let at = rest.find('"')?;
        rest = &rest[at + 1..];
        let end = rest.find('"')?;
        let (key, after) = rest.split_at(end);
        rest = &after[1..];

        if key != field {
            continue;
        }
        let after_colon = rest.trim_start();
        let Some(after_colon) = after_colon.strip_prefix(':') else {
            continue;
        };
        let value = after_colon.trim_start();
        let Some(value) = value.strip_prefix('"') else {
            // El campo existe pero no es una cadena (un número, `null`…).
            return None;
        };
        let end = value.find('"')?;
        let value = &value[..end];
        return if value.contains('\\') { None } else { Some(value) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn los_tipos_del_servidor_llevan_el_bit_alto() {
        for k in [kind::STATE, kind::TRANSCRIPT, kind::REPLY, kind::TTS_AUDIO, kind::TTS_END, kind::ERROR, kind::PONG] {
            assert!(is_from_server(k), "{} debería venir del servidor", name(k));
        }
        for k in [kind::HELLO, kind::AUDIO, kind::END, kind::CANCEL, kind::PING] {
            assert!(!is_from_server(k), "{} lo manda el dispositivo", name(k));
        }
    }

    #[test]
    fn los_tipos_son_unicos() {
        let todos = [
            kind::HELLO, kind::AUDIO, kind::END, kind::CANCEL, kind::PING,
            kind::STATE, kind::TRANSCRIPT, kind::REPLY, kind::TTS_AUDIO,
            kind::TTS_END, kind::ERROR, kind::PONG,
        ];
        for (i, a) in todos.iter().enumerate() {
            for b in &todos[i + 1..] {
                assert_ne!(a, b, "dos tramas comparten el tipo {a:#04x}");
            }
        }
    }

    #[test]
    fn decodificar_separa_tipo_y_carga() {
        let frame = decode(&[kind::TTS_AUDIO, 1, 2, 3]).unwrap();
        assert_eq!(frame.kind, kind::TTS_AUDIO);
        assert_eq!(frame.payload, &[1, 2, 3]);
        assert_eq!(frame.name(), "TTS_AUDIO");
    }

    #[test]
    fn una_trama_sin_carga_es_valida() {
        let frame = decode(&[kind::TTS_END]).unwrap();
        assert_eq!(frame.payload, &[] as &[u8]);
    }

    #[test]
    fn una_trama_vacia_es_error() {
        assert_eq!(decode(&[]), Err(ProtocolError::Empty));
    }

    #[test]
    fn una_trama_gigante_es_error() {
        let grande = [0u8; MAX_FRAME_BYTES + 1];
        assert_eq!(decode(&grande), Err(ProtocolError::TooLong(MAX_FRAME_BYTES + 1)));
    }

    /// El límite es inclusivo, igual que en el servidor: exactamente el máximo pasa.
    #[test]
    fn el_maximo_exacto_se_acepta() {
        let justo = [0u8; MAX_FRAME_BYTES];
        assert!(decode(&justo).is_ok());
    }

    #[test]
    fn el_hello_es_json_valido_y_completo() {
        let buf = hello("luka-speaker", "0.1.0", 16_000, 1);
        let frame = decode(buf.as_frame().unwrap()).unwrap();
        assert_eq!(frame.kind, kind::HELLO);
        assert_eq!(
            frame.as_str().unwrap(),
            r#"{"device":"luka-speaker","fw":"0.1.0","sample_rate":16000,"channels":1}"#
        );
    }

    #[test]
    fn el_hello_escapa_el_nombre_del_dispositivo() {
        let buf = hello(r#"sa"lón\cocina"#, "0.1.0", 16_000, 1);
        let frame = decode(buf.as_frame().unwrap()).unwrap();
        assert!(frame.as_str().unwrap().contains(r#""device":"sa\"lón\\cocina""#));
    }

    /// Si el nombre no cupiera, la trama no debe salir a medias: un JSON truncado
    /// es peor que no mandar nada, porque el servidor lo rechaza sin explicar nada.
    #[test]
    fn un_hello_que_no_cabe_no_produce_trama() {
        let relleno = [b'x'; HELLO_CAP];
        let nombre_absurdo = core::str::from_utf8(&relleno).unwrap();
        let buf = hello(nombre_absurdo, "0.1.0", 16_000, 1);
        assert!(buf.as_frame().is_none());
    }

    #[test]
    fn las_tramas_sin_carga_son_de_un_byte() {
        for k in [kind::END, kind::CANCEL, kind::PING] {
            assert_eq!(bare(k).as_frame().unwrap(), &[k]);
        }
    }

    #[test]
    fn el_audio_se_encuadra_sin_copias_extra() {
        let mut out = [0u8; 8];
        let pcm = [0xAA, 0xBB, 0xCC];
        let frame = audio_into(&mut out, &pcm).unwrap();
        assert_eq!(frame, &[kind::AUDIO, 0xAA, 0xBB, 0xCC]);
    }

    #[test]
    fn el_audio_que_no_cabe_se_rechaza() {
        let mut out = [0u8; 3];
        assert!(audio_into(&mut out, &[1, 2, 3]).is_none());
    }

    #[test]
    fn se_lee_el_estado_que_manda_el_servidor() {
        let payload = br#"{"state":"listening"}"#;
        assert_eq!(json_str_field(payload, "state"), Some(state::LISTENING));
    }

    #[test]
    fn se_lee_un_campo_que_no_es_el_primero() {
        let payload = br#"{"code":"no_speech","message":"No te he entendido."}"#;
        assert_eq!(json_str_field(payload, "code"), Some("no_speech"));
        assert_eq!(json_str_field(payload, "message"), Some("No te he entendido."));
    }

    #[test]
    fn un_campo_ausente_da_none() {
        assert_eq!(json_str_field(br#"{"state":"idle"}"#, "code"), None);
    }

    /// Antes que entregar una cadena mal desescapada, no entregar nada.
    #[test]
    fn un_valor_con_escapes_se_rechaza_en_vez_de_devolverse_mal() {
        let payload = br#"{"text":"dijo \"hola\""}"#;
        assert_eq!(json_str_field(payload, "text"), None);
    }

    #[test]
    fn un_campo_que_no_es_cadena_da_none() {
        assert_eq!(json_str_field(br#"{"sample_rate":16000}"#, "sample_rate"), None);
    }

    /// Que una clave aparezca dentro del valor de otra no debe confundir la búsqueda.
    #[test]
    fn una_clave_camuflada_en_un_valor_no_confunde() {
        let payload = br#"{"message":"state","state":"thinking"}"#;
        assert_eq!(json_str_field(payload, "state"), Some(state::THINKING));
    }

    #[test]
    fn el_json_roto_no_revienta() {
        for basura in [&b"{"[..], &b"{\"state\""[..], &b"{\"state\":"[..], &b"no soy json"[..], &[0xff, 0xfe][..]] {
            assert_eq!(json_str_field(basura, "state"), None);
        }
    }
}
