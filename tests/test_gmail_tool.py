"""Tests de la tool de Gmail (Fase 1: list/search/read; Fase 2: send/reply/draft/trash/archive) con la API mockeada."""
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
    gmail._pending = None
    yield
    gmail._pending = None


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


# --- Fase 2: escritura con confirmación ---

@pytest.mark.asyncio
async def test_send_stages_and_run_pending_executes():
    svc = MagicMock()
    with patch.object(gmail, "_service", return_value=svc):
        out = await gmail.gmail_manager("send", to="ana@x.com", subject="Hola", body="Qué tal")
        assert "¿Lo envío?" in out
        assert gmail.peek_pending() is not None      # acción a la espera de confirmación
        svc.users.return_value.messages.return_value.send.assert_not_called()  # aún no se envía
        result = await gmail.run_pending()
    assert "enviado" in result.lower()
    assert gmail.peek_pending() is None              # consumida tras ejecutar
    svc.users.return_value.messages.return_value.send.assert_called_once()


@pytest.mark.asyncio
async def test_send_without_valid_address_does_not_stage():
    with patch.object(gmail, "_service", return_value=MagicMock()):
        out = await gmail.gmail_manager("send", to="ana", subject="Hola", body="x")
    assert "válida" in out
    assert gmail.peek_pending() is None


@pytest.mark.asyncio
async def test_reply_resolves_recipient_from_listed_message():
    messages = [{"id": "a1", "threadId": "t1", "labelIds": ["INBOX"], "snippet": "s",
                 "payload": {"headers": [{"name": "From", "value": "Ana <ana@x.com>"},
                                         {"name": "Subject", "value": "Reunión"},
                                         {"name": "Message-ID", "value": "<m1@x>"}]}}]
    svc = _fake_service(messages)
    with patch.object(gmail, "_service", return_value=svc):
        await gmail.gmail_manager("list")
        out = await gmail.gmail_manager("reply", message_id="1", body="Vale")
    assert "¿La envío?" in out
    pending = gmail.peek_pending()
    assert pending["to"] == "Ana <ana@x.com>"
    assert pending["subject"] == "Re: Reunión"
    assert pending["thread_id"] == "t1"
    assert pending["in_reply_to"] == "<m1@x>"


@pytest.mark.asyncio
async def test_trash_stages_and_executes():
    messages = [{"id": "a1", "labelIds": ["INBOX"], "snippet": "s",
                 "payload": {"headers": [{"name": "From", "value": "Ana <ana@x.com>"},
                                         {"name": "Subject", "value": "Spam"}]}}]
    svc = _fake_service(messages)
    with patch.object(gmail, "_service", return_value=svc):
        await gmail.gmail_manager("list")
        out = await gmail.gmail_manager("trash", message_id="1")
        assert "papelera" in out.lower()
        result = await gmail.run_pending()
    assert "papelera" in result.lower()
    svc.users.return_value.messages.return_value.trash.assert_called_once()


@pytest.mark.asyncio
async def test_archive_removes_inbox_label():
    messages = [{"id": "a1", "labelIds": ["INBOX"], "snippet": "s",
                 "payload": {"headers": [{"name": "From", "value": "a@b.c"},
                                         {"name": "Subject", "value": "X"}]}}]
    svc = _fake_service(messages)
    with patch.object(gmail, "_service", return_value=svc):
        await gmail.gmail_manager("list")
        await gmail.gmail_manager("archive", message_id="1")
        await gmail.run_pending()
    modify = svc.users.return_value.messages.return_value.modify
    modify.assert_called_once()
    assert modify.call_args.kwargs["body"] == {"removeLabelIds": ["INBOX"]}


@pytest.mark.asyncio
async def test_draft_does_not_require_confirmation():
    svc = MagicMock()
    with patch.object(gmail, "_service", return_value=svc):
        out = await gmail.gmail_manager("draft", to="ana@x.com", subject="Hola", body="x")
    assert "borrador" in out.lower()
    assert gmail.peek_pending() is None              # los borradores no se confirman
    svc.users.return_value.drafts.return_value.create.assert_called_once()


@pytest.mark.asyncio
async def test_pending_expires_after_ttl(monkeypatch):
    with patch.object(gmail, "_service", return_value=MagicMock()):
        await gmail.gmail_manager("send", to="ana@x.com", subject="Hola", body="x")
    assert gmail.peek_pending() is not None
    # Avanzar el reloj más allá del TTL: la acción debe descartarse sola.
    monkeypatch.setattr(gmail.time, "monotonic",
                        lambda: gmail._pending_ts + gmail._PENDING_TTL + 1)
    assert gmail.peek_pending() is None


@pytest.mark.asyncio
async def test_run_pending_with_nothing_staged():
    out = await gmail.run_pending()
    assert "pendiente" in out.lower()
