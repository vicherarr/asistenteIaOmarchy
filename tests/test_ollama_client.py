"""Tests para src/ollama_client.py"""

import json
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from src.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaMessage,
)
from src.config import settings


@pytest.fixture
def client():
    return OllamaClient()


@pytest.fixture
def mock_response_success():
    return {
        "model": settings.OLLAMA_MODEL,
        "message": {"role": "assistant", "content": "Hola, soy Gemma."},
        "done": True,
    }


@pytest.mark.asyncio
async def test_health_check_success(client):
    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch.object(client._client, 'get', new_callable=AsyncMock, return_value=mock_response):
        result = await client.health_check()

    assert result is True


@pytest.mark.asyncio
async def test_health_check_failure(client):
    with patch.object(client._client, 'get', new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
        result = await client.health_check()

    assert result is False


@pytest.mark.asyncio
async def test_generate_success(client, mock_response_success):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_response_success
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, 'post', new_callable=AsyncMock, return_value=mock_response):
        result = await client.generate([
            OllamaMessage(role="system", content="Eres un asistente"),
            OllamaMessage(role="user", content="Hola"),
        ])

    assert result == "Hola, soy Gemma."


@pytest.mark.asyncio
async def test_generate_connect_error(client):
    with patch.object(client._client, 'post', new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
        with pytest.raises(OllamaError, match="No se puede conectar"):
            await client.generate([OllamaMessage(role="user", content="Hola")])


@pytest.mark.asyncio
async def test_generate_http_error(client):
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=mock_response
    )

    with patch.object(client._client, 'post', new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(OllamaError, match="Error HTTP"):
            await client.generate([OllamaMessage(role="user", content="Hola")])


@pytest.mark.asyncio
async def test_generate_json_decode_error(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.side_effect = json.JSONDecodeError("Expecting value", "doc", 0)
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, 'post', new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(OllamaError, match="Respuesta JSON inválida"):
            await client.generate([OllamaMessage(role="user", content="Hola")])


@pytest.mark.asyncio
async def test_generate_with_custom_model(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "model": "llama3",
        "message": {"role": "assistant", "content": "Respuesta de llama3"},
        "done": True,
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, 'post', new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.generate(
            [OllamaMessage(role="user", content="Hola")],
            model="llama3",
        )

    call_args = mock_post.call_args
    assert call_args[1]["json"]["model"] == "llama3"


@pytest.mark.asyncio
async def test_generate_legacy_response_format(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "model": settings.OLLAMA_MODEL,
        "response": "Respuesta formato legacy",
        "done": True,
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, 'post', new_callable=AsyncMock, return_value=mock_response):
        result = await client.generate([OllamaMessage(role="user", content="Hola")])

    assert result == "Respuesta formato legacy"


def test_ollama_message_model():
    msg = OllamaMessage(role="user", content="Hola")
    assert msg.role == "user"
    assert msg.content == "Hola"


def test_client_default_config():
    client = OllamaClient()
    assert client.base_url == settings.OLLAMA_BASE_URL
    assert client.model == settings.OLLAMA_MODEL
    assert client.timeout == settings.OLLAMA_TIMEOUT


def test_client_custom_config():
    client = OllamaClient(base_url="http://custom:11434", model="llama3", timeout=60.0)
    assert client.base_url == "http://custom:11434"
    assert client.model == "llama3"
    assert client.timeout == 60.0
