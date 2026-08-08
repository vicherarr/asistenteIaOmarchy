"""Motor de inferencia OpenRouter: un LLM en la nube, por HTTP.

A diferencia de los otros dos motores, aquí no hay pesos ni VRAM: se le habla a la API
de OpenRouter (OpenAI-compatible) y el modelo corre en el proveedor. Eso da acceso a
modelos mucho más grandes de los que caben en la tarjeta —con 256k de contexto— sin
tocar el hardware, a cambio de depender de la red y de la cuota del tier gratuito.

Hereda de ExLlamaEngine porque ese módulo YA es el cliente OpenAI-compatible del
proyecto: el streaming SSE, la conversión de tools a function specs, la acumulación de
`delta.tool_calls` y el bucle agéntico (modelo -> tool -> modelo) son exactamente los
mismos. Lo único que cambia es el transporte, así que aquí solo se redefine eso:
URL, autenticación, cabeceras de atribución, salud y el etiquetado de la imagen.

Solo se usan modelos GRATIS y con tool calling; de eso se encarga la CLI
('asistenteia engine openrouter model'), que no deja seleccionar otros.
"""

from __future__ import annotations

import logging

import httpx

from src.config import settings as _settings
from src.engines.base import EngineCapabilities
from src.engines.exllama_engine import ExLlamaEngine

logger = logging.getLogger(__name__)

# Cabeceras de atribución de OpenRouter: identifican a la app en su panel. Son
# opcionales para la API, pero es lo correcto y ayuda a diagnosticar cuotas.
_APP_URL = "https://github.com/vicherarr/asistenteIaOmarchy"
_APP_TITLE = "Luka (AsistenteIA)"

_KEY_HINT = "Configúrala con: asistenteia engine openrouter key <TU_API_KEY>"


class OpenRouterEngine(ExLlamaEngine):
    """Cliente de OpenRouter conforme a InferenceEngine."""

    def __init__(self, settings=_settings):
        # No se llama a super().__init__: el del padre hace un ping SÍNCRONO al arrancar
        # (bloquea el arranque de la app contra un servidor local). Contra un endpoint
        # remoto eso es latencia y un punto de fallo gratuito, así que aquí no se pinga.
        self.base_url = settings.OPENROUTER_BASE_URL.rstrip("/")
        self.model = settings.OPENROUTER_MODEL
        self.api_key = settings.OPENROUTER_API_KEY.strip()
        self.timeout = settings.OPENROUTER_TIMEOUT
        self.max_tokens = settings.OPENROUTER_MAX_TOKENS
        self.temperature = settings.OPENROUTER_TEMPERATURE
        self.vision = settings.OPENROUTER_VISION
        self.max_tool_rounds = settings.OPENROUTER_MAX_TOOL_ROUNDS
        self.fallback_models = [
            m.strip() for m in (settings.OPENROUTER_FALLBACK_MODELS or "").split(",") if m.strip()
        ]
        # El padre usa self.thinking para el sufijo '/no_think' y para leads_with_reasoning;
        # aquí el razonamiento viaja en `delta.reasoning`, un campo que el bucle ignora, así
        # que nunca se habla ni se muestra (que es lo que queremos en un asistente de voz).
        self.thinking = False

        self._headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": _APP_URL,
            "X-Title": _APP_TITLE,
        }
        if self.api_key:
            self._headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(timeout=self.timeout, headers=self._headers)

        self._ready = bool(self.api_key)
        if self._ready:
            logger.info(f"OpenRouter listo (modelo {self.model}, visión: {self.vision}).")
        else:
            logger.warning(f"OpenRouter sin API key. {_KEY_HINT}")

        # Verdad de campo del turno para el guardarraíl anti-invención (assistant_service):
        # el bucle de tools es nuestro, así que sabemos con certeza qué se ejecutó.
        self.last_turn_tools_used: list[str] = []
        self.last_turn_tools_disabled: bool = False
        self.tracks_tool_usage: bool = True

    # ---- metadatos / salud ----
    @property
    def name(self) -> str:
        return "OpenRouter"

    @property
    def _engine_label(self) -> str:
        return "OpenRouter"

    @property
    def model_label(self) -> str:
        return self.model  # p.ej. "google/gemma-4-31b-it:free"

    def _ping(self) -> bool:
        """Salud sin red: con key, se asume disponible. El fallo real se detecta al
        inferir y se cuenta con un mensaje concreto (401/429/red)."""
        if not self.api_key:
            logger.warning(f"OpenRouter sin API key. {_KEY_HINT}")
            return False
        return True

    @property
    def is_ready(self) -> bool:
        return bool(self.api_key)

    def backend_label(self) -> str:
        return "Nube (OpenRouter)" if self.api_key else "Desconectado"

    @property
    def capabilities(self) -> EngineCapabilities:
        # gpu=False: el modelo no corre aquí. audio=False: el STT cae a Whisper, como
        # con exllama. vision según el modelo elegido (la CLI ajusta el flag sola).
        return EngineCapabilities(tools=True, vision=self.vision, audio=False, gpu=False)

    @property
    def leads_with_reasoning(self) -> bool:
        return False  # el razonamiento va en `delta.reasoning`, que no se emite al stream

    # ---- transporte ----
    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"  # la base ya incluye /v1

    @property
    def _system_suffix(self) -> str:
        return ""  # '/no_think' es de Qwen; para otros modelos es basura en el prompt

    def _extra_payload(self) -> dict:
        """Lista de reserva de OpenRouter: si el modelo principal falla (típicamente un
        429 del pool gratuito compartido, que se satura a ratos aunque nuestra cuota esté
        intacta), enruta solo al siguiente. Sin esto, un pico de uso ajeno deja a Luka
        mudo; con esto, contesta otro modelo gratis y el usuario no se entera."""
        if not self.fallback_models:
            return {}
        modelos = [self.model] + [m for m in self.fallback_models if m != self.model]
        return {"models": modelos}

    def _image_mime(self, path: str, raw: bytes) -> str:
        """MIME real de la imagen. Importa: las fotos de la cámara del satélite llegan en
        JPEG (device_gateway) y las capturas redimensionadas también, y un proveedor en la
        nube puede rechazar un data-URI etiquetado como PNG que no lo es."""
        if raw.startswith(b"\xff\xd8"):
            return "image/jpeg"
        if raw.startswith(b"\x89PNG"):
            return "image/png"
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return "image/webp"
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")

    def _http_error_message(self, exc: Exception) -> str:
        """Mensaje hablado cuando la petición falla. Se distinguen los dos fallos que de
        verdad pasan: la key mal puesta y la cuota diaria del tier gratuito."""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            return "Error: la API key de OpenRouter no es válida. Vuelve a configurarla."
        if status == 429:
            return ("Error: OpenRouter está limitando las peticiones; seguramente se ha "
                    "agotado la cuota diaria de los modelos gratuitos.")
        if status == 404:
            return f"Error: OpenRouter no reconoce el modelo {self.model}."
        if status and status >= 500:
            return "Error: el proveedor de OpenRouter está fallando ahora mismo."
        return "Error: no he podido conectar con OpenRouter. Revisa la conexión."

    async def chat_stream(self, prompt, tools=None, system_prompt=None, history=None, image_path=None):
        if not self.api_key:
            yield f"Error: falta la API key de OpenRouter. {_KEY_HINT}"
            return
        async for chunk in super().chat_stream(prompt, tools, system_prompt, history, image_path):
            yield chunk
