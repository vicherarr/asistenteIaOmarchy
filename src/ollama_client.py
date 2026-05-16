"""Cliente para la API local de Ollama (Gemma 4:e4b)."""

import json
import logging
from typing import AsyncGenerator, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:e2b"


class OllamaMessage(BaseModel):
    role: str
    content: str
    images: list[str] = Field(default_factory=list)


class OllamaRequest(BaseModel):
    model: str = Field(default=DEFAULT_MODEL)
    messages: list[OllamaMessage]
    stream: bool = Field(default=False)
    options: dict = Field(default_factory=lambda: {
        "temperature": 0.3,
        "num_ctx": 4096,
    })


class OllamaResponse(BaseModel):
    model: str
    message: OllamaMessage
    done: bool


class OllamaError(Exception):
    """Errores del cliente Ollama."""
    pass


class OllamaClient:
    """Cliente asíncrono para comunicarse con Ollama local."""

    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
        )

    async def health_check(self) -> bool:
        """Verifica que Ollama esté respondiendo."""
        try:
            response = await self._client.get("/api/tags")
            return response.status_code == 200
        except httpx.ConnectError:
            logger.error("No se puede conectar a Ollama en %s", self.base_url)
            return False

    async def generate(
        self,
        messages: list[OllamaMessage],
        model: Optional[str] = None,
    ) -> str:
        """
        Genera una respuesta del modelo.
        messages: lista de mensajes con role y content.
        Devuelve el texto de la respuesta.
        """
        request = OllamaRequest(
            model=model or self.model,
            messages=messages,
            stream=False,
        )

        try:
            response = await self._client.post(
                "/api/chat",
                json=request.model_dump(),
            )
            response.raise_for_status()
            data = response.json()

            if "message" in data:
                return data["message"]["content"]
            elif "response" in data:
                return data["response"]
            else:
                raise OllamaError(f"Respuesta inesperada de Ollama: {data}")

        except httpx.HTTPStatusError as e:
            raise OllamaError(f"Error HTTP de Ollama: {e.response.status_code} - {e.response.text}")
        except httpx.ConnectError:
            raise OllamaError("No se puede conectar a Ollama. Verificar que el servicio esté activo.")
        except json.JSONDecodeError:
            raise OllamaError("Respuesta JSON inválida de Ollama")

    async def generate_with_image(
        self,
        text: str,
        image_base64: str,
        model: Optional[str] = None,
    ) -> str:
        """
        Genera una respuesta del modelo con una imagen (multimodal).
        image_base64: imagen codificada en base64.
        Devuelve el texto de la respuesta.
        """
        request = OllamaRequest(
            model=model or self.model,
            messages=[
                OllamaMessage(
                    role="user",
                    content=text,
                    images=[image_base64],
                )
            ],
            stream=False,
        )

        try:
            response = await self._client.post(
                "/api/chat",
                json=request.model_dump(),
            )
            response.raise_for_status()
            data = response.json()

            if "message" in data:
                return data["message"]["content"]
            elif "response" in data:
                return data["response"]
            else:
                raise OllamaError(f"Respuesta inesperada de Ollama: {data}")

        except httpx.HTTPStatusError as e:
            raise OllamaError(f"Error HTTP de Ollama: {e.response.status_code} - {e.response.text}")
        except httpx.ConnectError:
            raise OllamaError("No se puede conectar a Ollama. Verificar que el servicio esté activo.")
        except json.JSONDecodeError:
            raise OllamaError("Respuesta JSON inválida de Ollama")

    async def generate_stream(
        self,
        messages: list[OllamaMessage],
        model: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Genera una respuesta en streaming (token a token).
        Útil para mostrar progreso en tiempo real.
        """
        request = OllamaRequest(
            model=model or self.model,
            messages=messages,
            stream=True,
        )

        try:
            async with self._client.stream(
                "POST",
                "/api/chat",
                json=request.model_dump(),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if "message" in data:
                        yield data["message"].get("content", "")
                    elif "response" in data:
                        yield data.get("response", "")
                    if data.get("done", False):
                        break

        except httpx.HTTPStatusError as e:
            raise OllamaError(f"Error HTTP de Ollama: {e.response.status_code}")
        except httpx.ConnectError:
            raise OllamaError("No se puede conectar a Ollama")

    async def close(self) -> None:
        """Cierra el cliente HTTP."""
        await self._client.aclose()
