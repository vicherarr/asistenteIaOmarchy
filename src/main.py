"""Orchestrator principal - Servidor FastAPI del asistente de voz."""

import os
# Optimización crítica: Limitar hilos de CPU en librerías de álgebra para evitar colisiones
# de memoria (Segmentation Fault / Core Dump) entre LiteRT GPU, PyTorch CPU y Kokoro.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import asyncio
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.security import APIKeyHeader, APIKeyQuery
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
from src.wake_word_listener import SherpaWakeWordListener

try:
    from src.bt_button_listener import BtButtonListener, BtButtonListenerError
    BT_LISTENER_AVAILABLE = True
except ImportError:
    BT_LISTENER_AVAILABLE = False
    BtButtonListener = None  # type: ignore
    BtButtonListenerError = Exception  # type: ignore

try:
    from src.mpris_dummy_player import MprisDummyPlayer, MprisPlayerError
    MPRIS_AVAILABLE = True
except ImportError:
    MPRIS_AVAILABLE = False
    MprisDummyPlayer = None  # type: ignore
    MprisPlayerError = Exception  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class TokenObfuscatorFilter(logging.Filter):
    """Filtro de logging para ofuscar el parámetro token=... en las trazas de Uvicorn."""
    def filter(self, record):
        import re
        # Ofuscar el token en record.args si están presentes (Uvicorn access logger pasa argumentos)
        if record.args and len(record.args) >= 3:
            path = record.args[2]
            if isinstance(path, str) and "token=" in path:
                obfuscated_path = re.sub(r"token=[a-zA-Z0-9_\-]+", "token=******", path)
                args_list = list(record.args)
                args_list[2] = obfuscated_path
                record.args = tuple(args_list)
        
        # También ofuscar en el mensaje directo o formateado
        if isinstance(record.msg, str) and "token=" in record.msg:
            record.msg = re.sub(r"token=[a-zA-Z0-9_\-]+", "token=******", record.msg)
            
        return True


def setup_logging_filters():
    """Aplica el filtro de ofuscación de tokens a todos los loggers relevantes."""
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "uvicorn.asgi"):
        ulogger = logging.getLogger(logger_name)
        # Evitar duplicados
        if not any(isinstance(f, TokenObfuscatorFilter) for f in ulogger.filters):
            ulogger.addFilter(TokenObfuscatorFilter())


# Aplicar el filtro de forma inmediata
setup_logging_filters()


MAX_HISTORY = settings.MAX_HISTORY


def _is_mpris_proxy_running() -> bool:
    """Verifica si mpris-proxy ya está corriendo en el sistema."""
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-x", "mpris-proxy"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
        )
        return result.returncode == 0
    except Exception:
        return False


class RateLimiter:
    """Rate limiter simple en memoria (token bucket por IP)."""

    def __init__(self, max_requests: int = 5, window_seconds: float = 1.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, client_ip: str) -> bool:
        now = time.monotonic()
        window_start = now - self.window_seconds

        # Limpiar requests antiguos
        self.requests[client_ip] = [
            t for t in self.requests[client_ip] if t > window_start
        ]

        if len(self.requests[client_ip]) >= self.max_requests:
            return False

        self.requests[client_ip].append(now)
        return True

    def cleanup(self):
        """Limpia entradas antiguas para evitar memory leak."""
        now = time.monotonic()
        cutoff = now - self.window_seconds * 2
        expired = [
            ip for ip, times in self.requests.items()
            if not times or max(times) < cutoff
        ]
        for ip in expired:
            del self.requests[ip]


# Instancias de rate limiter por tipo de endpoint
_rate_limiter_default = RateLimiter(max_requests=5, window_seconds=1.0)
_rate_limiter_strict = RateLimiter(max_requests=1, window_seconds=2.0)


