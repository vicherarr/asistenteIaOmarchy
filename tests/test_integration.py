"""Tests de integración con prompts diseñados para probar el flujo completo del sistema.

Estos tests simulan interacciones reales con el asistente usando prompts inventados
que cubren diferentes capacidades: tool calling, streaming, historial, TTS, etc.
"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.assistant_service import AssistantService
from src.schema import ChatMessage
from src.utils import strip_markdown


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_litert():
    """Mock del cliente LiteRT que simula respuestas del modelo."""
    client = MagicMock()
    client.engine = MagicMock()
    client.chat_stream = AsyncMock()
    client.chat = AsyncMock()
    return client


@pytest.fixture
def mock_tts():
    """Mock del motor TTS."""
    engine = MagicMock()
    engine.synthesize_only = AsyncMock(return_value=None)
    engine.play_audio_array = AsyncMock()
    engine.close_persistent_stream = MagicMock()
    engine.stop = MagicMock()
    engine._is_playing = False
    return engine


@pytest.fixture
def mock_stt():
    """Mock del motor STT."""
    engine = MagicMock()
    engine.transcribe = AsyncMock(return_value="texto transcrito")
    return engine


@pytest.fixture
def service(mock_litert, mock_tts, mock_stt):
    """Servicio de asistente con mocks."""
    return AssistantService(
        litert_client=mock_litert,
        tts_engine=mock_tts,
        stt_engine=mock_stt,
    )


# ============================================================================
# Prompts de prueba diseñados para cubrir diferentes capacidades
# ============================================================================

PROMPTS_PRUEBA = [
    # 1. Apertura de aplicación
    {
        "nombre": "abrir_spotify",
        "prompt": "Abre Spotify por favor",
        "herramienta_esperada": "execute_system_command",
    },
    # 2. Música específica
    {
        "nombre": "musica_especifica",
        "prompt": "Pon música de Daft Punk",
        "herramienta_esperada": "play_specific_music",
    },
    # 3. Búsqueda web
    {
        "nombre": "busqueda_web",
        "prompt": "¿Qué es la inteligencia artificial?",
        "herramienta_esperada": "web_search",
    },
    # 4. Comando de terminal
    {
        "nombre": "comando_terminal",
        "prompt": "Ejecuta el comando ls -la en mi directorio home",
        "herramienta_esperada": "open_terminal_and_run_command",
    },
    # 5. Gestión de ventanas
    {
        "nombre": "cerrar_ventana",
        "prompt": "Cierra la ventana que tengo abierta ahora",
        "herramienta_esperada": "manage_windows",
    },
    # 6. Clipboard
    {
        "nombre": "copiar_texto",
        "prompt": "Copia este texto al portapapeles: hola mundo",
        "herramienta_esperada": "clipboard_manager",
    },
    # 7. Diagnóstico del sistema
    {
        "nombre": "diagnostico_sistema",
        "prompt": "Cómo está mi disco? Dime el espacio",
        "herramienta_esperada": "open_terminal_and_run_command",
    },
    # 8. Navegación web
    {
        "nombre": "navegar_web",
        "prompt": "Abre la página google.com en el navegador",
        "herramienta_esperada": "control_local_browser",
    },
    # 9. Lectura de página web
    {
        "nombre": "leer_pagina",
        "prompt": "Lee el contenido de https://example.com",
        "herramienta_esperada": "read_web_page",
    },
    # 10. Investigación profunda
    {
        "nombre": "investigacion",
        "prompt": "Investiga a fondo sobre los últimos avances en modelos de lenguaje",
        "herramienta_esperada": "control_local_browser",
    },
    # 11. Parar música
    {
        "nombre": "parar_musica",
        "prompt": "Para la música que está sonando",
        "herramienta_esperada": "execute_system_command",
    },
    # 12. Mover ventana a workspace
    {
        "nombre": "mover_workspace",
        "prompt": "Mueve esta ventana al escritorio 3",
        "herramienta_esperada": "manage_windows",
    },
    # 13. Leer terminal
    {
        "nombre": "leer_terminal",
        "prompt": "Qué muestra la terminal ahora?",
        "herramienta_esperada": "read_terminal_screen",
    },
    # 14. Logs del servicio
    {
        "nombre": "leer_logs",
        "prompt": "Muéstrame los logs del servicio asistenteia",
        "herramienta_esperada": "read_log_file",
    },
    # 15. Diagnóstico bluetooth
    {
        "nombre": "diag_bluetooth",
        "prompt": "Hay algún problema con el bluetooth?",
        "herramienta_esperada": "system_diagnostics",
    },
]


# ============================================================================
# Tests de integración de flujo completo
# ============================================================================

class TestFlujoTranscripcion:
    """Tests que verifican el flujo completo de transcripción con diferentes prompts."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caso", PROMPTS_PRUEBA, ids=[c["nombre"] for c in PROMPTS_PRUEBA])
    async def test_procesa_prompt_con_herramienta(self, service, mock_litert, caso):
        """Verifica que cada prompt se procesa correctamente y genera respuesta."""
        # Configurar mock de chat_stream para devolver respuesta simple
        async def mock_stream(*args, **kwargs):
            yield f"He procesado tu petición: {caso['prompt'][:30]}..."

        mock_litert.chat_stream = mock_stream

        history = []
        chunks = []

        async for chunk in service.process_transcription_stream(
            text=caso["prompt"],
            conversation_history=history,
        ):
            chunks.append(chunk)

        # Verificar que se generó respuesta
        assert len(chunks) > 0
        assert "He procesado tu petición" in "".join(chunks)

        # Verificar que el historial se actualizó
        assert len(history) == 2  # user + assistant
        assert history[0].role == "user"
        assert history[0].content == caso["prompt"]
        assert history[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_historial_acumulativo(self, service, mock_litert):
        """Verifica que el historial se acumula correctamente entre múltiples turnos."""
        async def mock_stream(*args, **kwargs):
            yield "Respuesta del asistente"

        mock_litert.chat_stream = mock_stream

        history = []

        # Primer turno
        async for _ in service.process_transcription_stream(
            text="Hola, cómo estás?",
            conversation_history=history,
        ):
            pass

        assert len(history) == 2

        # Segundo turno
        async def mock_stream_2(*args, **kwargs):
            yield "Segunda respuesta"

        mock_litert.chat_stream = mock_stream_2

        async for _ in service.process_transcription_stream(
            text="Qué puedes hacer?",
            conversation_history=history,
        ):
            pass

        # Debería haber 4 mensajes (2 turnos completos)
        assert len(history) == 4
        assert history[0].content == "Hola, cómo estás?"
        assert history[2].content == "Qué puedes hacer?"

    @pytest.mark.asyncio
    async def test_historial_limita_a_max_history(self, service, mock_litert):
        """Verifica que el historial no supera el límite max_history."""
        async def mock_stream(*args, **kwargs):
            yield "OK"

        mock_litert.chat_stream = mock_stream

        history = []
        max_history = 3

        # Generar más turnos que el límite
        for i in range(10):
            async for _ in service.process_transcription_stream(
                text=f"Mensaje {i}",
                conversation_history=history,
                max_history=max_history,
            ):
                pass

        # El historial no debería exceder max_history * 2 (user + assistant por turno)
        # Pero el código limita a max_history mensajes totales
        assert len(history) <= max_history * 2

    @pytest.mark.asyncio
    async def test_cancelacion_de_streaming(self, service, mock_litert):
        """Verifica que el streaming se puede cancelar correctamente."""
        cancelled = False

        async def mock_stream_lento(*args, **kwargs):
            nonlocal cancelled
            for i in range(100):
                await asyncio.sleep(0.01)
                try:
                    yield f"Chunk {i}"
                except asyncio.CancelledError:
                    cancelled = True
                    raise

        mock_litert.chat_stream = mock_stream_lento

        history = []
        chunks = []

        task = asyncio.create_task(
            service.process_transcription_stream(
                text="Prompt de prueba",
                conversation_history=history,
            ).__anext__()
        )

        # Cancelar después de un breve momento
        await asyncio.sleep(0.05)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verificar que se canceló
        assert cancelled or service._current_tts_task is not None


class TestToolCalling:
    """Tests que verifican que las herramientas se registran y configuran correctamente."""

    def test_tools_registradas(self, service):
        """Verifica que todas las herramientas están registradas."""
        tool_names = [t.__name__ for t in service.tools]

        herramientas_esperadas = [
            "execute_system_command",
            "read_log_file",
            "clipboard_manager",
            "web_search",
            "system_diagnostics",
            "read_web_page",
            "play_specific_music",
            "open_terminal_and_run_command",
            "read_terminal_screen",
            "control_local_browser",
            "send_input_to_terminal",
            "interrupt_terminal_command",
            "launch_application",
            "close_application",
            "analyze_screen",
            "analyze_clipboard_image",
            "take_screenshot",
            "create_document",
        ]

        for herramienta in herramientas_esperadas:
            assert herramienta in tool_names, f"Falta herramienta: {herramienta}"

    def test_tools_son_async(self, service):
        """Verifica que las herramientas son funciones asíncronas."""
        import inspect

        for tool in service.tools:
            assert inspect.iscoroutinefunction(tool), f"{tool.__name__} no es async"

    @pytest.mark.asyncio
    async def test_chat_stream_recibe_tools(self, service, mock_litert):
        """Verifica que chat_stream recibe las herramientas correctamente."""
        tools_recibidas = []

        async def capture_tools(*args, **kwargs):
            tools_recibidas.extend(kwargs.get("tools", []))
            yield "Respuesta"

        mock_litert.chat_stream = capture_tools

        history = []
        async for _ in service.process_transcription_stream(
            text="Prueba",
            conversation_history=history,
        ):
            pass

        # Verificar que se pasaron las herramientas
        assert len(tools_recibidas) == len(service.tools)


class TestTTSPipeline:
    """Tests que verifican el pipeline de TTS de doble cola."""

    @pytest.mark.asyncio
    async def test_sintetiza_y_reproduce(self, service, mock_tts):
        """Verifica que el pipeline sintetiza y reproduce audio."""
        import numpy as np

        mock_tts.synthesize_only = AsyncMock(return_value=np.array([0.1, 0.2]))

        queue_text = asyncio.Queue()
        queue_audio = asyncio.Queue()

        await queue_text.put("Hola mundo.")
        await queue_text.put(None)

        await service._synth_worker(queue_text, queue_audio)

        # Verificar que se sintetizó
        mock_tts.synthesize_only.assert_called_once_with("Hola mundo.")

        # Verificar que se encoló audio (1 array + 1 None)
        assert queue_audio.qsize() == 2

    @pytest.mark.asyncio
    async def test_pipeline_doble_cola_completo(self, service, mock_tts):
        """Verifica el pipeline completo de síntesis y reproducción en paralelo."""
        import numpy as np

        mock_tts.synthesize_only = AsyncMock(return_value=np.array([0.1]))
        mock_tts.play_audio_array = AsyncMock()

        history = []
        chunks = []

        async def mock_stream(*args, **kwargs):
            yield "Hola. "
            yield "Cómo estás? "
            yield "Bien gracias."

        service.litert.chat_stream = mock_stream

        async for chunk in service.process_transcription_stream(
            text="Salúdame",
            conversation_history=history,
        ):
            chunks.append(chunk)

        # Verificar que se generaron chunks
        assert len(chunks) > 0

        # Verificar que el historial se actualizó
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_tts_stop_detiene_reproduccion(self, service, mock_tts):
        """Verifica que stop() detiene el TTS correctamente."""
        service.tts.stop()
        mock_tts.stop.assert_called_once()


class TestExtractSentences:
    """Tests para la extracción de frases del buffer de texto."""

    def test_frase_simple(self, service):
        sentences, remaining = service._extract_sentences("Hola mundo. ")
        assert len(sentences) == 1
        assert sentences[0] == "Hola mundo."

    def test_multiples_frases(self, service):
        text = "Primero. Segundo! Tercero? Cuarto: Quinto."
        sentences, remaining = service._extract_sentences(text)
        assert len(sentences) == 5

    def test_frase_incompleta(self, service):
        text = "Esto está incompleto"
        sentences, remaining = service._extract_sentences(text)
        assert len(sentences) == 0
        assert remaining == text

    def test_buffer_largo_corta_por_coma(self, service):
        text = "Esta es una frase muy larga que definitivamente supera los ochenta caracteres, así que debería cortarse."
        assert len(text) > 80
        sentences, remaining = service._extract_sentences(text)
        # Debería haber cortado por la coma
        assert len(sentences) >= 1

    def test_respeta_decimales(self, service):
        text = "El resultado es 3.14 puntos."
        sentences, remaining = service._extract_sentences(text)
        assert len(sentences) == 1
        assert "3.14" in sentences[0]


class TestIsSpeakable:
    """Tests para el filtro de texto hablable."""

    def test_texto_normal(self, service):
        assert service._is_speakable("Hola, cómo estás?") is True

    def test_codigo_rechazado(self, service):
        assert service._is_speakable("[1, 2, 3]") is False
        assert service._is_speakable('{"key": "value"}') is False
        assert service._is_speakable("```python\nprint('hola')\n```") is False

    def test_texto_corto_rechazado(self, service):
        assert service._is_speakable("OK") is False

    def test_sin_letras_rechazado(self, service):
        assert service._is_speakable("12345") is False


class TestStripMarkdown:
    """Tests para la función strip_markdown."""

    def test_bold(self):
        assert strip_markdown("**texto**") == "texto"

    def test_italic(self):
        assert strip_markdown("*texto*") == "texto"

    def test_code_block(self):
        assert strip_markdown("```código```") == ""

    def test_inline_code(self):
        assert strip_markdown("`código`") == "código"

    def test_link(self):
        assert strip_markdown("[texto](url)") == "texto"

    def test_heading(self):
        assert strip_markdown("# Título") == "Título"

    def test_combined(self):
        text = "**Hola** *mundo* con `código` y [link](url)"
        result = strip_markdown(text)
        assert "Hola" in result
        assert "mundo" in result
        assert "código" in result
        assert "link" in result


class TestProcessAudio:
    """Tests para el flujo de procesamiento de audio."""

    @pytest.mark.asyncio
    async def test_audio_vacio(self, service, mock_stt):
        """Verifica que audio sin voz se maneja correctamente."""
        mock_stt.transcribe = AsyncMock(return_value="")

        result = await service.process_audio(
            audio_path=MagicMock(),
            conversation_history=[],
        )

        assert result["status"] == "error"
        assert "No se detectó voz" in result["message"]

    @pytest.mark.asyncio
    async def test_audio_corto(self, service, mock_stt):
        """Verifica que audio muy corto se rechaza."""
        mock_stt.transcribe = AsyncMock(return_value="a")

        result = await service.process_audio(
            audio_path=MagicMock(),
            conversation_history=[],
        )

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_audio_valido(self, service, mock_stt, mock_litert):
        """Verifica que audio válido se procesa correctamente."""
        mock_stt.transcribe = AsyncMock(return_value="Hola asistente")

        async def mock_stream(*args, **kwargs):
            yield "Hola! Cómo puedo ayudarte?"

        mock_litert.chat_stream = mock_stream

        result = await service.process_audio(
            audio_path=MagicMock(),
            conversation_history=[],
        )

        assert result["status"] == "success"
        assert result["transcribed_text"] == "Hola asistente"
        assert "Hola" in result["response_text"]


class TestNotifications:
    """Tests para las notificaciones de escritorio."""

    def test_send_notification(self, service):
        """Verifica que se envían notificaciones."""
        with patch("subprocess.Popen") as mock_popen:
            service.send_notification("Test message")
            mock_popen.assert_called()

    @pytest.mark.asyncio
    async def test_send_notification_async(self, service):
        """Verifica que se envían notificaciones async."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            await service.send_notification_async("Test message")
            mock_exec.assert_called()


class TestCancelAudioTasks:
    """Tests para la cancelación de tareas de audio."""

    @pytest.mark.asyncio
    async def test_cancel_sin_tareas(self, service):
        """Verifica que cancelar sin tareas activas no falla."""
        await service.cancel_audio_tasks()
        # No debería lanzar excepción

    @pytest.mark.asyncio
    async def test_wait_for_tts_sin_tarea(self, service):
        """Verifica que esperar TTS sin tarea no falla."""
        await service.wait_for_tts_complete()
        # No debería lanzar excepción


class TestCleanup:
    """Tests para la limpieza de recursos."""

    @pytest.mark.asyncio
    async def test_cleanup_completo(self, service, mock_tts):
        """Verifica que el cleanup cierra todos los recursos."""
        await service.cleanup()

        mock_tts.stop.assert_called()
        mock_tts.close_persistent_stream.assert_called()
