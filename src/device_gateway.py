"""Pasarela para dispositivos satélite (altavoz ESP32-S3).

Un dispositivo abre un WebSocket, manda el audio de su micrófono, y recibe de
vuelta la transcripción, la respuesta de Luka y su voz sintetizada. Reutiliza
tal cual el `STTEngine` y el `AssistantService` que ya existen: esto es una capa
de transporte, no un segundo cerebro.

Todo es **aditivo**: sin ningún dispositivo conectado nada de este módulo se
ejecuta, y el asistente se comporta exactamente igual que antes.
"""
from __future__ import annotations

import asyncio
import logging
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from src import device_protocol as proto
from src.config import settings

logger = logging.getLogger(__name__)

# Formato del enlace con el dispositivo. 16 kHz mono PCM16 en ambos sentidos: es
# lo que quiere Whisper y lo que el firmware captura de forma nativa, así que no
# hay conversiones intermedias en la ruta de subida.
LINK_SAMPLE_RATE = 16_000
LINK_CHANNELS = 1

# Tope de audio por turno. A 16 kHz PCM16 son 32 KB/s, así que 30 s son ~960 KB.
# Protege contra un dispositivo que se quede mandando audio y nunca envíe END.
MAX_TURN_SECONDS = 30
MAX_TURN_BYTES = LINK_SAMPLE_RATE * 2 * MAX_TURN_SECONDS

# Trozos en los que se parte el audio de bajada. 20 ms a 16 kHz = 640 bytes; se
# mandan bloques de ~100 ms para no saturar de tramas pequeñas el WebSocket.
DOWNLINK_CHUNK_SAMPLES = LINK_SAMPLE_RATE // 10


