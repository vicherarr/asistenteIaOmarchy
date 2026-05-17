"""Orchestrator principal - Servidor FastAPI del asistente de voz."""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.audio_manager import AudioManager
from src.command_executor import CommandExecutor, parse_gemma_response
from src.context_injector import build_full_system_prompt
from src.ollama_client import OllamaClient, OllamaMessage
from src.tts_engine import TTSEngine
from src.vision_tool import VisionTool
from src.assistant_service import AssistantService
from src.config import settings
from src.audio_recorder import AudioRecorder
from src.stt_engine import STTEngine
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
        self.ollama_client = OllamaClient()
        self.command_executor = CommandExecutor()
        self.tts_engine = TTSEngine()
        self.vision_tool = VisionTool()
        self.audio_recorder = AudioRecorder()
        self.stt_engine = STTEngine()
        self.system_prompt = build_full_system_prompt()
        self.assistant_service = AssistantService(
            ollama_client=self.ollama_client,
            command_executor=self.command_executor,
            vision_tool=self.vision_tool,
            tts_engine=self.tts_engine,
            stt_engine=self.stt_engine,
            system_prompt=self.system_prompt,
        )
        self.conversation_history: list[OllamaMessage] = []
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
    ollama_connected: bool
    bluetooth_audio: str
    conversation_length: int
    processing: bool


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Inicializando AsistenteIA...")
    
    state = AppState()
    app.state.app_state = state

    state.audio_manager.auto_configure_bluetooth()

    if not await state.ollama_client.health_check():
        logger.warning("Ollama no responde. Asegúrate de que está corriendo: ollama serve")

    logger.info("AsistenteIA listo")

    yield

    if hasattr(app.state, "app_state") and app.state.app_state.ollama_client:
        await app.state.app_state.ollama_client.close()
    logger.info("AsistenteIA detenido")


app = FastAPI(title="AsistenteIA", lifespan=lifespan)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Manejador global de excepciones para devolver JSON siempre."""
    logger.error(f"Error no manejado: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "Ocurrió un error interno en el servidor"},
    )


async def _process_transcription(text: str, state: AppState) -> dict:
    """Lógica principal de procesamiento delegada al servicio de negocio."""
    state.processing = True
    try:
        try:
            return await state.assistant_service.process_transcription(
                text=text,
                conversation_history=state.conversation_history,
                max_history=MAX_HISTORY
            )
        except Exception as e:
            logger.error(f"Error procesando transcripción: {e}")
            raise HTTPException(status_code=502, detail=f"Error interno: {e}")
    finally:
        state.processing = False


@app.post("/transcribe", response_model=TranscriptionResponse)
async def handle_transcription(
    request: TranscriptionRequest,
    state: AppState = Depends(get_app_state)
):
    """Endpoint principal: recibe texto, procesa con Gemma, ejecuta comandos y genera voz."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Texto vacío")

    if state.current_task and not state.current_task.done():
        state.current_task.cancel()

    state.current_task = asyncio.create_task(_process_transcription(request.text, state))

    try:
        result = await state.current_task
        return TranscriptionResponse(**result)
    except asyncio.CancelledError:
        return TranscriptionResponse(
            status="cancelled",
            response_text="",
            commands_executed=0,
        )


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
                
            state.current_task = asyncio.create_task(
                state.assistant_service.process_audio(
                    audio_path, 
                    state.conversation_history, 
                    MAX_HISTORY
                )
            )
            return {"status": "processing"}
        else:
            return {"status": "error", "message": "No se pudo obtener el audio"}
    else:
        # Iniciar nueva grabación
        # 1. Cancelar cualquier cosa en curso
        if state.current_task and not state.current_task.done():
            state.current_task.cancel()
        if state.tts_engine:
            state.tts_engine.stop()
            
        # 2. Configurar audio
        await asyncio.to_thread(state.audio_manager.auto_configure_bluetooth)
            
        # 3. Empezar a grabar
        state.is_recording = True
        state.audio_recorder.start_recording()
        state.assistant_service.send_notification("Escuchando... habla ahora")
        
        return {"status": "listening"}


@app.post("/cancel")
async def cancel_processing(state: AppState = Depends(get_app_state)):
    """Cancela cualquier procesamiento en curso y detiene TTS."""
    cancelled = False

    if state.current_task and not state.current_task.done():
        state.current_task.cancel()
        cancelled = True
        logger.info("Tarea cancelada por el usuario")

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
    ollama_ok = False
    if state.ollama_client:
        ollama_ok = await state.ollama_client.health_check()

    bt_status = await asyncio.to_thread(state.audio_manager.get_status_summary) if state.audio_manager else "No inicializado"

    return StatusResponse(
        ollama_connected=ollama_ok,
        bluetooth_audio=bt_status,
        conversation_length=len(state.conversation_history),
        processing=state.processing or state.is_recording,
    )


@app.post("/reset")
async def reset_conversation(state: AppState = Depends(get_app_state)):
    """Reinicia el historial de conversación."""
    state.conversation_history.clear()
    return {"status": "reset", "message": "Historial de conversación reiniciado"}


@app.post("/audio/configure")
async def configure_audio(state: AppState = Depends(get_app_state)):
    """Reconfigura dispositivos de audio Bluetooth."""
    if state.audio_manager:
        source, sink = state.audio_manager.auto_configure_bluetooth()
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