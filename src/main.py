"""Orchestrator principal - Servidor FastAPI del asistente de voz."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.audio_manager import AudioManager
from src.litert_client import LiteRTClient
from src.tts_engine import TTSEngine
from src.assistant_service import AssistantService
from src.config import settings
from src.audio_recorder import AudioRecorder
from src.stt_engine import STTEngine
from src.schema import ChatMessage
from src.utils import strip_markdown

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MAX_HISTORY = settings.MAX_HISTORY


class AppState:
    """Clase para mantener el estado de la aplicación sin variables globales."""
    def __init__(self):
        self.audio_manager = AudioManager()
        self.litert_client = LiteRTClient()
        self.tts_engine = TTSEngine()
        self.audio_recorder = AudioRecorder()
        self.stt_engine = STTEngine()
        self.assistant_service = AssistantService(
            litert_client=self.litert_client,
            tts_engine=self.tts_engine,
            stt_engine=self.stt_engine,
        )
        self.conversation_history: list[ChatMessage] = []
        self.current_task: Optional[asyncio.Task] = None
        self.processing: bool = False
        self.is_recording: bool = False


def get_app_state(request: Request) -> AppState:
    """Dependencia de FastAPI para inyectar el estado."""
    return request.app.state.app_state


class TranscriptionRequest(BaseModel):
    text: str


class TranscriptionResponse(BaseModel):
    status: str
    response_text: str
    commands_executed: int
    audio_file: Optional[str] = None


class StatusResponse(BaseModel):
    litert_connected: bool
    bluetooth_audio: str
    conversation_length: int
    processing: bool


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Inicializando AsistenteIA con LiteRT...")
    
    state = AppState()
    app.state.app_state = state

    # Configurar audio al inicio
    await state.audio_manager.auto_configure_bluetooth()

    if not state.litert_client.engine:
        logger.warning(f"LiteRT no pudo cargar el modelo en {settings.LITERT_MODEL_PATH}")

    logger.info("AsistenteIA listo")

    yield

    if hasattr(app.state, "app_state") and app.state.app_state.litert_client:
        app.state.app_state.litert_client.close()
    logger.info("AsistenteIA detenido")


app = FastAPI(title="AsistenteIA", lifespan=lifespan)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Manejador global de excepciones para devolver JSON siempre."""
    logger.error(f"Error no manejado: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": str(exc)},
    )


@app.post("/transcribe", response_model=TranscriptionResponse)
async def handle_transcription(
    request: TranscriptionRequest,
    state: AppState = Depends(get_app_state)
):
    """Endpoint principal: recibe texto, procesa con LiteRT y genera voz."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Texto vacío")

    # Cancelar cualquier tarea en curso (audio o transcripción previa)
    if state.current_task and not state.current_task.done():
        state.current_task.cancel()
    
    # Detener TTS si estaba sonando algo de una petición anterior
    state.tts_engine.stop()

    async def run_transcription():
        sink_id = state.audio_manager.default_sink
        return await state.assistant_service.process_transcription(
            text=request.text,
            conversation_history=state.conversation_history,
            sink_id=sink_id,
            max_history=MAX_HISTORY
        )

    state.processing = True
    state.current_task = asyncio.create_task(run_transcription())
    
    try:
        result = await state.current_task
        return TranscriptionResponse(**result)
    except asyncio.CancelledError:
        logger.info("Petición de transcripción cancelada por una nueva.")
        raise HTTPException(status_code=409, detail="Interrumpido por nueva petición")
    finally:
        # Solo marcamos como no procesando si no hay una nueva tarea ocupando el lugar
        if state.current_task.done():
            state.processing = False


@app.post("/listen/toggle")
async def toggle_listen(state: AppState = Depends(get_app_state)):
    """Alterna el estado de grabación del micrófono."""
    if state.is_recording:
        # Detener grabación y procesar
        state.is_recording = False
        audio_path = state.audio_recorder.stop_recording()
        
        if audio_path:
            if state.current_task and not state.current_task.done():
                state.current_task.cancel()
            
            async def process_task():
                try:
                    sink_id = state.audio_manager.default_sink
                    await state.assistant_service.process_audio(
                        audio_path, 
                        state.conversation_history,
                        sink_id=sink_id,
                        max_history=MAX_HISTORY
                    )
                except Exception as e:
                    logger.error(f"Error en tarea de audio: {e}")
                finally:
                    state.processing = False

            state.processing = True
            state.current_task = asyncio.create_task(process_task())
            return {"status": "processing"}
        else:
            return {"status": "error", "message": "No se pudo obtener el audio"}
    else:
        # Iniciar nueva grabación
        if state.current_task and not state.current_task.done():
            state.current_task.cancel()
        if state.tts_engine:
            state.tts_engine.stop()
            
        state.is_recording = True
        
        # 1. Notificar y empezar a grabar
        state.assistant_service.send_notification("Escuchando...")
        source_id = state.audio_manager.default_source
        if source_id:
            asyncio.create_task(state.audio_manager.set_volume(source_id, 0.9))
            
        state.audio_recorder.start_recording(source_id=source_id)
        
        # 2. Refrescar configuración de audio en segundo plano
        asyncio.create_task(state.audio_manager.auto_configure_bluetooth())
        
        return {"status": "listening"}


@app.post("/cancel")
async def cancel_processing(state: AppState = Depends(get_app_state)):
    """Cancela cualquier procesamiento en curso y detiene TTS."""
    cancelled = False

    if state.current_task and not state.current_task.done():
        state.current_task.cancel()
        cancelled = True

    state.processing = False
    state.is_recording = False
    
    if state.audio_recorder.is_recording:
        state.audio_recorder.stop_recording()

    if state.tts_engine:
        state.tts_engine.stop()

    return {"status": "cancelled", "was_processing": cancelled}


@app.get("/status", response_model=StatusResponse)
async def get_status(state: AppState = Depends(get_app_state)):
    """Estado actual del asistente."""
    litert_ok = state.litert_client.engine is not None
    bt_status = await state.audio_manager.get_status_summary() if state.audio_manager else "No inicializado"

    return StatusResponse(
        litert_connected=litert_ok,
        bluetooth_audio=bt_status,
        conversation_length=len(state.conversation_history),
        processing=state.processing or state.is_recording,
    )


@app.get("/history")
async def get_history(state: AppState = Depends(get_app_state)):
    """Devuelve el historial completo o el último mensaje."""
    return {
        "history": [msg.dict() for msg in state.conversation_history],
        "last_response": state.conversation_history[-1].content if state.conversation_history and state.conversation_history[-1].role == "assistant" else None
    }


@app.post("/reset")
async def reset_conversation(state: AppState = Depends(get_app_state)):
    """Reinicia el historial de conversación."""
    state.conversation_history.clear()
    return {"status": "reset", "message": "Historial de conversación reiniciado"}


@app.post("/audio/configure")
async def configure_audio(state: AppState = Depends(get_app_state)):
    """Reconfigura dispositivos de audio Bluetooth."""
    if state.audio_manager:
        source, sink = await state.audio_manager.auto_configure_bluetooth()
        return {
            "status": "configured",
            "source": source,
            "sink": sink,
        }
    raise HTTPException(status_code=500, detail="Audio manager no inicializado")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level="info" if not settings.DEBUG else "debug",
    )