async def rate_limit_middleware(request: Request, call_next):
    """Middleware que aplica rate limiting según la ruta."""
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path

    # Bypasear rate limiting en entorno de pruebas unitarias
    if client_ip == "testclient":
        return await call_next(request)

    # Endpoints de solo lectura sin rate limit
    if path in ("/health", "/status", "/history", "/status/events", "/chat"):
        return await call_next(request)

    # Endpoints estrictos (1 req cada 2s)
    if path in ("/listen/toggle", "/reset"):
        limiter = _rate_limiter_strict
    # Endpoints de streaming (3 req/s)
    elif path in ("/transcribe/stream",):
        limiter = RateLimiter(max_requests=3, window_seconds=1.0)
    # Resto (5 req/s)
    else:
        limiter = _rate_limiter_default

    if not limiter.is_allowed(client_ip):
        logger.warning(f"Rate limit excedido para {client_ip} en {path}")
        return JSONResponse(
            status_code=429,
            content={"status": "error", "message": "Demasiadas peticiones. Espera unos segundos."},
        )

    # Cleanup periódico
    if path == "/status":
        limiter.cleanup()

    return await call_next(request)


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
        self.paused_players: list[str] = []
        self.wake_word_listener: Optional[SherpaWakeWordListener] = None
        self.bt_button_listener: Optional["BtButtonListener"] = None  # type: ignore
        self.mpris_dummy_player: Optional["MprisDummyPlayer"] = None  # type: ignore
        self.mpris_proxy_process: Optional[asyncio.subprocess.Process] = None
        self._last_avrcp_toggle_time: float = 0.0
        self.last_user_transcription: Optional[str] = None  # Último texto transcrito por STT
        self.last_transcription_timestamp: float = 0.0


def get_app_state(request: Request) -> AppState:
    """Dependencia de FastAPI para inyectar el estado."""
    return request.app.state.app_state


api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)
api_key_query = APIKeyQuery(name="token", auto_error=False)


async def verify_token(
    token_header: Optional[str] = Depends(api_key_header),
    token_query: Optional[str] = Depends(api_key_query)
):
    """Dependencia para verificar el token de acceso."""
    expected_token = settings.API_TOKEN
    if not expected_token:
        # Si no hay token configurado en .env, permitimos acceso libre
        return

    if token_header == expected_token or token_query == expected_token:
        return

    raise HTTPException(
        status_code=401,
        detail="No autorizado: Token de acceso ausente o inválido."
    )


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
    speaking: bool = False
    gpu_active: bool = False
    litert_backend: str = "Desconectado"
    last_user_transcription: Optional[str] = None
    last_transcription_timestamp: float = 0.0


class HealthResponse(BaseModel):
    litert: bool = False
    whisper: bool = False
    kokoro: bool = False
    tmux: bool = False
    cdp: bool = False
    version: str = "1.0.0"




