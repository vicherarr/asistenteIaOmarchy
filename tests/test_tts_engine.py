"""Tests para src/tts_engine.py"""

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import numpy as np
import pytest

from src.tts_engine import TTSEngine, TTSError


@pytest.fixture
def engine():
    # Evitamos que intente inicializar Kokoro de verdad si no está instalado
    with patch('src.tts_engine.TTSEngine._init_kokoro'):
        return TTSEngine()


@pytest.mark.asyncio
async def test_speak_empty_text(engine):
    result = await engine.speak("")
    assert result is None


@pytest.mark.asyncio
async def test_play_audio_with_bluetooth_sink(engine):
    with patch('asyncio.create_subprocess_exec') as mock_exec:
        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_exec.return_value = mock_process
        
        await engine._play_audio("/tmp/test.wav", sink_id="45")
    
    # Verificamos que se usó paplay con el dispositivo correcto
    args = mock_exec.call_args[0]
    assert "paplay" in args
    assert "--device" in args
    assert "45" in args


@pytest.mark.asyncio
async def test_play_audio_without_bluetooth_sink(engine):
    with patch('asyncio.create_subprocess_exec') as mock_exec:
        mock_process = AsyncMock()
        mock_process.wait.return_value = 0
        mock_exec.return_value = mock_process
        
        # Para archivos wav sin sink usa paplay por defecto en nuestra nueva implementación
        await engine._play_audio("/tmp/test.wav", sink_id=None)
    
    args = mock_exec.call_args[0]
    assert "paplay" in args


@pytest.mark.asyncio
async def test_stop(engine):
    mock_process = AsyncMock()
    mock_process.returncode = None
    engine._playback_process = mock_process
    
    engine.stop()
    mock_process.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_speak_gtts_fallback(engine):
    # Simulamos que Kokoro no está disponible
    engine._kokoro_pipeline = None
    
    with patch('src.tts_engine.TTSEngine._speak_gtts', new_callable=AsyncMock) as mock_gtts:
        mock_gtts.return_value = "/tmp/gtts.mp3"
        await engine.speak("Hola")
        mock_gtts.assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_only_no_kokoro(engine):
    """synthesize_only devuelve None si Kokoro no está disponible."""
    engine._kokoro_pipeline = None
    result = await engine.synthesize_only("Hola")
    assert result is None


@pytest.mark.asyncio
async def test_synthesize_only_empty_text(engine):
    """synthesize_only devuelve None para texto vacío."""
    result = await engine.synthesize_only("")
    assert result is None


@pytest.mark.asyncio
async def test_synthesize_only_returns_numpy(engine):
    """synthesize_only genera un array numpy cuando Kokoro está disponible."""
    # Mock del pipeline de Kokoro
    mock_pipeline = MagicMock()
    # Simular generator que produce chunks de audio
    audio_chunk = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    mock_pipeline.return_value = [
        ("phonemes", "graphemes", audio_chunk),
    ]
    engine._kokoro_pipeline = mock_pipeline
    engine._is_playing = True  # Necesario para que el loop no se cancele

    result = await engine.synthesize_only("Hola")

    assert result is not None
    assert isinstance(result, np.ndarray)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_synthesize_only_cancellation(engine):
    """synthesize_only respeta _is_playing para cancelación."""
    mock_pipeline = MagicMock()
    audio_chunk = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    # Generator que produce muchos chunks pero se cancela
    def generator():
        engine._is_playing = False  # Simular cancelación inmediata
        yield ("p", "g", audio_chunk)
    mock_pipeline.return_value = generator()
    engine._kokoro_pipeline = mock_pipeline

    result = await engine.synthesize_only("Hola")
    # Debería ser None porque se canceló antes de procesar
    assert result is None


@pytest.mark.asyncio
async def test_play_audio_array_empty(engine):
    """play_audio_array no hace nada con arrays vacíos."""
    result = await engine.play_audio_array(np.array([]))
    # No debería lanzar excepción
    assert result is None


