"""Tests de integración para src/main.py (FastAPI)"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from src.main import app, AppState, get_app_state


@pytest.fixture
def mock_app_state():
    """Crea un mock completo del AppState para inyectar en los tests."""
    state = MagicMock(spec=AppState)
    state.litert_client = MagicMock()
    state.litert_client.engine = MagicMock()
    state.tts_engine = MagicMock()
    state.vision_tool = MagicMock()
    
    # audio_manager con métodos asíncronos
    state.audio_manager = MagicMock()
    state.audio_manager.get_status_summary = AsyncMock(return_value="Bluetooth OK")
    state.audio_manager.auto_configure_bluetooth = AsyncMock(return_value=("72", "70"))
    state.audio_manager.set_volume = AsyncMock(return_value=None)
    
    state.assistant_service = AsyncMock()
    state.audio_recorder = MagicMock()
    state.stt_engine = MagicMock()
    
    # Valores por defecto
    state.conversation_history = []
    state.processing = False
    state.is_recording = False
    state.current_task = None
    
    state.audio_recorder.is_recording = False
    
    return state


@pytest.fixture
def client(mock_app_state):
    """Configura el cliente de prueba con inyección de dependencias."""
    app.dependency_overrides[get_app_state] = lambda: mock_app_state
    # Parcheamos los motores pesados para que el lifespan no cargue modelos reales ni acceda a hardware
    with patch("src.main.LiteRTClient") as mock_litert_class, \
         patch("src.main.TTSEngine"), \
         patch("src.main.STTEngine"), \
         patch("src.main.AudioRecorder"), \
         patch("src.main.AudioManager") as mock_audio_class:
        
        # Simular que el motor se cargó correctamente en el mock de la clase
        mock_litert_class.return_value.engine = MagicMock()
        
        # Simular que auto_configure_bluetooth es asíncrono
        mock_audio_instance = mock_audio_class.return_value
        mock_audio_instance.auto_configure_bluetooth = AsyncMock(return_value=("mock_source", "mock_sink"))
        
        with TestClient(app) as c:
            yield c
    app.dependency_overrides.clear()


def test_status_endpoint(client, mock_app_state):
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert data["litert_connected"] is True
    assert data["bluetooth_audio"] == "Bluetooth OK"
    assert data["conversation_length"] == 0


def test_transcribe_empty(client):
    response = client.post("/transcribe", json={"text": ""})
    assert response.status_code == 400


def test_transcribe_success(client, mock_app_state):
    # Simulamos respuesta exitosa del servicio de negocio
    mock_app_state.assistant_service.process_transcription.return_value = {
        "status": "success",
        "response_text": "Abriendo Spotify",
        "commands_executed": 1,
        "audio_file": "/tmp/test.wav"
    }

    response = client.post("/transcribe", json={"text": "Abre Spotify"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["response_text"] == "Abriendo Spotify"
    assert data["commands_executed"] == 1


def test_transcribe_stream_success(client, mock_app_state):
    async def mock_stream(*args, **kwargs):
        yield "Abriendo "
        yield "Spotify"

    mock_app_state.assistant_service.process_transcription_stream = mock_stream

    response = client.post("/transcribe/stream", json={"text": "Abre Spotify"})

    assert response.status_code == 200
    assert response.text == "Abriendo Spotify"


def test_reset_conversation(client, mock_app_state):
    mock_app_state.conversation_history = [{"role": "user", "content": "test"}]
    
    response = client.post("/reset")
    assert response.status_code == 200
    # Verificamos que se vació el historial
    assert mock_app_state.conversation_history == []


def test_configure_audio(client, mock_app_state):
    response = client.post("/audio/configure")
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "72"
    assert data["sink"] == "70"


def test_cancel_processing(client, mock_app_state):
    # Simulamos una tarea en curso
    mock_task = MagicMock()
    mock_task.done.return_value = False
    mock_app_state.current_task = mock_task
    
    response = client.post("/cancel")
    assert response.status_code == 200
    assert response.json()["was_processing"] is True
    mock_task.cancel.assert_called_once()
    mock_app_state.tts_engine.stop.assert_called_once()


def test_health_endpoint(client, mock_app_state):
    with patch("src.main.asyncio.create_subprocess_exec") as mock_exec, \
         patch("src.main.asyncio.open_connection") as mock_connect:
        
        # Mock para whisper-cli
        mock_whisper = AsyncMock()
        mock_whisper.returncode = 0
        mock_whisper.wait = AsyncMock()
        
        # Mock para tmux
        mock_tmux = AsyncMock()
        mock_tmux.returncode = 0
        mock_tmux.wait = AsyncMock()
        
        mock_exec.side_effect = lambda *args, **kwargs: mock_whisper if args[0] == "whisper-cli" else mock_tmux
        
        # Mock para conexión CDP con wait_closed awaitable
        mock_writer = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_connect.return_value = (MagicMock(), mock_writer)
        
        mock_app_state.tts_engine._kokoro_pipeline = MagicMock()
        
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["litert"] is True
        assert data["whisper"] is True
        assert data["kokoro"] is True
        assert data["tmux"] is True
        assert data["cdp"] is True
        assert data["version"] == "1.0.0"


def test_health_endpoint_components_down(client, mock_app_state):
    with patch("src.main.asyncio.create_subprocess_exec", side_effect=FileNotFoundError), \
         patch("src.main.asyncio.open_connection", side_effect=ConnectionRefusedError):
        
        mock_app_state.litert_client.engine = None
        mock_app_state.tts_engine._kokoro_pipeline = None
        
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["litert"] is False
        assert data["whisper"] is False
        assert data["kokoro"] is False
        assert data["tmux"] is False
        assert data["cdp"] is False


def test_rate_limit_exceeded(client, mock_app_state):
    """Verifica que el RateLimiter bloquea requests excesivos."""
    from src.main import RateLimiter
    
    limiter = RateLimiter(max_requests=3, window_seconds=1.0)
    
    # 3 requests permitidos
    assert limiter.is_allowed("127.0.0.1") is True
    assert limiter.is_allowed("127.0.0.1") is True
    assert limiter.is_allowed("127.0.0.1") is True
    
    # 4º request bloqueado
    assert limiter.is_allowed("127.0.0.1") is False
    
    # Cleanup funciona
    limiter.cleanup()


def test_vad_silence_callback_triggers_local_toggle(client, mock_app_state):
    """Verifica que el callback de VAD de silencio prolongado invoque localmente toggle_listen."""
    # 1. Simular inicio de grabación (el asistente no está grabando al inicio)
    mock_app_state.is_recording = False
    mock_app_state.audio_recorder.is_recording = False
    
    # 2. Hacer POST a /listen/toggle para iniciar la grabación
    response = client.post("/listen/toggle")
    assert response.status_code == 200
    assert response.json()["status"] == "listening"
    
    # 3. Extraer el callback de silencio pasado a start_recording
    mock_app_state.audio_recorder.start_recording.assert_called_once()
    _, kwargs = mock_app_state.audio_recorder.start_recording.call_args
    on_silence_callback = kwargs.get("on_silence_callback")
    assert on_silence_callback is not None
    
    # 4. Simular que el VAD detecta silencio ejecutando el callback
    # Marcamos is_recording=True para simular el estado de grabación activa cuando se dispara
    mock_app_state.is_recording = True
    on_silence_callback()
    
    # 5. Verificar que se detiene la grabación localmente de forma asíncrona
    mock_app_state.audio_recorder.stop_recording.assert_called_once()
