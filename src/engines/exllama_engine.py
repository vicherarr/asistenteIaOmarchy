"""Motor de inferencia ExLlamaV3 vía TabbyAPI (servidor OpenAI-compatible).

Implementa el contrato InferenceEngine hablando con un sidecar TabbyAPI por HTTP.
A diferencia de LiteRT (tool-calling nativo en proceso), aquí el bucle agéntico es
nuestro: el modelo emite `<tool_call>{json}</tool_call>` en el texto, lo parseamos,
ejecutamos la herramienta Python y reinyectamos el resultado, hasta la respuesta
final. El razonamiento `<think>...</think>` de Qwen3 se filtra (configurable).

No soporta audio (transcribe_audio -> ""): el STT cae a Whisper por capacidades.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import typing
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import httpx

from src.config import settings as _settings
from src.engines.base import EngineCapabilities

logger = logging.getLogger(__name__)

_PYTYPE_TO_JSON = {
    str: "string", int: "integer", float: "number",
    bool: "boolean", list: "array", dict: "object",
}

_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"
_TOOL_OPEN, _TOOL_CLOSE = "<tool_call>", "</tool_call>"
_MARKERS = (_TOOL_OPEN, _THINK_OPEN)


def _json_type(annotation) -> str:
    """Mapea una anotación Python a un tipo JSON-schema (desenvuelve Optional/Union)."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        for arg in typing.get_args(annotation):
            if arg is not type(None):
                return _json_type(arg)
    return _PYTYPE_TO_JSON.get(annotation, "string")


def callable_to_schema(func: Callable) -> dict:
    """Convierte una tool (callable con type hints + docstring) a una function spec OpenAI."""
    sig = inspect.signature(func)
    props: Dict[str, dict] = {}
    required: List[str] = []
    for name, p in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        props[name] = {"type": _json_type(p.annotation)}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    desc = (func.__doc__ or func.__name__).strip().split("\n")[0]
    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


class _StreamFilter:
    """Parser incremental de un stream de texto del modelo.

    Separa el texto natural (emitible al TTS) de los bloques `<think>...</think>`
    (descartados) y `<tool_call>...</tool_call>` (capturados y parseados como JSON).
    Maneja marcadores partidos entre chunks reteniendo el sufijo ambiguo.
    """

    def __init__(self):
        self._buf = ""
        self._mode = "normal"  # normal | think | tool
        self.tool_calls: List[dict] = []

    def _hold_len(self, s: str) -> int:
        """Longitud del sufijo de s que podría ser el inicio de un marcador."""
        best = 0
        for marker in _MARKERS:
            for k in range(1, min(len(s), len(marker) - 1) + 1):
                if s.endswith(marker[:k]):
                    best = max(best, k)
        return best

    def _capture_tool(self, inner: str) -> None:
        try:
            obj = json.loads(inner.strip())
            if isinstance(obj, dict) and obj.get("name"):
                args = obj.get("arguments", {})
                if isinstance(args, str):
                    args = json.loads(args) if args.strip() else {}
                self.tool_calls.append({"name": obj["name"], "arguments": args or {}})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"tool_call con JSON inválido, ignorado: {e} -- {inner!r}")

    def feed(self, chunk: str) -> List[str]:
        """Procesa un chunk; devuelve la lista de fragmentos de texto emitibles."""
        self._buf += chunk
        out: List[str] = []
        while True:
            if self._mode == "normal":
                it = self._buf.find(_THINK_OPEN)
                ic = self._buf.find(_TOOL_OPEN)
                idx = min([x for x in (it, ic) if x != -1], default=-1)
                if idx == -1:
                    hold = self._hold_len(self._buf)
                    safe = self._buf[: len(self._buf) - hold]
                    if safe:
                        out.append(safe)
                    self._buf = self._buf[len(self._buf) - hold:]
                    break
                if idx > 0:
                    out.append(self._buf[:idx])
                if idx == it:
                    self._buf = self._buf[idx + len(_THINK_OPEN):]
                    self._mode = "think"
                else:
                    self._buf = self._buf[idx + len(_TOOL_OPEN):]
                    self._mode = "tool"
            elif self._mode == "think":
                e = self._buf.find(_THINK_CLOSE)
                if e == -1:
                    break
                self._buf = self._buf[e + len(_THINK_CLOSE):]
                self._mode = "normal"
            else:  # tool
                e = self._buf.find(_TOOL_CLOSE)
                if e == -1:
                    break
                self._capture_tool(self._buf[:e])
                self._buf = self._buf[e + len(_TOOL_CLOSE):]
                self._mode = "normal"
        return out

    def flush(self) -> List[str]:
        """Texto restante al cerrar el stream (solo si quedó en modo normal)."""
        if self._mode == "normal" and self._buf:
            txt, self._buf = self._buf, ""
            return [txt]
        self._buf = ""
        return []


