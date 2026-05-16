"""Tests de integración para src/main.py (FastAPI)"""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app, conversation_history


@pytest.fixture
def client():
    conversation_history.clear()
    return TestClient(app)


@pytest.fixture
def mock_ollama_response():
    return {
        "model": "gemma4:e4b",
        "message": {
            "role": "assistant",
            "content": '''```json
{
    "response_text": "Abriendo Spotify para ti",
    "commands": [
        {"command": "omarchy launch spotify", "description": "Launch Spotify"}
    ],
    "action_type": "both"
}
```'''
        },
        "done": True,
    }


def test_status_endpoint(client):
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "ollama_connected" in data
    assert "bluetooth_audio" in data
    assert "conversation_length" in data


def test_transcribe_empty(client):
    response = client.post("/transcribe", json={"text": ""})
    assert response.status_code == 400

    response = client.post("/transcribe", json={"text": "   "})
    assert response.status_code == 400


def test_transcribe_success(client, mock_ollama_response):
    with patch('src.main.ollama_client') as mock_client:
        mock_client.generate = AsyncMock(return_value=mock_ollama_response["message"]["content"])
        mock_client.health_check = AsyncMock(return_value=True)

        with patch('src.main.command_executor') as mock_executor:
            mock_executor.execute_multiple = MagicMock(return_value=[(True, "OK")])

            with patch('src.main.tts_engine') as mock_tts:
                mock_tts.speak_async = AsyncMock(return_value="/tmp/test.wav")

                response = client.post("/transcribe", json={
                    "text": "Abre Spotify por favor"
                })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "Spotify" in data["response_text"]
    assert data["commands_executed"] == 1


def test_transcribe_ollama_failure(client):
    with patch('src.main.ollama_client') as mock_client:
        mock_client.generate = AsyncMock(side_effect=Exception("Ollama connection refused"))

        response = client.post("/transcribe", json={
            "text": "Hola"
        })

    assert response.status_code == 502
    assert "Ollama" in response.json()["detail"]


def test_reset_conversation(client):
    conversation_history.append({"role": "user", "content": "test"})
    assert len(conversation_history) > 0

    response = client.post("/reset")
    assert response.status_code == 200
    assert len(conversation_history) == 0


def test_configure_audio(client):
    with patch('src.main.audio_manager') as mock_audio:
        mock_audio.auto_configure_bluetooth = MagicMock(return_value=("46", "45"))

        response = client.post("/audio/configure")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "configured"
    assert data["source"] == "46"
    assert data["sink"] == "45"


def test_conversation_history_grows(client, mock_ollama_response):
    with patch('src.main.ollama_client') as mock_client:
        mock_client.generate = AsyncMock(return_value=mock_ollama_response["message"]["content"])
        mock_client.health_check = AsyncMock(return_value=True)

        with patch('src.main.command_executor') as mock_executor:
            mock_executor.execute_multiple = MagicMock(return_value=[])

            with patch('src.main.tts_engine') as mock_tts:
                mock_tts.speak_async = AsyncMock(return_value=None)

                client.post("/transcribe", json={"text": "Mensaje 1"})
                client.post("/transcribe", json={"text": "Mensaje 2"})

    assert len(conversation_history) == 4


def test_transcribe_no_commands(client):
    response_text = "Hola, ¿en qué puedo ayudarte?"

    with patch('src.main.ollama_client') as mock_client:
        mock_client.generate = AsyncMock(return_value=response_text)
        mock_client.health_check = AsyncMock(return_value=True)

        with patch('src.main.command_executor') as mock_executor:
            mock_executor.execute_multiple = MagicMock(return_value=[])

            with patch('src.main.tts_engine') as mock_tts:
                mock_tts.speak_async = AsyncMock(return_value=None)

                response = client.post("/transcribe", json={
                    "text": "Hola"
                })

    assert response.status_code == 200
    data = response.json()
    assert data["response_text"] == response_text
    assert data["commands_executed"] == 0
