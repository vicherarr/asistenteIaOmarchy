"""Motor Hermes: el bucle agéntico lo lleva Hermes Agent, no Luka.

Los otros tres motores son *modelos* a los que assistant_service les pasa las tools y
sobre los que itera él. Este es distinto: Hermes ES un agente —trae sus ~60 herramientas,
memoria, skills y subagentes— y se le entrega el turno entero. Luka conserva lo suyo:
voz, STT/TTS, satélite y el resto de la aplicación.

Encaja igual en el contrato InferenceEngine, porque desde fuera hace lo mismo que los
demás: recibe un prompt y devuelve texto en streaming. Lo que cambia es quién ejecuta las
herramientas por debajo.

Hermes corre en OTRO proceso, con su propio venv (ver scripts/hermes_bridge.py para el
motivo y el protocolo). Aquí se gestiona ese subproceso y se traduce su protocolo de
líneas JSON al contrato del motor.

El LLM sale del mismo sidecar TabbyAPI que usa exllama, pero con otro perfil: 64k de
contexto y `reasoning: true` para que el razonamiento no acabe hablado. Lo escribe
`ai_tabby_sync_config_for_engine` al cambiar de motor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Callable, List, Optional

from src.config import resolve_path, settings as _settings
from src.engines.base import EngineCapabilities

logger = logging.getLogger(__name__)

_INSTALL_HINT = "Instálalo con: asistenteia engine hermes install"


class HermesEngine:
    """Pilota Hermes Agent en un subproceso, conforme a InferenceEngine."""

    def __init__(self, settings=_settings):
        self.hermes_dir = str(resolve_path(settings.HERMES_DIR))
        self.python = str(resolve_path(settings.HERMES_PYTHON)) if settings.HERMES_PYTHON \
            else os.path.join(self.hermes_dir, ".venv", "bin", "python")
        self.bridge = str(resolve_path("scripts/hermes_bridge.py"))
        # El modelo y la URL son los del sidecar: Hermes no sirve pesos, los consume.
        self.base_url = settings.EXLLAMA_BASE_URL.rstrip("/") + "/v1"
        self.api_key = settings.EXLLAMA_API_KEY.strip() or "x"
        self.model = settings.HERMES_MODEL
        self.timeout = settings.HERMES_TIMEOUT
        self.max_iterations = settings.HERMES_MAX_ITERATIONS
        self.max_tokens = settings.HERMES_MAX_TOKENS
        self.enabled_toolsets = settings.HERMES_ENABLED_TOOLSETS
        self.disabled_toolsets = settings.HERMES_DISABLED_TOOLSETS
        self.skip_memory = settings.HERMES_SKIP_MEMORY

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()      # un turno cada vez: el puente es secuencial
        self._task_seq = 0

        # Verdad de campo del turno para el guardarraíl anti-invención. Aquí es de
        # primera mano: el puente avisa en cuanto ARRANCA cada herramienta.
        self.last_turn_tools_used: list[str] = []
        self.last_turn_tools_disabled: bool = False
        self.tracks_tool_usage: bool = True

        self._installed = os.path.isfile(self.bridge) and os.access(self.python, os.X_OK)
        if not self._installed:
            logger.warning(f"Hermes no está instalado en {self.hermes_dir}. {_INSTALL_HINT}")

    # ---- metadatos / salud ----
    @property
    def name(self) -> str:
        return "Hermes"

    @property
    def model_label(self) -> str:
        return self.model

    @property
    def is_ready(self) -> bool:
        return self._installed

    def backend_label(self) -> str:
        return "GPU (Hermes)" if self._installed else "Desconectado"

    @property
    def capabilities(self) -> EngineCapabilities:
        # audio=False -> el STT cae a Whisper, igual que con exllama.
        # vision=False -> por ahora Hermes usa sus propias tools de visión, no la nuestra.
        return EngineCapabilities(tools=True, vision=False, audio=False, gpu=True)

    @property
    def streams_clean_text(self) -> bool:
        return True   # el puente solo emite deltas de respuesta, sin llamadas ni marcadores

    @property
    def leads_with_reasoning(self) -> bool:
        # El razonamiento va por reasoning_callback y el puente lo descarta, así que
        # nunca llega al stream. (Requiere `reasoning: true` en el sidecar.)
        return False

    # ---- subproceso ----
    async def _ensure_proc(self) -> Optional[str]:
        """Arranca el puente si hace falta. Devuelve un mensaje de error, o None si va."""
        if self._proc is not None and self._proc.returncode is None:
            return None
        if not self._installed:
            return f"Error: Hermes no está instalado. {_INSTALL_HINT}"

        cmd = [
            self.python, self.bridge,
            "--hermes-dir", self.hermes_dir,
            "--base-url", self.base_url,
            "--api-key", self.api_key,
            "--model", self.model,
            "--max-iterations", str(self.max_iterations),
            "--max-tokens", str(self.max_tokens),
            "--enabled-toolsets", self.enabled_toolsets,
            "--disabled-toolsets", self.disabled_toolsets,
        ]
        if self.skip_memory:
            cmd.append("--skip-memory")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=None,   # el stderr del puente va al log del servicio, es útil
            )
        except OSError as e:
            logger.error(f"No se pudo arrancar el puente de Hermes: {e}")
            return "Error: no se pudo arrancar Hermes."

        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=120)
        except asyncio.TimeoutError:
            self._kill()
            return "Error: Hermes tardó demasiado en arrancar."
        if not line or not json.loads(line.decode()).get("ready"):
            self._kill()
            return "Error: Hermes no arrancó correctamente."
        logger.info(f"Hermes listo (modelo {self.model}).")
        return None

    def _kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        self._proc = None

    # ---- contrato: inferencia ----
    async def chat_stream(
        self,
        prompt: str,
        tools: Optional[List[Callable]] = None,   # se ignora: las tools son de Hermes
        system_prompt: Optional[str] = None,
        history: Optional[List[Any]] = None,
        image_path: Optional[str] = None,         # sin visión por ahora
    ) -> AsyncIterator[str]:
        async with self._lock:
            self.last_turn_tools_used.clear()
            self.last_turn_tools_disabled = False

            err = await self._ensure_proc()
            if err:
                yield err
                return

            self._task_seq += 1
            req = {
                "prompt": prompt,
                "system_prompt": system_prompt or "",
                "history": _history_to_dicts(history),
                "task_id": f"luka-{self._task_seq}",
            }
            try:
                self._proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode())
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                self._kill()
                yield "Error: se perdió la conexión con Hermes."
                return

            # Igual que ExLlamaEngine: se traga el espacio en blanco de cabecera. Tras un
            # bloque de razonamiento el modelo suele abrir con varios saltos de línea, y
            # eso llega a la UI como huecos y al TTS como una pausa sin motivo.
            seen = False
            while True:
                try:
                    raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    self._kill()
                    yield "Error: Hermes ha tardado demasiado en responder."
                    return
                if not raw:                      # el proceso murió a mitad de turno
                    self._kill()
                    yield "Error: Hermes se ha cerrado inesperadamente."
                    return
                try:
                    msg = json.loads(raw.decode())
                except json.JSONDecodeError:
                    continue                     # línea corrupta: se ignora, no se aborta

                if "delta" in msg:
                    text = msg["delta"]
                    if not text:
                        continue
                    if not seen:
                        text = text.lstrip()
                        if not text:
                            continue
                        seen = True
                    yield text
                elif "tool" in msg:
                    self.last_turn_tools_used.append(msg["tool"])
                elif msg.get("done"):
                    for name in msg.get("tools") or []:
                        if name not in self.last_turn_tools_used:
                            self.last_turn_tools_used.append(name)
                    if msg.get("error"):
                        logger.error(f"Hermes falló el turno: {msg['error']}")
                        yield "Error: Hermes no ha podido completar la tarea."
                    return

    async def chat(
        self,
        prompt: str,
        tools: Optional[List[Callable]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[List[Any]] = None,
        image_path: Optional[str] = None,
    ) -> str:
        parts: List[str] = []
        async for chunk in self.chat_stream(prompt, tools, system_prompt, history, image_path):
            parts.append(chunk)
        return "".join(parts).strip()

    async def transcribe_audio(self, audio_path: str) -> str:
        return ""   # sin audio: el STT usa Whisper (capabilities.audio=False)

    def reset_conversation(self) -> None:
        """Instancia nueva de agente en el puente. Hermes pide una por tarea."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.stdin.write(b'{"reset": true}\n')
        except (BrokenPipeError, ConnectionResetError):
            self._kill()

    def close(self) -> None:
        self._kill()


def _history_to_dicts(history) -> list[dict]:
    """Historial de Luka -> el formato que espera Hermes (lista de {role, content})."""
    out: list[dict] = []
    for m in history or []:
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
        content = getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else None)
        if role and content is not None:
            out.append({"role": role, "content": content})
    return out