@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging_filters()
    logger.info("Inicializando AsistenteIA con LiteRT...")
    
    state = AppState()
    app.state.app_state = state

    # Pre-calentar el worker de STT (carga el modelo en su subproceso aislado)
    try:
        await state.stt_engine.start()
    except Exception as e:
        logger.warning(f"No se pudo pre-arrancar el worker STT: {e}")

    # Configurar audio al inicio
    await state.audio_manager.auto_configure_bluetooth()

    if not state.litert_client.engine:
        logger.warning(f"LiteRT no pudo cargar el modelo en {settings.LITERT_MODEL_PATH}")

    # Configurar e iniciar el WakeWordListener si está habilitado
    if settings.WAKE_WORD_ENABLED:
        async def on_wake_detected():
            logger.info("Wake word 'LUKA' detectada! Iniciando toggle_listen asíncrono.")
            await toggle_listen(state)

        try:
            state.wake_word_listener = SherpaWakeWordListener(
                model_dir=str(settings.PROJECT_ROOT / settings.WAKE_WORD_MODEL_DIR),
                keywords_file=str(settings.PROJECT_ROOT / settings.WAKE_WORD_KEYWORDS_FILE),
                on_wake_word_detected=on_wake_detected,
                keywords_threshold=settings.WAKE_WORD_THRESHOLD,
                keywords_score=2.0,
                max_active_paths=8,
                num_trailing_blanks=0,
                input_gain=3.0,
                provider=settings.LITERT_BACKEND if settings.LITERT_BACKEND in ("cpu", "cuda") else "cpu",
            )
            state.wake_word_listener.start()
            logger.info(f"SherpaWakeWordListener iniciado. Modelo: {settings.WAKE_WORD_MODEL_DIR}")
        except Exception as e:
            logger.warning(f"No se pudo iniciar SherpaWakeWordListener: {e}")

    # Configurar e iniciar el listener de botones Bluetooth (AVRCP)
    if BT_LISTENER_AVAILABLE:
        try:
            state.bt_button_listener = BtButtonListener()

            async def on_bt_play():
                logger.info("Botón PLAY del HOME SPA-133 detectado. Activando escucha...")
                await toggle_listen(state)

            async def on_bt_pause():
                logger.info("Botón PAUSE del HOME SPA-133 detectado. Cancelando...")
                await cancel_processing(state)

            state.bt_button_listener.on_play = on_bt_play
            state.bt_button_listener.on_pause = on_bt_pause
            await state.bt_button_listener.start()
            logger.info(f"Listener de botones BT iniciado: {state.bt_button_listener.device_name}")
        except BtButtonListenerError as e:
            logger.warning(f"No se pudo iniciar el listener de botones BT: {e}")
    else:
        logger.info("python-evdev no disponible. Listener de botones BT omitido.")

    # Configurar Dummy MPRIS Player para interceptar comandos AVRCP
    if MPRIS_AVAILABLE:
        try:
            state.mpris_dummy_player = MprisDummyPlayer()

            MPRIS_DEBOUNCE_SECONDS = 5.0

            async def _mpris_play():
                """Inicia o detiene escucha con debounce."""
                now = asyncio.get_event_loop().time()
                if now - state._last_avrcp_toggle_time < MPRIS_DEBOUNCE_SECONDS:
                    logger.debug(f"AVRCP Play ignorado (debounce {now - state._last_avrcp_toggle_time:.2f}s)")
                    return
                state._last_avrcp_toggle_time = now
                logger.info("AVRCP: Play detectado. Toggle escucha...")
                await toggle_listen(state)

            async def _mpris_pause():
                """Pause ignorado: los JBL Tune 670NC envian Pause() fantasmas
                autonomamente sin que se pulse el boton. Solo Play actua."""
                logger.debug("AVRCP: Pause() ignorado (bug firmware JBL)")

            state.mpris_dummy_player.on_play = _mpris_play
            state.mpris_dummy_player.on_pause = _mpris_pause
            await state.mpris_dummy_player.start()
            logger.info("Dummy MPRIS Player registrado: org.mpris.MediaPlayer2.asistenteia")

            # Iniciar mpris-proxy si no está corriendo
            if not _is_mpris_proxy_running():
                logger.info("Iniciando mpris-proxy...")
                proc = await asyncio.create_subprocess_exec(
                    "mpris-proxy",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                state.mpris_proxy_process = proc
                logger.info(f"mpris-proxy iniciado (PID {proc.pid})")
            else:
                logger.info("mpris-proxy ya estaba corriendo.")
        except Exception as e:
            logger.warning(f"No se pudo iniciar el dummy MPRIS player: {e}")
    else:
        logger.info("dbus-next no disponible. Dummy MPRIS Player omitido.")

    logger.info("AsistenteIA listo")

    yield

    # Graceful shutdown: esperar tasks pendientes y cerrar recursos
    logger.info("Iniciando cierre seguro del asistente...")

    if hasattr(app.state, "app_state"):
        state = app.state.app_state

        # Detener WakeWordListener si está activo
        if state.wake_word_listener:
            logger.info("Deteniendo SherpaWakeWordListener...")
            state.wake_word_listener.stop()

        # Detener BtButtonListener si está activo
        if state.bt_button_listener:
            logger.info("Deteniendo BtButtonListener...")
            await state.bt_button_listener.stop()

        # Detener Dummy MPRIS Player y mpris-proxy
        if state.mpris_dummy_player:
            logger.info("Deteniendo Dummy MPRIS Player...")
            await state.mpris_dummy_player.stop()
        if state.mpris_proxy_process:
            logger.info("Deteniendo mpris-proxy...")
            try:
                state.mpris_proxy_process.terminate()
                await asyncio.wait_for(state.mpris_proxy_process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                state.mpris_proxy_process.kill()
            except Exception:
                pass

        # 1. Cancelar tarea en curso si existe
        if state.current_task and not state.current_task.done():
            logger.info("Cancelando tarea en curso...")
            state.current_task.cancel()
            try:
                await asyncio.wait_for(state.current_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # 2. Cleanup del servicio de negocio (audio, workers, tts)
        await state.assistant_service.cleanup()

        # 3. Detener grabación si está activa
        if state.is_recording and state.audio_recorder.is_recording:
            logger.info("Deteniendo grabación de micrófono...")
            state.audio_recorder.stop_recording()

        # 4. Cerrar worker de STT
        if state.stt_engine:
            try:
                await state.stt_engine.close()
            except Exception as e:
                logger.warning(f"Error cerrando worker STT: {e}")

        # 5. Cerrar LiteRT client
        if state.litert_client:
            logger.info("Cerrando cliente LiteRT...")
            state.litert_client.close()

    logger.info("AsistenteIA detenido correctamente")


app = FastAPI(title="AsistenteIA", lifespan=lifespan)
app.middleware("http")(rate_limit_middleware)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Manejador global de excepciones para devolver JSON siempre."""
    logger.error(f"Error no manejado: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": str(exc)},
    )


@app.post("/transcribe/stream", dependencies=[Depends(verify_token)])
async def handle_transcription_stream(
    request: TranscriptionRequest,
    state: AppState = Depends(get_app_state)
):
    """Endpoint de streaming: recibe texto, procesa con LiteRT en chunks y genera voz al final."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Texto vacío")

    # Cancelar cualquier tarea en curso (audio o transcripción previa)
    if state.current_task and not state.current_task.done():
        state.current_task.cancel()
    
    # Detener TTS si estaba sonando algo de una petición anterior
    state.tts_engine.stop()

    async def stream_generator():
        state.processing = True
        current_task = asyncio.current_task()
        state.current_task = current_task
        try:
            sink_id = state.audio_manager.default_sink
            async for chunk in state.assistant_service.process_transcription_stream(
                text=request.text,
                conversation_history=state.conversation_history,
                sink_id=sink_id,
                max_history=MAX_HISTORY
            ):
                yield chunk
        except asyncio.CancelledError:
            logger.info("Streaming cancelado por una nueva petición o desconexión.")
            raise
        finally:
            if state.current_task == current_task:
                state.processing = False

    return StreamingResponse(stream_generator(), media_type="text/plain")


@app.post("/transcribe", response_model=TranscriptionResponse, dependencies=[Depends(verify_token)])
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


@app.post("/listen/toggle", dependencies=[Depends(verify_token)])
async def toggle_listen(state: AppState = Depends(get_app_state)):
    """Alterna el estado de grabación del micrófono."""
    logger.info(
        f"toggle_listen invocado. Estado: is_recording={state.is_recording}, "
        f"processing={state.processing}, current_task_done={state.current_task.done() if state.current_task else True}, "
        f"history_len={len(state.conversation_history)}"
    )
    if state.is_recording:
        # Detener grabación y procesar
        state.is_recording = False
        audio_path = state.audio_recorder.stop_recording()
        
        if audio_path:
            if state.current_task and not state.current_task.done():
                state.current_task.cancel()
            
            def on_transcription(text: str):
                # Exponer la transcripción a la UI en cuanto el STT termina,
                # sin esperar a que el LLM genere la respuesta.
                state.last_user_transcription = text
                state.last_transcription_timestamp = asyncio.get_event_loop().time()
                logger.info(f"Transcripción guardada para UI: {text}")
                # Notificar al sistema (Hyprland/mako) en el instante del reconocimiento.
                state.assistant_service.send_notification(f"Has dicho: {text}")

            async def process_task():
                try:
                    sink_id = state.audio_manager.default_sink
                    await state.assistant_service.process_audio(
                        audio_path,
                        state.conversation_history,
                        sink_id=sink_id,
                        max_history=MAX_HISTORY,
                        on_transcription=on_transcription
                    )
                    # Esperar a que el TTS termine de reproducir
                    await state.assistant_service.wait_for_tts_complete()
                except Exception as e:
                    logger.error(f"Error en tarea de audio: {e}")
                finally:
                    state.processing = False
                    # Reanudar música
                    if not state.is_recording and not state.processing and state.paused_players:
                        await state.audio_manager.resume_players(state.paused_players)
                        state.paused_players = []

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
        
        # Pausar reproductores de música activos antes de empezar
        state.paused_players = await state.audio_manager.pause_active_players()
        
        # 1. Notificar y empezar a grabar
        await state.assistant_service.send_notification_async("Escuchando...")
        source_id = state.audio_manager.default_source
        if source_id:
            asyncio.create_task(state.audio_manager.set_volume(source_id, 1.8))
        
        # Definir callback para cuando se detecte silencio prolongado
        def on_silence_detected():
            logger.info("Auto-stop disparado por silencio. Ejecutando toggle local en memoria...")
            # Llamar directamente a toggle_listen de forma asíncrona y en memoria.
            # Esto evita pasar por la capa de transporte HTTP y el middleware de rate-limiting (Error 429).
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(toggle_listen(state))
            except RuntimeError:
                # En entornos de pruebas unitarias sin event loop activo, usamos asyncio.run
                asyncio.run(toggle_listen(state))
            
        logger.info("toggle_listen: Iniciando grabación de micrófono.")
        state.audio_recorder.start_recording(
            source_id=source_id, 
            on_silence_callback=on_silence_detected
        )
        
        # 2. Refrescar configuración de audio en segundo plano
        asyncio.create_task(state.audio_manager.auto_configure_bluetooth())
        
        return {"status": "listening"}


@app.post("/cancel", dependencies=[Depends(verify_token)])
async def cancel_processing(state: AppState = Depends(get_app_state)):
    """Cancela cualquier procesamiento en curso y detiene TTS."""
    cancelled = False

    if state.current_task and not state.current_task.done():
        state.current_task.cancel()
        cancelled = True

    # Cancelar tareas de audio en segundo plano de forma encapsulada
    await state.assistant_service.cancel_audio_tasks()
    cancelled = True

    state.processing = False
    state.is_recording = False
    
    if state.audio_recorder.is_recording:
        state.audio_recorder.stop_recording()

    if state.tts_engine:
        state.tts_engine.stop()

    return {"status": "cancelled", "was_processing": cancelled}


@app.get("/status", response_model=StatusResponse, dependencies=[Depends(verify_token)])
async def get_status(state: AppState = Depends(get_app_state)):
    """Estado actual del asistente."""
    litert_ok = state.litert_client.engine is not None
    bt_status = await state.audio_manager.get_status_summary() if state.audio_manager else "No inicializado"
    is_speaking = state.tts_engine._is_playing if state.tts_engine else False

    # Detección verídica del backend de hardware de LiteRT
    litert_backend = "Desconectado"
    gpu_active = False
    if litert_ok and state.litert_client.engine:
        try:
            import litert_lm
            backend_enum = state.litert_client.engine.backend
            if backend_enum == litert_lm.Backend.GPU:
                litert_backend = "GPU"
                gpu_active = True
            elif backend_enum == litert_lm.Backend.CPU:
                litert_backend = "CPU"
            else:
                litert_backend = "Auto"
        except Exception:
            litert_backend = "CPU"

    return StatusResponse(
        litert_connected=litert_ok,
        bluetooth_audio=bt_status,
        conversation_length=len(state.conversation_history),
        processing=state.processing or state.is_recording,
        speaking=is_speaking,
        gpu_active=gpu_active,
        litert_backend=litert_backend,
        last_user_transcription=state.last_user_transcription,
        last_transcription_timestamp=state.last_transcription_timestamp
    )


@app.get("/status/events", dependencies=[Depends(verify_token)])
async def status_events(request: Request, state: AppState = Depends(get_app_state)):
    """Canal de eventos en tiempo real (SSE) para el estado del asistente."""
    import json
    
    async def event_generator():
        last_hash = None
        while True:
            if await request.is_disconnected():
                break
            
            try:
                # Reutilizar el cálculo de get_status
                status_resp = await get_status(state)
                try:
                    status_dict = status_resp.model_dump()
                except AttributeError:
                    status_dict = status_resp.dict()
                
                # Crear un hash simple para detectar cambios reales
                current_hash = hash(frozenset((k, str(v)) for k, v in status_dict.items()))
                
                if current_hash != last_hash:
                    yield f"data: {json.dumps(status_dict)}\n\n"
                    last_hash = current_hash
            except Exception as e:
                logger.error(f"Error en event_generator de estado: {e}")
                
            await asyncio.sleep(0.3)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/chat", response_class=HTMLResponse)
async def get_chat_interface():
    """Sirve la interfaz gráfica web tipo ChatGPT."""
    import os
    chat_file_path = os.path.join(os.path.dirname(__file__), "gui", "chat.html")
    try:
        with open(chat_file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content, status_code=200)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Interfaz de chat no encontrada.")


@app.get("/health", response_model=HealthResponse)
async def health_check(state: AppState = Depends(get_app_state)):
    """Verificación rápida de todos los componentes del sistema."""
    # LiteRT
    litert_ok = state.litert_client.engine is not None

    # Whisper (verificar que whisper-cli está en PATH)
    whisper_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "whisper-cli", "--help",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
        whisper_ok = proc.returncode == 0 or proc.returncode is not None  # --help puede devolver != 0 pero existe
    except FileNotFoundError:
        pass

    # Kokoro
    kokoro_ok = state.tts_engine._kokoro_pipeline is not None

    # TMUX (verificar sesión asistenteia)
    tmux_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", "asistenteia",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
        tmux_ok = proc.returncode == 0
    except FileNotFoundError:
        pass

    # CDP (verificar puerto 9222 accesible)
    cdp_ok = False
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 9222),
            timeout=2.0
        )
        writer.close()
        await writer.wait_closed()
        cdp_ok = True
    except (OSError, asyncio.TimeoutError):
        pass

    return HealthResponse(
        litert=litert_ok,
        whisper=whisper_ok,
        kokoro=kokoro_ok,
        tmux=tmux_ok,
        cdp=cdp_ok,
    )




