"""Módulo de transcripción de voz a texto (STT).

La inferencia de faster-whisper corre en un subproceso worker dedicado
(src/stt_worker.py) y NO en este proceso. Motivo: cuando CTranslate2 convive
con el motor LiteRT en el mismo proceso, la contención de hilos dispara la
latencia de ~4s a ~12-15s. El worker aísla el STT y mantiene el modelo
residente, comunicándose por líneas JSON sobre stdin/stdout."""

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)


class STTEngine:
    """Cliente del worker de STT. Gestiona su ciclo de vida y la comunicación."""

    def __init__(self, model: Optional[str] = None, litert_client=None) -> None:
        self._model_name = model or settings.STT_MODEL
        self._language = settings.STT_LANGUAGE
        self._prompt = settings.STT_PROMPT
        self._vad = settings.STT_VAD
        self._use_gemma = settings.STT_USE_GEMMA_AUDIO
        self._litert = litert_client
        self._lock = asyncio.Lock()
        self._proc: Optional[asyncio.subprocess.Process] = None

    async def start(self) -> None:
        """Arranca el worker por adelantado (pre-calentado al iniciar el servicio).

        En modo Gemma no hay worker que arrancar: el reconocimiento usa el motor LiteRT."""
        if self._use_gemma:
            logger.info("Reconocimiento por audio nativo de Gemma activo; no se arranca el worker Whisper.")
            return
        async with self._lock:
            await self._ensure_worker()

    async def _ensure_worker(self) -> None:
        """Garantiza que el worker está vivo; lo lanza si hace falta. Asume lock tomado."""
        if self._proc is not None and self._proc.returncode is None:
            return

        logger.info(
            f"Lanzando worker STT '{self._model_name}' "
            f"({settings.STT_DEVICE}/{settings.STT_COMPUTE_TYPE}, {settings.STT_THREADS} hilos)..."
        )
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "src.stt_worker",
            "--model", self._model_name,
            "--device", settings.STT_DEVICE,
            "--compute-type", settings.STT_COMPUTE_TYPE,
            "--threads", str(settings.STT_THREADS),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            cwd=str(settings.PROJECT_ROOT),
        )
        ready = await self._read_response()
        if not ready or not ready.get("ready"):
            raise RuntimeError(f"El worker STT no señaló estar listo: {ready}")
        logger.info("Worker STT listo y residente.")

    async def _read_response(self) -> Optional[dict]:
        """Lee líneas de stdout del worker hasta encontrar un JSON válido."""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                return None  # EOF: el worker ha muerto
            try:
                return json.loads(raw.decode().strip())
            except json.JSONDecodeError:
                continue  # ignorar cualquier línea no-JSON espuria en stdout

    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe un archivo de audio a texto, con normalización previa."""
        logger.info(f"Procesando audio para STT: {audio_path}")

        # 1. Normalización suave con FFmpeg (configuración óptima demostrada en lab)
        cleaned_path = audio_path.with_suffix(".cleaned.wav")
        audio_to_transcribe = audio_path
        try:
            ffmpeg_process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(audio_path),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar", "16000", "-ac", "1", str(cleaned_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            await ffmpeg_process.communicate()
            if ffmpeg_process.returncode == 0:
                audio_to_transcribe = cleaned_path
        except FileNotFoundError:
            logger.warning("ffmpeg no encontrado; se transcribe el audio sin normalizar.")

        # 2. Transcripción: Gemma (audio nativo) o worker Whisper, según configuración.
        try:
            if self._use_gemma:
                if self._litert is None:
                    logger.error("STT_USE_GEMMA_AUDIO activo pero no se pasó litert_client.")
                    return ""
                text = await self._litert.transcribe_audio(str(audio_to_transcribe))
            else:
                text = await self._request_transcription(str(audio_to_transcribe))
            logger.info(f"Transcripción obtenida: '{text}'")
            return text
        except Exception as e:
            logger.error(f"Error inesperado en STT: {e}", exc_info=True)
            return ""
        finally:
            if audio_to_transcribe != audio_path:
                Path(audio_to_transcribe).unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)

    async def _request_transcription(self, path: str) -> str:
        request = {
            "path": path,
            "language": self._language,
            "beam_size": 5,
            "initial_prompt": self._prompt,
            "vad_filter": self._vad,
        }
        async with self._lock:
            for attempt in range(2):
                await self._ensure_worker()
                assert self._proc is not None and self._proc.stdin is not None
                try:
                    self._proc.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
                    await self._proc.stdin.drain()
                    response = await self._read_response()
                except (BrokenPipeError, ConnectionResetError):
                    response = None

                if response is None:
                    # El worker murió: forzar respawn y reintentar una vez.
                    logger.warning("El worker STT no respondió (caído). Reintentando...")
                    self._proc = None
                    continue
                if "error" in response:
                    raise RuntimeError(f"Worker STT devolvió error: {response['error']}")
                return response.get("text", "")
        raise RuntimeError("El worker STT no respondió tras reintento.")

    async def close(self) -> None:
        """Detiene el worker de forma ordenada."""
        async with self._lock:
            if self._proc is None or self._proc.returncode is not None:
                return
            logger.info("Deteniendo worker STT...")
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