@pytest.mark.asyncio
async def test_play_audio_array_opens_persistent_stream(engine):
    """play_audio_array abre un OutputStream persistente la primera vez."""
    mock_stream = MagicMock()
    with patch('sounddevice.OutputStream') as mock_os:
        mock_os.return_value.__enter__ = MagicMock(return_value=mock_stream)
        mock_os.return_value.__exit__ = MagicMock(return_value=False)
        mock_stream.closed = False

        audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        await engine.play_audio_array(audio)

        # Verificar que se abrió el stream persistente
        assert engine._persistent_stream is not None


@pytest.mark.asyncio
async def test_play_audio_array_reuses_stream(engine):
    """play_audio_array reutiliza el stream persistente abierto."""
    mock_stream = MagicMock()
    mock_stream.closed = False
    engine._persistent_stream = mock_stream

    audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    await engine.play_audio_array(audio)

    # Verificar que se escribió al stream existente (no se abrió uno nuevo)
    mock_stream.write.assert_called_once()


@pytest.mark.asyncio
async def test_close_persistent_stream(engine):
    """close_persistent_stream cierra y limpia el stream."""
    mock_stream = MagicMock()
    mock_stream.closed = False   # como un stream de verdad recién abierto
    engine._persistent_stream = mock_stream

    engine.close_persistent_stream()

    mock_stream.stop.assert_called_once()
    mock_stream.close.assert_called_once()
    assert engine._persistent_stream is None


@pytest.mark.asyncio
async def test_tensor_to_numpy(engine):
    """_tensor_to_numpy convierte correctamente diferentes tipos de tensor."""
    # Test con objeto que tiene .cpu()
    mock_tensor = MagicMock()
    mock_tensor.cpu.return_value.numpy.return_value = np.array([1.0])
    result = engine._tensor_to_numpy(mock_tensor)
    assert isinstance(result, np.ndarray)

    # Test con objeto que tiene .numpy()
    mock_tensor2 = MagicMock()
    mock_tensor2.numpy.return_value = np.array([2.0])
    del mock_tensor2.cpu
    result2 = engine._tensor_to_numpy(mock_tensor2)
    assert isinstance(result2, np.ndarray)

    # Test con array numpy directo
    arr = np.array([3.0])
    result3 = engine._tensor_to_numpy(arr)
    assert np.array_equal(result3, arr)


# --- La carrera que provocaba el SIGSEGV ---------------------------------------------
# Los OutputStream de PortAudio no son thread-safe. Las escrituras van en un worker
# (asyncio.to_thread) y `stop()` llega desde el hilo del bucle de eventos al interrumpir.
# Cerrar el stream mientras otro hilo está dentro de write() libera lo que el bucle de
# ALSA sigue usando: core dump en snd_pcm_poll_descriptors_revents. Pasó en producción el
# 23/08/2026 — el proceso murió 4 s después de un /cancel y systemd lo reinició.

def test_cerrar_no_cierra_dos_veces(engine):
    """El doble cierre era la otra mitad del crash.

    `stop()` cerraba el stream de Kokoro y el bloque `with sd.OutputStream(...)` del
    worker lo cerraba otra vez al salir: dos Pa_CloseStream sobre el mismo handle.
    """
    stream = MagicMock()
    stream.closed = False

    engine._cerrar(stream)
    assert stream.close.call_count == 1

    stream.closed = True          # ya cerrado, como lo vería el segundo que llega
    engine._cerrar(stream)
    assert stream.close.call_count == 1, "cerró dos veces el mismo stream"


def test_cerrar_aguanta_none_y_errores(engine):
    """Cerrar nunca debe tumbar a quien llama: corre desde paths de cancelación."""
    engine._cerrar(None)
    roto = MagicMock()
    roto.closed = False
    roto.stop.side_effect = RuntimeError("PortAudio dice que no")
    engine._cerrar(roto)          # no debe propagar


