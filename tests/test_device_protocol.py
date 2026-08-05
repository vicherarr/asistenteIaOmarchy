"""Tests del protocolo y las conversiones de audio de los dispositivos satélite.

Todo aquí es lógica pura, sin red ni hardware: un fallo de encuadre o de formato
de audio visto desde un ESP32 cuesta horas de diagnóstico, y aquí se caza en
milisegundos.
"""
import asyncio
import contextlib
import wave

import numpy as np
import pytest

from src import device_protocol as proto
from src.device_gateway import (
    LINK_SAMPLE_RATE,
    float_to_link_pcm,
    pcm16_to_wav,
)


class TestFraming:
    def test_ida_y_vuelta_de_una_trama_binaria(self):
        pcm = b"\x01\x02\x03\x04"
        frame = proto.decode(proto.encode(proto.AUDIO, pcm))
        assert frame.kind == proto.AUDIO
        assert frame.payload == pcm

    def test_ida_y_vuelta_de_una_trama_json(self):
        frame = proto.decode(proto.encode_json(proto.HELLO, device="luka-speaker", fw="0.1.0"))
        assert frame.kind == proto.HELLO
        assert frame.json() == {"device": "luka-speaker", "fw": "0.1.0"}

    def test_una_trama_sin_cuerpo_es_json_vacio(self):
        # Un dispositivo puede saludar sin decir nada de sí mismo; tratarlo como
        # {} es más útil que reventar, porque todo tiene defaults.
        assert proto.decode(proto.encode(proto.HELLO)).json() == {}

    def test_los_acentos_sobreviven(self):
        # El nombre del dispositivo y las transcripciones van en español.
        frame = proto.decode(proto.encode_json(proto.TRANSCRIPT, text="qué día más raro"))
        assert frame.json()["text"] == "qué día más raro"

    def test_las_tramas_del_servidor_tienen_el_bit_alto(self):
        # Convención que permite saber de un vistazo el sentido de una trama.
        for kind in (proto.STATE, proto.TRANSCRIPT, proto.REPLY,
                     proto.TTS_AUDIO, proto.TTS_END, proto.ERROR, proto.PONG):
            assert kind & 0x80, f"{proto.NAMES[kind]} debería tener el bit alto"
        for kind in (proto.HELLO, proto.AUDIO, proto.END, proto.CANCEL, proto.PING):
            assert not kind & 0x80, f"{proto.NAMES[kind]} no debería tener el bit alto"

    def test_no_hay_tipos_de_trama_duplicados(self):
        kinds = [proto.HELLO, proto.AUDIO, proto.END, proto.CANCEL, proto.PING,
                 proto.STATE, proto.TRANSCRIPT, proto.REPLY, proto.TTS_AUDIO,
                 proto.TTS_END, proto.ERROR, proto.PONG]
        assert len(kinds) == len(set(kinds))


class TestFramingErrores:
    def test_trama_vacia(self):
        with pytest.raises(proto.ProtocolError, match="vacía"):
            proto.decode(b"")

    def test_trama_demasiado_grande(self):
        # Un dispositivo defectuoso no debe poder hacer crecer la memoria del PC.
        with pytest.raises(proto.ProtocolError, match="máximo"):
            proto.decode(b"\x02" + b"\x00" * proto.MAX_FRAME_BYTES)

    def test_json_malformado(self):
        with pytest.raises(proto.ProtocolError, match="no es JSON"):
            proto.decode(proto.encode(proto.HELLO, b"{esto no es json")).json()

    def test_json_que_no_es_un_objeto(self):
        with pytest.raises(proto.ProtocolError, match="objeto JSON"):
            proto.decode(proto.encode(proto.HELLO, b"[1, 2, 3]")).json()

    def test_un_tipo_desconocido_no_revienta(self):
        # Un firmware más nuevo puede mandar tramas que este servidor no conoce:
        # debe poder ignorarlas, no caerse.
        frame = proto.decode(b"\x7f\x00")
        assert frame.name == "0x7f"


