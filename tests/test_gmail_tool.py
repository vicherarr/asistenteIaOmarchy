"""Tests de la tool de Gmail (Fase 1: list/search/read) con la API mockeada."""
import base64
from unittest.mock import MagicMock, patch

import pytest

from src.integrations.google import gmail


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8")


def _fake_service(messages: list[dict], full: dict | None = None) -> MagicMock:
    """Construye un mock del cliente Gmail con list/get encadenados."""
    svc = MagicMock()
    users = svc.users.return_value
    msgs = users.messages.return_value
    msgs.list.return_value.execute.return_value = {"messages": [{"id": m["id"]} for m in messages]}

    def _get(userId, id, format, metadataHeaders=None):
        result = MagicMock()
        if format == "full" and full is not None:
            result.execute.return_value = full
        else:
            match = next(m for m in messages if m["id"] == id)
            result.execute.return_value = match
        return result

    msgs.get.side_effect = _get
    return svc


@pytest.fixture(autouse=True)
def _reset_cache():
    gmail._last_ids = []
    yield


@pytest.mark.asyncio
async def test_list_numbers_and_marks_unread():
    messages = [
        {"id": "a1", "labelIds": ["INBOX", "UNREAD"], "snippet": "hola",
         "payload": {"headers": [{"name": "From", "value": "Ana <ana@x.com>"},
                                 {"name": "Subject", "value": "Reunión"}]}},
        {"id": "b2", "labelIds": ["INBOX"], "snippet": "factura",
         "payload": {"headers": [{"name": "From", "value": "pepe@y.com"},
                                 {"name": "Subject", "value": "Pago"}]}},
    ]
    with patch.object(gmail, "_service", return_value=_fake_service(messages)):
        out = await gmail.gmail_manager("list", max_results=5)
    assert "1. ● Ana — Reunión" in out
    assert "2. pepe@y.com — Pago" in out      # leído: sin ●
    assert gmail._last_ids == ["a1", "b2"]      # caché para 'lee el N'


@pytest.mark.asyncio
async def test_read_by_index_uses_last_list():
    messages = [{"id": "a1", "labelIds": ["INBOX"], "snippet": "s",
                 "payload": {"headers": [{"name": "From", "value": "Ana <ana@x.com>"},
                                         {"name": "Subject", "value": "Hola"}]}}]
    full = {"payload": {"headers": [{"name": "From", "value": "Ana <ana@x.com>"},
                                    {"name": "Subject", "value": "Hola"},
                                    {"name": "Date", "value": "hoy"}],
                        "mimeType": "text/plain",
                        "body": {"data": _b64("Cuerpo del correo de prueba.")}}}
    svc = _fake_service(messages, full=full)
    with patch.object(gmail, "_service", return_value=svc):
        await gmail.gmail_manager("list")
        out = await gmail.gmail_manager("read", message_id="1")
    assert "Asunto: Hola" in out
    assert "Cuerpo del correo de prueba." in out


@pytest.mark.asyncio
async def test_read_invalid_index_is_friendly():
    with patch.object(gmail, "_service", return_value=_fake_service([])):
        out = await gmail.gmail_manager("read", message_id="9")
    assert "número 9" in out


@pytest.mark.asyncio
async def test_unknown_action():
    out = await gmail.gmail_manager("frobnicate")
    assert "no reconocida" in out.lower()


@pytest.mark.asyncio
async def test_body_truncation():
    long_body = "x" * (gmail._MAX_BODY_CHARS + 500)
    messages = [{"id": "a1", "labelIds": [], "snippet": "s",
                 "payload": {"headers": [{"name": "Subject", "value": "S"}]}}]
    full = {"payload": {"headers": [{"name": "From", "value": "a@b.c"},
                                    {"name": "Subject", "value": "S"}],
                        "mimeType": "text/plain", "body": {"data": _b64(long_body)}}}
    with patch.object(gmail, "_service", return_value=_fake_service(messages, full=full)):
        out = await gmail.gmail_manager("read", message_id="abc123")
    assert "truncado" in out