def pcm16_to_wav(pcm: bytes, path: Path, sample_rate: int = LINK_SAMPLE_RATE) -> None:
    """Escribe PCM16 crudo como un .wav, que es lo que espera el STT."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(LINK_CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def float_to_link_pcm(audio: np.ndarray, source_rate: int) -> bytes:
    """Convierte el audio del TTS (float32) al PCM16 de 16 kHz del enlace.

    Kokoro sintetiza a 24 kHz en coma flotante, pero el dispositivo tiene su I2S
    configurado a 16 kHz. Se remuestrea **aquí**, en el PC, a propósito: el ESP32
    tiene cosas mejores que hacer con sus ciclos, y aquí sobra CPU.

    Interpolación lineal en vez de un remuestreo con filtro: para voz a estas
    frecuencias la diferencia audible es nula y evita arrastrar SciPy como
    dependencia solo para esto.
    """
    if audio.size == 0:
        return b""

    audio = np.asarray(audio, dtype=np.float32).reshape(-1)

    if source_rate != LINK_SAMPLE_RATE:
        n_out = int(round(audio.size * LINK_SAMPLE_RATE / source_rate))
        if n_out <= 0:
            return b""
        # `endpoint=False` evita repetir la última muestra en cada bloque, que en
        # audio troceado se oiría como un chasquido periódico.
        positions = np.linspace(0, audio.size, n_out, endpoint=False)
        audio = np.interp(positions, np.arange(audio.size), audio).astype(np.float32)

    # Recorta antes de escalar: si el TTS se pasa de 1.0, sin esto daría la vuelta
    # al rango entero y sonaría a distorsión brutal en vez de a saturación suave.
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


class DeviceSession:
    """Una sesión con un dispositivo satélite conectado."""

    def __init__(self, websocket, app_state, name: str = "dispositivo") -> None:
        self.ws = websocket
        self.state = app_state
        self.name = name
        self.connected_at = time.monotonic()
        self._audio = bytearray()
        # Última telemetría recibida. Sirve de doble propósito: diagnóstico
        # (¿va bien la WiFi? ¿se está comiendo la memoria?) y latido, porque su
        # ausencia prolongada delata un enlace muerto que TCP aún no ha notado.
        self._telemetry: dict = {}
        self._telemetry_at: Optional[float] = None
        self._turn: Optional[asyncio.Task] = None
        # Serializa los envíos: el sink del TTS y el bucle de recepción escriben
        # los dos en el mismo WebSocket, y entrelazar tramas lo corrompería.
        self._send_lock = asyncio.Lock()
        self._closed = False
        # Quien esté esperando una foto. `None` si nadie la pidió.
        self._image_waiter: Optional[asyncio.Future] = None

    # ---------------------------------------------------------------- envío
    async def send(self, frame: bytes) -> None:
        """Envía una trama ya codificada, tolerando que el enlace se haya caído."""
        if self._closed:
            return
        try:
            async with self._send_lock:
                await self.ws.send_bytes(frame)
        except Exception as e:  # noqa: BLE001 — desconexión durante el envío
            logger.info(f"[{self.name}] enlace perdido al enviar: {e}")
            self._closed = True

    async def send_tts_audio(self, audio: np.ndarray, source_rate: int) -> None:
        """*Sink* de audio: reenvía al dispositivo lo que sintetiza el TTS."""
        pcm = float_to_link_pcm(audio, source_rate)
        for i in range(0, len(pcm), DOWNLINK_CHUNK_SAMPLES * 2):
            await self.send(proto.encode(proto.TTS_AUDIO, pcm[i:i + DOWNLINK_CHUNK_SAMPLES * 2]))

    # ------------------------------------------------------------- recepción
    async def run(self) -> None:
        """Bucle principal: recibe tramas hasta que el dispositivo se va."""
        logger.info(f"[{self.name}] conectado")
        await self.send(proto.state(proto.STATE_IDLE))
        try:
            while True:
                data = await self.ws.receive_bytes()
                try:
                    frame = proto.decode(data)
                except proto.ProtocolError as e:
                    logger.warning(f"[{self.name}] trama inválida: {e}")
                    await self.send(proto.error("bad_frame", str(e)))
                    continue
                await self._handle(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — desconexión es lo normal aquí
            logger.info(f"[{self.name}] desconectado: {type(e).__name__}")
        finally:
            self._closed = True
            await self._cancel_turn()

    async def _handle(self, frame: proto.Frame) -> None:
        if frame.kind == proto.AUDIO:
            # Descarta en vez de crecer sin límite: el audio viejo de un turno que
            # nunca se cerró no vale nada, y la memoria del servidor sí.
            if len(self._audio) + len(frame.payload) > MAX_TURN_BYTES:
                logger.warning(f"[{self.name}] turno de más de {MAX_TURN_SECONDS}s; se corta")
                await self._process_turn()
                return
            self._audio.extend(frame.payload)

        elif frame.kind == proto.END:
            await self._process_turn()

        elif frame.kind == proto.CANCEL:
            logger.info(f"[{self.name}] turno cancelado por el dispositivo")
            self._audio.clear()
            await self._cancel_turn()
            await self.send(proto.state(proto.STATE_IDLE))

        elif frame.kind == proto.IMAGE:
            await self._handle_image(frame.payload)

        elif frame.kind == proto.PING:
            await self.send(proto.encode(proto.PONG))

        elif frame.kind == proto.TELEMETRY:
            self._telemetry = frame.json()
            self._telemetry_at = time.monotonic()
            logger.debug(f"[{self.name}] telemetría: {self._telemetry}")
            # Se responde igual que a un PING: al dispositivo le sirve para saber
            # que el enlace funciona en los dos sentidos, no solo de subida.
            await self.send(proto.encode(proto.PONG))

        elif frame.kind == proto.HELLO:
            info = frame.json()
            self.name = str(info.get("device") or self.name)
            logger.info(f"[{self.name}] HELLO: {info}")
            await self.send(proto.state(proto.STATE_IDLE))

        else:
            logger.warning(f"[{self.name}] trama inesperada: {frame.name}")

    # ----------------------------------------------------------------- cámara
    async def request_capture(self, timeout: float = 8.0) -> Optional[Path]:
        """Pide una foto al dispositivo y espera a que llegue.

        Devuelve la ruta del JPEG, o `None` si no llegó a tiempo. El plazo cubre
        de sobra lo que tarda: capturar y comprimir son ~100 ms en la placa y
        subir 6 kB por el enlace, un suspiro. Si se agota, es que el dispositivo
        no tiene cámara o no está.
        """
        # Se renueva por petición: así una foto vieja que llegue tarde no se
        # cuela como respuesta a la siguiente.
        self._image_waiter = asyncio.get_running_loop().create_future()
        await self.send(proto.encode(proto.CAPTURE))
        try:
            return await asyncio.wait_for(self._image_waiter, timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] la foto no llegó en {timeout}s")
            return None
        finally:
            self._image_waiter = None

    async def _handle_image(self, payload: bytes) -> None:
        # Un JPEG empieza SIEMPRE por FF D8. Comprobarlo distingue "hay imagen"
        # de "hay N bytes de basura del tamaño correcto", que desde el log se ven
        # exactamente igual.
        if len(payload) < 4 or payload[0] != 0xFF or payload[1] != 0xD8:
            logger.warning(f"[{self.name}] IMAGE de {len(payload)} B que no es un JPEG")
            return

        path = settings.TEMP_DIR / f"device_cam_{int(time.time() * 1000)}.jpg"
        path.write_bytes(payload)
        logger.info(f"[{self.name}] foto recibida: {len(payload)} B -> {path}")

        waiter = self._image_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(path)
        else:
            # Nadie la esperaba: se guarda igual, pero se dice, porque significa
            # que llegó tarde o que alguien pidió una foto y se fue.
            logger.info(f"[{self.name}] la foto llegó sin que nadie la esperara")

    # ------------------------------------------------------------------ turno
    async def _cancel_turn(self) -> None:
        """Aborta el turno en curso **y la voz que ya se estaba sintetizando**.

        Cancelar `self._turn` no basta y era un fallo real: los workers de
        síntesis y de reproducción no viven en esta tarea, viven en el
        `AssistantService`. Sin pararlos, el TTS de la respuesta abortada seguía
        generando audio y empujándolo por el *sink*, que lo mandaba al
        dispositivo **ya dentro del turno siguiente**: Luka contestaba a la
        pregunta anterior encima de la nueva.
        """
        if self._turn and not self._turn.done():
            self._turn.cancel()
            try:
                await self._turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await self.state.assistant_service.cancel_audio_tasks()

    async def _process_turn(self) -> None:
        """Cierra el turno de habla y lanza el procesamiento."""
        pcm, self._audio = bytes(self._audio), bytearray()
        if len(pcm) < LINK_SAMPLE_RATE:  # menos de medio segundo: un roce, no voz
            logger.info(f"[{self.name}] turno demasiado corto ({len(pcm)} bytes); ignorado")
            await self.send(proto.state(proto.STATE_IDLE))
            return
        await self._cancel_turn()
        self._turn = asyncio.create_task(self._run_turn(pcm))

    async def _run_turn(self, pcm: bytes) -> None:
        """STT → asistente → TTS, reenviando el progreso al dispositivo."""
        wav_path = settings.TEMP_DIR / f"device_{int(time.time() * 1000)}.wav"
        try:
            await self.send(proto.state(proto.STATE_THINKING))
            pcm16_to_wav(pcm, wav_path)

            # `transcribe` borra el fichero que se le pasa, así que el temporal es suyo.
            text = await self.state.stt_engine.transcribe(wav_path)
            if not text or len(text.strip()) < 2:
                logger.info(f"[{self.name}] no se reconoció voz")
                await self.send(proto.error("no_speech", "No te he entendido."))
                await self.send(proto.state(proto.STATE_IDLE))
                return

            logger.info(f"[{self.name}] transcrito: {text}")
            await self.send(proto.encode_json(proto.TRANSCRIPT, text=text))
            # Que la GUI del PC también refleje lo que se dijo por el dispositivo.
            self.state.last_user_transcription = text
            self.state.last_transcription_timestamp = asyncio.get_event_loop().time()

            await self.send(proto.state(proto.STATE_SPEAKING))
            reply = ""
            async for chunk in self.state.assistant_service.process_transcription_stream(
                text=text,
                conversation_history=self.state.conversation_history,
                sink_id=self.state.audio_manager.default_sink,
                max_history=settings.MAX_HISTORY,
            ):
                reply += chunk

            # El razonamiento no viaja al dispositivo. Que `assistant_service` ya
            # lo filtre para el TTS no cubre esto: son dos cosas distintas, la
            # voz y el texto de la trama, y el stream que se acumula aquí es el
            # crudo. Sin este filtro la REPLY llega con el <think> entero.
            await self.send(proto.encode_json(
                proto.REPLY,
                text=self.state.assistant_service.without_reasoning(reply).strip(),
            ))
            # Esperar al TTS para que TTS_END signifique de verdad "ya no viene más
            # audio", y el dispositivo pueda cerrar su reproducción sin cortarse.
            await self.state.assistant_service.wait_for_tts_complete()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] error procesando el turno: {e}", exc_info=True)
            await self.send(proto.error("turn_failed", str(e)))
        finally:
            wav_path.unlink(missing_ok=True)
            await self.send(proto.encode(proto.TTS_END))
            await self.send(proto.state(proto.STATE_IDLE))


class DeviceManager:
    """Registro del dispositivo conectado y puente con el `AssistantService`.

    De momento admite un dispositivo a la vez, que es lo que hay. La interfaz no
    lo da por supuesto, para que admitir varios luego no obligue a rehacer nada.
    """

    def __init__(self) -> None:
        self.session: Optional[DeviceSession] = None

    @property
    def connected(self) -> bool:
        return self.session is not None and not self.session._closed

    def attach(self, session: DeviceSession, assistant_service) -> None:
        """Registra la sesión y engancha el *sink* de audio del TTS."""
        self.session = session
        assistant_service.audio_sink = self._sink
        # Con dispositivo presente, por defecto suena en los dos sitios; el usuario
        # puede cambiarlo en caliente desde /device/audio-sink.
        assistant_service.audio_target = "both"
        logger.info("Dispositivo enganchado; salida de audio: both (PC + dispositivo)")

    def detach(self, session: DeviceSession, assistant_service) -> None:
        """Desengancha y **restaura el comportamiento original** (solo PC).

        Solo si la sesión que se va es la que está registrada. Sin esa
        comprobación había una carrera con consecuencias mudas: al reiniciarse el
        dispositivo, su sesión nueva abre el WebSocket **antes** de que el
        servidor procese el cierre de la vieja, y entonces el `finally` de la
        vieja borraba el `audio_sink` de la que acababa de engancharse.

            12:46:16  enganchado; salida de audio: both     <- sesión NUEVA
            12:46:22  desconectado; salida de audio: pc     <- la VIEJA lo pisa
            12:46:29  Sintetizando: 'Son las 12:'           <- habla, pero al PC

        A partir de ahí el dispositivo se queda mudo para siempre, sin un solo
        error en ningún log: el turno se procesa entero, se sintetiza la voz y se
        reproduce, pero el sink al que iría ya no existe. Desde la placa se ve
        como "entra en Speaking y no suena nada".
        """
        if self.session is not session:
            logger.info("Se cerró una sesión antigua; el dispositivo actual sigue enganchado")
            return
        self.session = None
        assistant_service.audio_sink = None
        assistant_service.audio_target = "pc"
        logger.info("Dispositivo desconectado; salida de audio: pc")

    async def _sink(self, audio: np.ndarray, source_rate: int) -> None:
        if self.session is not None:
            await self.session.send_tts_audio(audio, source_rate)

    def status(self) -> dict:
        """Resumen para /status."""
        if not self.connected or self.session is None:
            return {"connected": False}
        info = {
            "connected": True,
            "name": self.session.name,
            "uptime_s": round(time.monotonic() - self.session.connected_at, 1),
        }
        if self.session._telemetry:
            info["telemetry"] = self.session._telemetry
            # Cuánto hace que llegó: una telemetría vieja significa que el
            # dispositivo dejó de latir aunque el socket siga aparentando vida.
            if self.session._telemetry_at is not None:
                info["telemetry_age_s"] = round(
                    time.monotonic() - self.session._telemetry_at, 1
                )
        return info


# Registro único del proceso, como el resto de estado global del asistente.
manager = DeviceManager()
