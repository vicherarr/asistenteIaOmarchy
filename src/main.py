"""Orchestrator principal - Servidor FastAPI del asistente de voz."""

import logging
import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.audio_manager import AudioManager
from src.command_executor import CommandExecutor, parse_gemma_response
from src.context_injector import build_full_system_prompt
from src.ollama_client import OllamaClient, OllamaMessage
from src.tts_engine import TTSEngine
from src.vision_tool import VisionTool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

conversation_history: list[OllamaMessage] = []
MAX_HISTORY = 10

audio_manager: Optional[AudioManager] = None
ollama_client: Optional[OllamaClient] = None
command_executor: Optional[CommandExecutor] = None
tts_engine: Optional[TTSEngine] = None
vision_tool: Optional[VisionTool] = None
system_prompt: str = ""


def strip_markdown(text: str) -> str:
    """Elimina formato markdown del texto para que suene natural en TTS."""
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = re.sub(r"`{3}[\s\S]*?`{3}", "", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"^[#\->]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global audio_manager, ollama_client, command_executor, tts_engine, vision_tool, system_prompt

    logger.info("Inicializando AsistenteIA...")

    audio_manager = AudioManager()
    audio_manager.auto_configure_bluetooth()

    ollama_client = OllamaClient()
    if not await ollama_client.health_check():
        logger.warning("Ollama no responde. Asegúrate de que está corriendo: ollama serve")

    command_executor = CommandExecutor()
    tts_engine = TTSEngine()
    vision_tool = VisionTool()

    system_prompt = build_full_system_prompt()

    logger.info("AsistenteIA listo")

    yield

    if ollama_client:
        await ollama_client.close()
    logger.info("AsistenteIA detenido")


app = FastAPI(title="AsistenteIA", lifespan=lifespan)


@app.post("/transcribe", response_model=TranscriptionResponse)
async def handle_transcription(request: TranscriptionRequest):
    """
    Endpoint principal: recibe texto de Handy, procesa con Gemma,
    ejecuta comandos y genera respuesta de voz.
    """
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Texto vacío")

    logger.info(f"Transcripción recibida: {request.text[:100]}...")

    conversation_history.append(OllamaMessage(role="user", content=request.text))

    if len(conversation_history) > MAX_HISTORY:
        conversation_history[:] = conversation_history[-MAX_HISTORY:]

    messages = [OllamaMessage(role="system", content=system_prompt)] + conversation_history

    try:
        assert ollama_client is not None
        gemma_response = await ollama_client.generate(messages)
    except Exception as e:
        logger.error(f"Error llamando a Ollama: {e}")
        raise HTTPException(status_code=502, detail=f"Error con Ollama: {e}")

    parsed = parse_gemma_response(gemma_response)

    conversation_history.append(OllamaMessage(role="assistant", content=gemma_response))

    commands_executed = 0
    if parsed.commands:
        assert command_executor is not None
        results = command_executor.execute_multiple(parsed.commands)
        commands_executed = sum(1 for success, _ in results if success)
        for success, output in results:
            status = "OK" if success else "FALLÓ"
            logger.info(f"Comando [{status}]: {output}")

    if parsed.action_type == "vision":
        try:
            assert vision_tool is not None
            assert ollama_client is not None
            logger.info("Acción de visión: capturando pantalla")
            image_base64 = vision_tool.get_screen_for_vision()
            vision_response = await ollama_client.generate_with_image(
                text="Describe brevemente qué se ve en esta pantalla. Sé conciso y útil.",
                image_base64=image_base64,
            )
            parsed.response_text = vision_response
            logger.info(f"Respuesta de visión: {vision_response[:100]}...")
        except Exception as e:
            logger.error(f"Error en visión: {e}")
            parsed.response_text = "No pude capturar la pantalla en este momento."

    audio_file: Optional[str] = None
    if parsed.response_text and parsed.action_type in ("speak", "both", "vision"):
        parsed.response_text = strip_markdown(parsed.response_text)
        try:
            assert tts_engine is not None
            audio_file = await tts_engine.speak_async(parsed.response_text)
        except Exception as e:
            logger.error(f"Error en TTS: {e}")

    return TranscriptionResponse(
        status="success",
        response_text=parsed.response_text,
        commands_executed=commands_executed,
        audio_file=audio_file,
    )


@app.get("/status", response_model=StatusResponse)
async def get_status():
    """Estado actual del asistente."""
    ollama_ok = False
    if ollama_client:
        ollama_ok = await ollama_client.health_check()

    bt_status = audio_manager.get_status_summary() if audio_manager else "No inicializado"

    return StatusResponse(
        ollama_connected=ollama_ok,
        bluetooth_audio=bt_status,
        conversation_length=len(conversation_history),
    )


@app.post("/reset")
async def reset_conversation():
    """Reinicia el historial de conversación."""
    global conversation_history
    conversation_history.clear()
    return {"status": "reset", "message": "Historial de conversación reiniciado"}


@app.post("/audio/configure")
async def configure_audio():
    """Reconfigura dispositivos de audio Bluetooth."""
    if audio_manager:
        source, sink = audio_manager.auto_configure_bluetooth()
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
        host="127.0.0.1",
        port=8765,
        log_level="info",
    )