class TestConversionDeAudio:
    def test_pcm16_a_wav_conserva_formato_y_muestras(self, tmp_path):
        pcm = np.array([0, 1000, -1000, 32767, -32768], dtype="<i2").tobytes()
        path = tmp_path / "t.wav"
        pcm16_to_wav(pcm, path)

        with wave.open(str(path), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == LINK_SAMPLE_RATE
            assert w.readframes(w.getnframes()) == pcm

    def test_remuestreo_de_24k_a_16k(self):
        # Kokoro sintetiza a 24 kHz y el enlace va a 16 kHz: 2/3 de las muestras.
        audio = np.zeros(2400, dtype=np.float32)
        pcm = float_to_link_pcm(audio, 24_000)
        assert len(pcm) // 2 == 1600

    def test_sin_remuestreo_si_ya_esta_a_16k(self):
        audio = np.zeros(1600, dtype=np.float32)
        assert len(float_to_link_pcm(audio, LINK_SAMPLE_RATE)) // 2 == 1600

    def test_conserva_la_forma_de_onda(self):
        # Un tono de 100 Hz debe seguir siendo un tono tras remuestrear: se
        # comprueba por amplitud, que es lo que delataría un escalado roto.
        t = np.linspace(0, 1, 24_000, endpoint=False)
        audio = (0.5 * np.sin(2 * np.pi * 100 * t)).astype(np.float32)
        out = np.frombuffer(float_to_link_pcm(audio, 24_000), dtype="<i2")
        assert len(out) == 16_000
        assert 0.45 * 32767 < np.abs(out).max() <= 0.51 * 32767

    def test_satura_en_vez_de_dar_la_vuelta(self):
        # Sin el clip previo, un 1.5 daría la vuelta al rango entero y sonaría a
        # distorsión brutal en vez de a saturación suave.
        audio = np.array([1.5, -1.5, 0.0], dtype=np.float32)
        out = np.frombuffer(float_to_link_pcm(audio, LINK_SAMPLE_RATE), dtype="<i2")
        assert out[0] == 32767
        assert out[1] == -32767
        assert out[2] == 0

    def test_audio_vacio(self):
        assert float_to_link_pcm(np.array([], dtype=np.float32), 24_000) == b""

    def test_acepta_audio_bidimensional(self):
        # Kokoro puede devolver forma (n, 1); aplanarlo evita un error sutil en
        # el que el PCM saldría con el doble de longitud y sonaría acelerado.
        audio = np.zeros((1600, 1), dtype=np.float32)
        assert len(float_to_link_pcm(audio, LINK_SAMPLE_RATE)) // 2 == 1600


class FakeWebSocket:
    """WebSocket de mentira: entrega tramas de un guion y apunta lo que se envía.

    Al agotar el guion **se queda esperando**, no cierra. Es importante: un
    dispositivo real sigue conectado mientras el servidor procesa su turno, y si
    aquí se simulara una desconexión, la sesión cancelaría el turno (que es lo
    correcto en producción) y el test mediría otra cosa distinta de la que cree.
    """

    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []

    @property
    def drained(self) -> bool:
        return not self._incoming

    async def receive_bytes(self):
        if not self._incoming:
            await asyncio.sleep(3600)  # el cliente sigue ahí, callado
        return self._incoming.pop(0)

    async def send_bytes(self, data):
        self.sent.append(proto.decode(data))

    def kinds(self):
        return [f.kind for f in self.sent]

    def states(self):
        return [f.json()["state"] for f in self.sent if f.kind == proto.STATE]


async def drive(session, timeout: float = 3.0):
    """Ejecuta la sesión hasta que agota el guion y termina el turno, y la cierra.

    Devuelve el control cuando ya no queda nada por procesar, que es cuando las
    aserciones tienen sentido.
    """
    task = asyncio.create_task(session.run())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while loop.time() < deadline:
            await asyncio.sleep(0.01)
            turn_done = session._turn is None or session._turn.done()
            if session.ws.drained and turn_done:
                await asyncio.sleep(0.01)  # margen para las tramas de cierre
                if session._turn is None or session._turn.done():
                    return
        raise AssertionError("la sesión no terminó dentro del tiempo previsto")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _fake_state(transcription="hola luka", reply="Hola, dime."):
    """AppState mínimo con las piezas que toca la sesión."""
    from unittest.mock import AsyncMock, MagicMock

    state = MagicMock()
    state.stt_engine.transcribe = AsyncMock(return_value=transcription)
    state.audio_manager.default_sink = None
    state.conversation_history = []

    async def _stream(**_kwargs):
        yield reply

    state.assistant_service.process_transcription_stream = _stream
    state.assistant_service.wait_for_tts_complete = AsyncMock()
    return state


@pytest.mark.asyncio
class TestDeviceSession:
    async def test_un_turno_completo(self):
        from src.device_gateway import DeviceSession

        # Medio segundo de audio: justo por encima del umbral de "esto es un roce".
        audio = proto.encode(proto.AUDIO, b"\x00" * (LINK_SAMPLE_RATE + 100))
        ws = FakeWebSocket([
            proto.encode_json(proto.HELLO, device="luka-speaker"),
            audio,
            proto.encode(proto.END),
        ])
        session = DeviceSession(ws, _fake_state())
        await drive(session)

        kinds = ws.kinds()
        assert proto.TRANSCRIPT in kinds, "debería devolver lo que entendió"
        assert proto.REPLY in kinds, "debería devolver la respuesta de Luka"
        assert proto.TTS_END in kinds, "debería cerrar el audio del turno"
        # El último estado tiene que ser idle: si no, el anillo del dispositivo se
        # quedaría "pensando" para siempre.
        assert ws.states()[-1] == proto.STATE_IDLE

    async def test_audio_demasiado_corto_se_ignora(self):
        from src.device_gateway import DeviceSession

        ws = FakeWebSocket([
            proto.encode(proto.AUDIO, b"\x00" * 100),
            proto.encode(proto.END),
        ])
        state = _fake_state()
        session = DeviceSession(ws, state)
        await drive(session)

        state.stt_engine.transcribe.assert_not_called()
        assert proto.TRANSCRIPT not in ws.kinds()

    async def test_sin_voz_reconocida_avisa_y_vuelve_a_idle(self):
        from src.device_gateway import DeviceSession

        ws = FakeWebSocket([
            proto.encode(proto.AUDIO, b"\x00" * (LINK_SAMPLE_RATE + 100)),
            proto.encode(proto.END),
        ])
        session = DeviceSession(ws, _fake_state(transcription=""))
        await drive(session)

        errores = [f.json() for f in ws.sent if f.kind == proto.ERROR]
        assert errores and errores[0]["code"] == "no_speech"
        assert ws.states()[-1] == proto.STATE_IDLE

    async def test_ping_responde_pong(self):
        from src.device_gateway import DeviceSession

        ws = FakeWebSocket([proto.encode(proto.PING)])
        await drive(DeviceSession(ws, _fake_state()))
        assert proto.PONG in ws.kinds()

    async def test_una_trama_invalida_no_tumba_la_sesion(self):
        from src.device_gateway import DeviceSession

        ws = FakeWebSocket([b"", proto.encode(proto.PING)])
        await drive(DeviceSession(ws, _fake_state()))
        # Responde con ERROR pero sigue atendiendo: un byte perdido en la línea
        # no debe costar una reconexión entera.
        assert proto.ERROR in ws.kinds()
        assert proto.PONG in ws.kinds()

    async def test_el_audio_acumulado_se_limita(self):
        from src.device_gateway import DeviceSession, MAX_TURN_BYTES

        ws = FakeWebSocket([proto.encode(proto.AUDIO, b"\x00" * 32_000)] * 40)
        session = DeviceSession(ws, _fake_state())
        await drive(session)
        assert len(session._audio) <= MAX_TURN_BYTES


class TestGestorDeDispositivos:
    def test_al_desconectar_se_restaura_la_salida_por_el_pc(self):
        """Lo importante: que irse un dispositivo no deje al asistente mudo."""
        from unittest.mock import MagicMock

        from src.device_gateway import DeviceManager, DeviceSession

        service = MagicMock()
        service.audio_target = "pc"
        service.audio_sink = None

        manager = DeviceManager()
        manager.attach(DeviceSession(FakeWebSocket([]), MagicMock()), service)
        assert service.audio_target == "both"
        assert service.audio_sink is not None

        manager.detach(service)
        assert service.audio_target == "pc"
        assert service.audio_sink is None
        assert not manager.connected


class TestHelpers:
    def test_state_produce_json_valido(self):
        frame = proto.decode(proto.state(proto.STATE_LISTENING))
        assert frame.kind == proto.STATE
        assert frame.json() == {"state": "listening"}

    def test_error_lleva_codigo_y_mensaje(self):
        frame = proto.decode(proto.error("no_speech", "No te he entendido."))
        assert frame.kind == proto.ERROR
        assert frame.json() == {"code": "no_speech", "message": "No te he entendido."}
