"""Tests para src/assistant_service.py"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.assistant_service import AssistantService
from src.schema import ChatMessage


@pytest.fixture
def mock_litert():
    client = MagicMock()
    client.engine = MagicMock()
    client.chat_stream = MagicMock()
    return client


@pytest.fixture
def mock_tts():
    engine = MagicMock()
    engine.synthesize_only = AsyncMock()
    engine.play_audio_array = AsyncMock()
    engine.close_persistent_stream = MagicMock()
    engine.stop = MagicMock()
    return engine


@pytest.fixture
def mock_stt():
    engine = MagicMock()
    engine.transcribe = AsyncMock(return_value="texto transcrito")
    return engine


@pytest.fixture
def service(mock_litert, mock_tts, mock_stt):
    return AssistantService(
        litert_client=mock_litert,
        tts_engine=mock_tts,
        stt_engine=mock_stt,
    )


def test_extract_sentences_basic(service):
    """Extrae frases por puntuación básica."""
    text = "Hola mundo. ¿Cómo estás? ¡Bien! Gracias: adiós."
    sentences, remaining = service._extract_sentences(text)
    
    # ":" seguido de espacio también corta (5 frases)
    assert len(sentences) == 5
    assert sentences[0] == "Hola mundo."
    assert sentences[1] == "¿Cómo estás?"
    assert sentences[2] == "¡Bien!"
    assert sentences[3] == "Gracias:"


def test_extract_sentences_with_newlines(service):
    """Extrae frases por saltos de línea."""
    text = "Primera línea\nSegunda línea\n"
    sentences, remaining = service._extract_sentences(text)
    
    assert len(sentences) == 2


def test_extract_sentences_protects_decimals(service):
    """No corta decimales como frases separadas."""
    text = "El precio es 3.14 euros."
    sentences, remaining = service._extract_sentences(text)
    
    # "3.14" no debería separarse
    assert any("3.14" in s for s in sentences)


def test_extract_sentences_cuts_by_comma_when_long(service):
    """Corta por coma cuando el buffer supera 80 caracteres."""
    # Buffer largo (>80 chars) → debe cortar por coma
    text = "Esto es una frase muy larga que supera los ochenta caracteres, y por lo tanto debería cortarse por la coma."
    assert len(text) > 80
    sentences, remaining = service._extract_sentences(text)
    
    # Debería haber cortado por la coma
    assert len(sentences) >= 1
    assert any("," in s for s in sentences)


def test_extract_sentences_no_comma_when_short(service):
    """NO corta por coma cuando el buffer es corto (<80 chars)."""
    text = "Hola, mundo."
    assert len(text) < 80
    sentences, remaining = service._extract_sentences(text)
    
    # No debería haber cortado por coma (solo una frase)
    assert len(sentences) == 1
    assert sentences[0] == "Hola, mundo."


def test_extract_sentences_remaining(service):
    """Devuelve el texto restante sin terminar."""
    text = "Frase completa. Texto incompleto"
    sentences, remaining = service._extract_sentences(text)
    
    assert len(sentences) == 1
    assert remaining == " Texto incompleto"


@pytest.mark.asyncio
async def test_synth_worker_processes_sentences(service, mock_tts):
    """El synth worker sintetiza frases y las encola como audio."""
    queue_text = asyncio.Queue()
    queue_audio = asyncio.Queue()
    
    # Encolar frases y señal de fin
    await queue_text.put("Hola mundo.")
    await queue_text.put("Adiós.")
    await queue_text.put(None)
    
    # Mock: synthesize_only devuelve un array numpy
    import numpy as np
    mock_tts.synthesize_only.return_value = np.array([0.1, 0.2])
    
    await service._synth_worker(queue_text, queue_audio)
    
    # Verificar que se sintetizaron ambas frases
    assert mock_tts.synthesize_only.call_count == 2
    
    # Verificar que se encolaron 2 arrays de audio y la señal None al final (total 3)
    assert queue_audio.qsize() == 3


@pytest.mark.asyncio
async def test_synth_worker_handles_error(service, mock_tts):
    """El synth worker continúa aunque una síntesis falle."""
    queue_text = asyncio.Queue()
    queue_audio = asyncio.Queue()
    
    await queue_text.put("Frase válida.")
    await queue_text.put("Frase con error.")
    await queue_text.put("Otra frase válida.")
    await queue_text.put(None)
    
    # Primera y tercera OK, segunda falla
    import numpy as np
    call_count = 0
    async def mock_synthesize(text):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise Exception("Error de síntesis")
        return np.array([0.1])
    
    mock_tts.synthesize_only = mock_synthesize
    
    await service._synth_worker(queue_text, queue_audio)
    
    # 2 arrays encolados y la señal None al final (total 3)
    assert queue_audio.qsize() == 3


@pytest.mark.asyncio
async def test_play_worker_reproduces_audio(service, mock_tts):
    """El play worker reproduce arrays de audio."""
    queue_audio = asyncio.Queue()
    
    import numpy as np
    await queue_audio.put(np.array([0.1, 0.2]))
    await queue_audio.put(np.array([0.3, 0.4]))
    await queue_audio.put(None)
    
    await service._play_worker(queue_audio)
    
    # Verificar que se reprodujeron ambos arrays
    assert mock_tts.play_audio_array.call_count == 2
    
    # Verificar que se cerró el stream persistente
    mock_tts.close_persistent_stream.assert_called_once()


@pytest.mark.asyncio
async def test_play_worker_handles_error(service, mock_tts):
    """El play worker continúa aunque una reproducción falle."""
    queue_audio = asyncio.Queue()
    
    import numpy as np
    await queue_audio.put(np.array([0.1]))
    await queue_audio.put(np.array([0.2]))  # Esta fallará
    await queue_audio.put(np.array([0.3]))
    await queue_audio.put(None)
    
    call_count = 0
    async def mock_play(audio):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise Exception("Error de reproducción")
    
    mock_tts.play_audio_array = mock_play
    
    # No debería lanzar excepción
    await service._play_worker(queue_audio)
    
    # Se intentaron reproducir los 3
    assert call_count == 3


@pytest.mark.asyncio
async def test_process_transcription_stream_multimodal_second_pass(service, mock_litert):
    """Verifica que si hay una captura de pantalla pendiente, se ejecuta la segunda pasada."""
    
    # 1. Configurar chat_stream para la primera y segunda pasada
    async def mock_chat_stream_1(*args, **kwargs):
        # Primera pasada: el modelo dice que va a analizar la pantalla
        yield "Tomando "
        yield "captura..."
        
    async def mock_chat_stream_2(*args, **kwargs):
        # Segunda pasada: el modelo analiza la pantalla real
        yield " Veo "
        yield "un error."
        
    # Usar side_effect para devolver diferentes generadores en cada llamada
    mock_litert.chat_stream.side_effect = [mock_chat_stream_1(), mock_chat_stream_2()]
    
    # 2. Configurar la captura pendiente simulada
    with patch("src.utils.get_pending_image", side_effect=["/tmp/screenshot.png", None]):
        history = []
        chunks = []
        
        async for chunk in service.process_transcription_stream("¿Qué hay en mi pantalla?", history):
            chunks.append(chunk)
            
        # 3. Validar resultados
        # Primera pasada chunks: "Tomando ", "captura..."
        # Anuncio de segunda pasada: "\n[Analizando imagen de pantalla...]\n"
        # Segunda pasada chunks: " Veo ", "un error."
        assert "Tomando " in chunks
        assert "captura..." in chunks
        assert "\n[Analizando imagen de pantalla...]\n" in chunks
        assert " Veo " in chunks
        assert "un error." in chunks
        
        # El historial debe tener la respuesta final concatenada
        assert len(history) == 2  # [User, Assistant]
        assert history[0].role == "user"
        assert history[1].role == "assistant"
        assert history[1].content == "Tomando captura...\n\n Veo un error."
        
        # chat_stream debe haber sido llamado dos veces
        assert mock_litert.chat_stream.call_count == 2
        
        # Verificar los argumentos de la segunda llamada: debe tener image_path y prompt del vision loop
        _, kwargs = mock_litert.chat_stream.call_args
        assert kwargs.get("image_path") == "/tmp/screenshot.png"
        assert "analyze_screen" in kwargs.get("prompt")