@pytest.mark.asyncio
async def test_stop_no_cierra_mientras_otro_hilo_escribe(engine):
    """El núcleo del arreglo: `stop()` espera a que el que escribe salga.

    Se simula el worker cogiendo `_stream_lock` y escribiendo. Si `stop()` cerrara sin
    pedir el lock, cerraría por debajo — que es exactamente el segfault.
    """
    import threading

    stream = MagicMock()
    stream.closed = False
    engine._persistent_stream = stream

    dentro_de_write = threading.Event()
    puede_salir = threading.Event()
    cerrado_durante_la_escritura = []

    def worker():
        with engine._stream_lock:
            dentro_de_write.set()
            puede_salir.wait(timeout=5)
            # Si stop() hubiera cerrado por debajo, ya estaría cerrado aquí.
            cerrado_durante_la_escritura.append(stream.close.called)

    hilo = threading.Thread(target=worker)
    hilo.start()
    assert dentro_de_write.wait(timeout=5)

    # stop() desde "el bucle de eventos" mientras el worker tiene el lock.
    parada = threading.Thread(target=engine.stop)
    parada.start()

    # La señal de corte se levanta SIN esperar al lock: corta el audio ya.
    assert engine._stop_requested.wait(timeout=5)

    puede_salir.set()
    hilo.join(timeout=5)
    parada.join(timeout=5)

    assert cerrado_durante_la_escritura == [False], "cerró el stream con un hilo dentro"
    assert stream.close.called, "tras salir el worker, stop() sí debe cerrar"
    assert engine._persistent_stream is None


@pytest.mark.asyncio
async def test_stop_no_se_cuelga_si_nadie_suelta_el_lock(engine):
    """Prefiere dejar el stream abierto a bloquear el bucle de eventos o petar."""
    import threading, time

    stream = MagicMock()
    stream.closed = False
    engine._persistent_stream = stream
    engine._ESPERA_CIERRE = 0.2          # no hacer esperar al test

    engine._stream_lock.acquire()        # nadie lo va a soltar
    try:
        t0 = time.monotonic()
        threading.Thread(target=engine.stop).start()
        time.sleep(0.6)
        assert time.monotonic() - t0 < 3, "stop() se quedó colgado"
    finally:
        engine._stream_lock.release()

    assert not stream.close.called, "cerrar con alguien dentro es el core dump"
    assert engine._stop_requested.is_set(), "aun así tiene que cortar el audio"


@pytest.mark.asyncio
async def test_escritura_troceada_para_al_pedir_stop(engine):
    """El troceado es lo que acota cuánto tarda `stop()` en hacerse con el lock."""
    stream = MagicMock()
    engine._stop_requested.clear()

    # Audio de 10 bloques; se corta tras el tercero.
    audio = np.zeros(engine._BLOQUE_ESCRITURA * 10, dtype=np.float32)
    escrituras = []

    def write(datos):
        escrituras.append(len(datos))
        if len(escrituras) == 3:
            engine._stop_requested.set()

    stream.write.side_effect = write
    completo = engine._escribir_troceado(stream, audio)

    assert completo is False, "debe avisar de que se cortó a medias"
    assert len(escrituras) == 3, "siguió escribiendo tras pedirle parar"
    assert all(n <= engine._BLOQUE_ESCRITURA for n in escrituras)


@pytest.mark.asyncio
async def test_rearm_deja_hablar_otra_vez(engine):
    """La señal de corte es pegajosa: sin rearmar, el TTS quedaría mudo para siempre."""
    engine.stop()
    assert engine._stop_requested.is_set()

    stream = MagicMock()
    stream.closed = False
    engine._persistent_stream = stream
    await engine.play_audio_array(np.zeros(128, dtype=np.float32))
    assert not stream.write.called, "no debe sonar nada del turno cancelado"

    engine.rearm()
    assert not engine._stop_requested.is_set()
    assert engine._is_playing is True

    await engine.play_audio_array(np.zeros(128, dtype=np.float32))
    assert stream.write.called, "tras rearmar tiene que volver a sonar"
