"""Tests de la tool de Google Calendar (agenda/search/create/delete/update) con la API mockeada."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.integrations.google import calendar


def _fake_service(items: list[dict] | None = None,
                  get_map: dict[str, dict] | None = None) -> MagicMock:
    """Mock del cliente Calendar: events().list/get/insert/delete/patch encadenados."""
    svc = MagicMock()
    events = svc.events.return_value
    events.list.return_value.execute.return_value = {"items": items or []}

    def _get(calendarId, eventId, **kw):
        result = MagicMock()
        result.execute.return_value = (get_map or {}).get(eventId, {"summary": "(sin título)"})
        return result

    events.get.side_effect = _get
    # insert/patch devuelven el evento creado/editado para el texto de respuesta.
    events.insert.return_value.execute.return_value = {
        "id": "new1", "summary": "Dentista",
        "start": {"dateTime": "2026-06-13T17:00:00+02:00"}}
    events.patch.return_value.execute.return_value = {
        "id": "a1", "summary": "Reunión",
        "start": {"dateTime": "2026-06-14T10:00:00+02:00"}}
    return svc


def _timed_event(eid: str, summary: str, dt: str, **extra) -> dict:
    ev = {"id": eid, "summary": summary, "start": {"dateTime": dt}}
    ev.update(extra)
    return ev


@pytest.fixture(autouse=True)
def _reset_state():
    calendar._last_ids = []
    calendar._last_rows = []
    calendar._pending = None
    yield
    calendar._pending = None


@pytest.mark.asyncio
async def test_agenda_numbers_and_caches():
    items = [
        _timed_event("a1", "Reunión", "2026-06-13T10:00:00+02:00", location="Sala 2"),
        _timed_event("b2", "Comida", "2026-06-13T14:00:00+02:00"),
    ]
    with patch.object(calendar, "_service", return_value=_fake_service(items)):
        out = await calendar.calendar_manager("agenda")
    assert "1." in out and "Reunión" in out
    assert "2." in out and "Comida" in out
    assert "Sala 2" in out
    assert calendar._last_ids == ["a1", "b2"]


@pytest.mark.asyncio
async def test_agenda_empty_is_friendly():
    with patch.object(calendar, "_service", return_value=_fake_service([])):
        out = await calendar.calendar_manager("agenda", time_range="hoy")
    assert "No tienes eventos" in out


@pytest.mark.asyncio
async def test_create_timed_event_executes_directly():
    svc = _fake_service()
    with patch.object(calendar, "_service", return_value=svc):
        out = await calendar.calendar_manager(
            "create", summary="Dentista", start="2026-06-13T17:00")
    assert "creado" in out.lower()
    svc.events.return_value.insert.assert_called_once()
    # No deja nada pendiente: crear es reversible y no se confirma.
    assert calendar.peek_pending() is None


@pytest.mark.asyncio
async def test_create_all_day_sets_exclusive_end():
    svc = _fake_service()
    with patch.object(calendar, "_service", return_value=svc):
        await calendar.calendar_manager("create", summary="Vacaciones", start="2026-06-13")
    body = svc.events.return_value.insert.call_args.kwargs["body"]
    assert body["start"] == {"date": "2026-06-13"}
    assert body["end"] == {"date": "2026-06-14"}      # end.date es exclusivo


@pytest.mark.asyncio
async def test_create_uses_duration_when_no_end():
    svc = _fake_service()
    with patch.object(calendar, "_service", return_value=svc):
        await calendar.calendar_manager(
            "create", summary="Llamada", start="2026-06-13T09:00", duration_minutes=30)
    body = svc.events.return_value.insert.call_args.kwargs["body"]
    start = datetime.fromisoformat(body["start"]["dateTime"])
    end = datetime.fromisoformat(body["end"]["dateTime"])
    assert end - start == timedelta(minutes=30)


@pytest.mark.asyncio
async def test_create_with_attendees_sends_updates():
    svc = _fake_service()
    with patch.object(calendar, "_service", return_value=svc):
        out = await calendar.calendar_manager(
            "create", summary="Sync", start="2026-06-13T11:00",
            attendees="ana@x.com, pepe@y.com")
    kwargs = svc.events.return_value.insert.call_args.kwargs
    assert kwargs["sendUpdates"] == "all"
    assert kwargs["body"]["attendees"] == [{"email": "ana@x.com"}, {"email": "pepe@y.com"}]
    assert "invitado" in out


@pytest.mark.asyncio
async def test_create_without_title_is_friendly():
    with patch.object(calendar, "_service", return_value=_fake_service()):
        out = await calendar.calendar_manager("create", start="2026-06-13T17:00")
    assert "título" in out.lower()


@pytest.mark.asyncio
async def test_create_with_bad_date_is_friendly():
    with patch.object(calendar, "_service", return_value=_fake_service()):
        out = await calendar.calendar_manager("create", summary="X", start="el martes")
    assert "ISO 8601" in out


@pytest.mark.asyncio
async def test_delete_by_number_stages_then_executes():
    items = [_timed_event("a1", "Reunión", "2026-06-13T10:00:00+02:00")]
    svc = _fake_service(items, get_map={"a1": items[0]})
    with patch.object(calendar, "_service", return_value=svc):
        await calendar.calendar_manager("agenda")
        out = await calendar.calendar_manager("delete", event_id="1")
        assert "borre" in out.lower()
        assert calendar.peek_pending() is not None
        svc.events.return_value.delete.assert_not_called()   # aún no
        result = await calendar.run_pending()
    assert "borrado" in result.lower()
    svc.events.return_value.delete.assert_called_once()
    assert calendar.peek_pending() is None


@pytest.mark.asyncio
async def test_delete_without_listing_is_friendly():
    with patch.object(calendar, "_service", return_value=_fake_service()):
        out = await calendar.calendar_manager("delete", event_id="2")
    assert "listado" in out.lower()
    assert calendar.peek_pending() is None


@pytest.mark.asyncio
async def test_delete_out_of_range_says_range():
    items = [_timed_event("a1", "Reunión", "2026-06-13T10:00:00+02:00")]
    with patch.object(calendar, "_service", return_value=_fake_service(items, get_map={"a1": items[0]})):
        await calendar.calendar_manager("agenda")
        out = await calendar.calendar_manager("delete", event_id="9")
    assert "del 1 al 1" in out


@pytest.mark.asyncio
async def test_update_stages_new_time():
    items = [_timed_event("a1", "Reunión", "2026-06-13T10:00:00+02:00")]
    svc = _fake_service(items, get_map={"a1": {"summary": "Reunión"}})
    with patch.object(calendar, "_service", return_value=svc):
        await calendar.calendar_manager("agenda")
        out = await calendar.calendar_manager("update", event_id="1", start="2026-06-14T10:00")
        assert "aplico" in out.lower()
        pending = calendar.peek_pending()
        assert pending["kind"] == "update"
        assert pending["id"] == "a1"
        assert pending["patch"]["start"]["dateTime"].startswith("2026-06-14T10:00")
        result = await calendar.run_pending()
    assert "actualizado" in result.lower()
    svc.events.return_value.patch.assert_called_once()


@pytest.mark.asyncio
async def test_update_without_changes_is_friendly():
    items = [_timed_event("a1", "Reunión", "2026-06-13T10:00:00+02:00")]
    with patch.object(calendar, "_service", return_value=_fake_service(items, get_map={"a1": items[0]})):
        await calendar.calendar_manager("agenda")
        out = await calendar.calendar_manager("update", event_id="1")
    assert "qué cambiar" in out.lower()
    assert calendar.peek_pending() is None


@pytest.mark.asyncio
async def test_resolve_ref_by_title():
    items = [_timed_event("a1", "Cita dentista", "2026-06-13T10:00:00+02:00")]
    with patch.object(calendar, "_service", return_value=_fake_service(items)):
        await calendar.calendar_manager("agenda")
    assert calendar._resolve_ref("dentista")[0] == "a1"


@pytest.mark.asyncio
async def test_search_no_results_is_helpful():
    with patch.object(calendar, "_service", return_value=_fake_service([])):
        out = await calendar.calendar_manager("search", query="boda")
    assert "No encontré" in out


@pytest.mark.asyncio
async def test_unknown_action():
    out = await calendar.calendar_manager("frobnicate")
    assert "no reconocida" in out.lower()


@pytest.mark.asyncio
async def test_accepts_integer_event_id():
    # El modelo a veces pasa el nº como entero: no debe romper.
    items = [_timed_event("a1", "Reunión", "2026-06-13T10:00:00+02:00")]
    with patch.object(calendar, "_service", return_value=_fake_service(items, get_map={"a1": items[0]})):
        await calendar.calendar_manager("agenda")
        out = await calendar.calendar_manager("delete", event_id=1)
    assert "borre" in out.lower()


@pytest.mark.asyncio
async def test_run_pending_with_nothing_staged():
    out = await calendar.run_pending()
    assert "pendiente" in out.lower()


def test_window_today_and_tomorrow():
    tz = calendar._local_tz()
    now = datetime.now(tz)
    lo, hi, label = calendar._window("hoy", "", "")
    assert label == "hoy"
    assert datetime.fromisoformat(lo).date() == now.date()
    lo2, _hi2, label2 = calendar._window("mañana", "", "")
    assert label2 == "mañana"
    assert datetime.fromisoformat(lo2).date() == (now + timedelta(days=1)).date()
