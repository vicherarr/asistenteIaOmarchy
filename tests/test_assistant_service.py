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


@pytest.mark.skip(reason="Loop multimodal desactivado - analyze_screen no está registrado en self.tools")
@pytest.mark.asyncio
async def test_process_transcription_stream_multimodal_second_pass(service, mock_litert):
    """VER: Loop multimodal desactivado según CONTEXT.md sección 10.1 y 15.20.
    Este test se mantiene como referencia histórica pero está desactivado."""
    pass


# --- Fase 4.1: fallback "significativo" según el tipo de stream del motor --------

def test_meaningful_response_clean_stream_accepts_short():
    """Motor de stream limpio (ExLlama): respuestas cortas válidas se aceptan."""
    f = AssistantService._is_meaningful_response
    assert f("42", clean_stream=True) is True
    assert f("París", clean_stream=True) is True
    assert f("   ", clean_stream=True) is False   # vacío sigue sin valer
    assert f("", clean_stream=True) is False


def test_meaningful_response_litert_requires_length():
    """LiteRT (fuga tool calls): se exige longitud mínima para descartar residuos."""
    f = AssistantService._is_meaningful_response
    assert f("42", clean_stream=False) is False           # corto -> fallback (como antes)
    assert f("Una respuesta más larga y real.", clean_stream=False) is True


def test_engine_streams_clean_text_flags(monkeypatch):
    """ExLlama declara stream limpio; LiteRT no."""
    from src.engines import exllama_engine as ee
    monkeypatch.setattr(ee.ExLlamaEngine, "_ping", lambda self: False)
    assert ee.ExLlamaEngine().streams_clean_text is True