@app.get("/history", dependencies=[Depends(verify_token)])
async def get_history(state: AppState = Depends(get_app_state)):
    """Devuelve el historial completo o el último mensaje."""
    return {
        "history": [msg.dict() for msg in state.conversation_history],
        "last_response": state.conversation_history[-1].content if state.conversation_history and state.conversation_history[-1].role == "assistant" else None
    }


@app.post("/reset", dependencies=[Depends(verify_token)])
async def reset_conversation(state: AppState = Depends(get_app_state)):
    """Reinicia el historial de conversación y aborta cualquier procesamiento o TTS activo."""
    if state.current_task and not state.current_task.done():
        state.current_task.cancel()
    
    state.processing = False
    state.is_recording = False
    
    if state.audio_recorder.is_recording:
        state.audio_recorder.stop_recording()

    if state.tts_engine:
        state.tts_engine.stop()

    # Reanudar cualquier reproductor que haya quedado pausado
    if state.paused_players:
        await state.audio_manager.resume_players(state.paused_players)
        state.paused_players = []

    state.conversation_history.clear()
    logger.info("Conversación y estado del backend completamente reiniciados.")
    return {"status": "reset", "message": "Historial de conversación y procesos del backend reiniciados"}


@app.post("/audio/configure", dependencies=[Depends(verify_token)])
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
    
    ssl_keyfile = settings.SSL_KEYFILE if settings.SSL_KEYFILE and os.path.exists(settings.SSL_KEYFILE) else None
    ssl_certfile = settings.SSL_CERTFILE if settings.SSL_CERTFILE and os.path.exists(settings.SSL_CERTFILE) else None
    
    if ssl_keyfile and ssl_certfile:
        logger.info(f"Iniciando servidor en modo seguro (HTTPS) con certificado autofirmado.")
    
    uvicorn.run(
        "src.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level="info" if not settings.DEBUG else "debug",
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
    )