def _collect_structured(acc: Dict[int, dict]) -> List[dict]:
    """Convierte tool_calls acumuladas del stream ({idx: {name, arguments-str}}) a
    [{name, arguments-dict}], parseando el JSON de argumentos."""
    calls = []
    for idx in sorted(acc):
        name = acc[idx].get("name")
        if not name:
            continue
        raw_args = (acc[idx].get("arguments") or "").strip()
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            logger.warning(f"Argumentos de tool '{name}' no parsean: {raw_args!r}")
            args = {}
        calls.append({"name": name, "arguments": args})
    return calls


class ExLlamaEngine:
    """Cliente del sidecar TabbyAPI (ExLlamaV3) conforme a InferenceEngine."""

    def __init__(self, settings=_settings):
        self.base_url = settings.EXLLAMA_BASE_URL.rstrip("/")
        self.model = settings.EXLLAMA_MODEL
        self.timeout = settings.EXLLAMA_TIMEOUT
        self.max_tokens = settings.EXLLAMA_MAX_TOKENS
        self.temperature = settings.EXLLAMA_TEMPERATURE
        self.thinking = settings.EXLLAMA_THINKING
        self.vision = settings.EXLLAMA_VISION
        self.max_tool_rounds = settings.EXLLAMA_MAX_TOOL_ROUNDS
        self._headers = {"Content-Type": "application/json"}
        if settings.EXLLAMA_API_KEY:
            self._headers["Authorization"] = f"Bearer {settings.EXLLAMA_API_KEY}"
        self._client = httpx.AsyncClient(timeout=self.timeout, headers=self._headers)
        self._ready = self._ping()

    # ---- contrato: salud / metadatos ----
    def _ping(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/v1/models", headers=self._headers, timeout=3)
            ok = r.status_code == 200
            if ok:
                logger.info(f"ExLlama/TabbyAPI accesible en {self.base_url} (modelo {self.model}).")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ExLlama/TabbyAPI no accesible en {self.base_url}: {e}")
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    def backend_label(self) -> str:
        return "GPU (ExLlama)" if self._ready else "Desconectado"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(tools=True, vision=self.vision, audio=False, gpu=True)

    # ---- construcción de la petición ----
    def _build_messages(self, prompt, system_prompt, history, image_path) -> List[dict]:
        msgs: List[dict] = []
        sys_txt = (system_prompt or "").strip()
        if not self.thinking:
            sys_txt = (sys_txt + " /no_think").strip()
        if sys_txt:
            msgs.append({"role": "system", "content": sys_txt})
        for m in history or []:
            role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
            content = getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else None)
            if role and content is not None:
                msgs.append({"role": role, "content": content})
        if image_path and self.vision:
            try:
                b64 = base64.b64encode(open(image_path, "rb").read()).decode()
                msgs.append({"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]})
                return msgs
            except OSError as e:
                logger.warning(f"No se pudo leer la imagen {image_path}: {e}")
        msgs.append({"role": "user", "content": prompt})
        return msgs

    async def _stream_deltas(self, messages, tool_schemas) -> AsyncIterator[dict]:
        """Itera los `delta` del streaming OpenAI (puede traer `content` y/o `tool_calls`)."""
        payload = {
            "model": self.model, "messages": messages,
            "max_tokens": self.max_tokens, "temperature": self.temperature, "stream": True,
        }
        if tool_schemas:
            payload["tools"] = tool_schemas
            payload["tool_choice"] = "auto"
        async with self._client.stream("POST", f"{self.base_url}/v1/chat/completions", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)["choices"][0].get("delta", {})
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    async def _exec_tool(self, call: dict, tool_map: Dict[str, Callable]) -> str:
        name, args = call.get("name"), call.get("arguments") or {}
        fn = tool_map.get(name)
        if not fn:
            return f"Error: herramienta desconocida '{name}'."
        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**args)
            else:
                result = await asyncio.to_thread(fn, **args)
            return str(result)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error ejecutando tool {name}({args}): {e}", exc_info=True)
            return f"Error ejecutando '{name}': {e}"

    # ---- contrato: inferencia ----
    async def chat_stream(
        self,
        prompt: str,
        tools: Optional[List[Callable]] = None,
        system_prompt: Optional[str] = None,
        history: Optional[List[Any]] = None,
        image_path: Optional[str] = None,
    ) -> AsyncIterator[str]:
        if not self._ready and not self._ping():
            yield "Error: Motor ExLlama no disponible."
            return
        self._ready = True

        tool_schemas = [callable_to_schema(t) for t in tools] if tools else None
        tool_map = {t.__name__: t for t in (tools or [])}
        messages = self._build_messages(prompt, system_prompt, history, image_path)
        seen = False  # para suprimir el espacio en blanco inicial (de turnos solo-think)

        for _round in range(self.max_tool_rounds):
            flt = _StreamFilter()           # filtra <think> y, como fallback, <tool_call> en texto
            raw: List[str] = []
            structured: Dict[int, dict] = {}  # tool_calls estructuradas del stream (por index)
            try:
                async for delta in self._stream_deltas(messages, tool_schemas):
                    content = delta.get("content")
                    if content:
                        raw.append(content)
                        for text in flt.feed(content):
                            if not seen:
                                text = text.lstrip()
                                if not text:
                                    continue
                                seen = True
                            yield text
                    for tc in delta.get("tool_calls") or []:
                        acc = structured.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["arguments"] += fn["arguments"]
            except httpx.HTTPError as e:
                self._ready = False
                logger.error(f"Fallo en la inferencia ExLlama: {e}")
                yield "Error: fallo de comunicación con el motor ExLlama."
                return
            for text in flt.flush():
                if not seen:
                    text = text.lstrip()
                    if not text:
                        continue
                    seen = True
                yield text

            # tool calls estructuradas (preferente); si no, las parseadas del texto (fallback).
            calls = _collect_structured(structured) or flt.tool_calls
            if not calls:
                return  # respuesta final ya emitida

            # Reinyecta la(s) llamada(s) en formato OpenAI y sus resultados, y reitera.
            if structured:
                messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": f"call_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                    for i, c in enumerate(calls)
                ]})
            else:
                messages.append({"role": "assistant", "content": "".join(raw)})
            for call in calls:
                result = await self._exec_tool(call, tool_map)
                messages.append({"role": "tool", "content": result})

        logger.warning(f"ExLlama: alcanzado el límite de {self.max_tool_rounds} rondas de tools.")

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
        return ""  # ExLlama no procesa audio; el STT usa Whisper (capabilities.audio=False)

    def reset_conversation(self) -> None:
        pass  # sin estado de conversación en servidor: el historial se pasa por petición

    def close(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self._client.aclose())
        except RuntimeError:
            try:
                asyncio.run(self._client.aclose())
            except Exception:  # noqa: BLE001
                pass
