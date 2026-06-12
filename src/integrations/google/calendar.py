"""Tool de Google Calendar para Luka: consultar la agenda, crear, mover y borrar eventos.

Diseño (calcado de gmail.py para mantener un patrón único):
- Una sola tool multiplexora `calendar_manager(action, ...)`.
- `googleapiclient` es síncrono → cada llamada va en un hilo (asyncio.to_thread).
- Los listados numeran los eventos (1..N) y guardan el mapeo nº→id en un caché de proceso,
  para que el usuario pueda decir "borra el 2" por voz sin manejar ids largos.
- CREAR se ejecuta directo (es fácil de deshacer). BORRAR y MOVER/EDITAR son irreversibles
  (y pueden mandar correos a invitados): se quedan en `stage_pending` y Luka pide el "sí/no"
  por voz; AssistantService resuelve la confirmación en el turno siguiente.
- El modelo debe pasar fechas/horas en ISO 8601 (se le inyecta la fecha/hora actual en el
  system prompt); para consultas vale un `time_range` natural ("hoy", "mañana", "semana").
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta

from src.integrations.google.auth import GoogleAuthError, build_service

logger = logging.getLogger(__name__)

_MAX_RESULTS_CAP = 25     # nunca pedir más de esto al API en un listado
_DEFAULT_RESULTS = 10     # cuántos traer si el modelo no especifica
_DEFAULT_DURATION_MIN = 60  # duración por defecto de un evento si no se da fin

# Caché del último listado: permite resolver "borra el 2" / "mueve el de la reunión".
_last_ids: list[str] = []
_last_rows: list[dict] = []   # [{"id", "summary", "start"}], mismo orden que _last_ids

_DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def _norm(s: str) -> str:
    """Minúsculas sin acentos, para casar referencias por voz ('víctor' ~ 'victor')."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


# --- Acción pendiente de confirmación (borrar/mover) -------------------------------
# Mismo patrón que gmail.py: la acción ya resuelta (con id real) espera aquí; Luka pide
# confirmación y AssistantService llama a run_pending()/clear_pending() según el sí/no.
_pending: dict | None = None
_pending_ts: float = 0.0
_PENDING_TTL = 120.0


def stage_pending(action: dict, summary: str) -> str:
    """Registra una acción a confirmar y devuelve el texto de confirmación para hablar."""
    global _pending, _pending_ts
    _pending = action
    _pending_ts = time.monotonic()
    return summary


def peek_pending() -> dict | None:
    """Devuelve la acción pendiente si sigue fresca; si caducó, la descarta y devuelve None."""
    global _pending
    if _pending is None:
        return None
    if time.monotonic() - _pending_ts > _PENDING_TTL:
        _pending = None
        return None
    return _pending


def clear_pending() -> None:
    global _pending
    _pending = None


def _service():
    return build_service("calendar", "v3")


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _tz_name() -> str:
    """Nombre IANA de la zona local (p.ej. 'Europe/Madrid'); cae a la abreviatura si falla."""
    try:
        tz = datetime.now().astimezone().tzinfo
        return getattr(tz, "key", None) or tz.tzname(None) or "UTC"
    except Exception:  # noqa: BLE001
        return "UTC"


def _parse_dt(s: str):
    """Interpreta una fecha/hora del modelo. Devuelve ('date', 'YYYY-MM-DD') para todo el
    día, ('dateTime', datetime con tz) para una hora concreta, o None si no se entiende."""
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return ("date", s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    return ("dateTime", dt)


def _event_when(ev: dict) -> tuple[str, bool]:
    """(texto legible del inicio, es_todo_el_día) para un evento del API."""
    start = ev.get("start", {})
    if "date" in start:  # evento de todo el día
        try:
            d = datetime.strptime(start["date"], "%Y-%m-%d")
            return f"{_DIAS[d.weekday()]} {d.day:02d}/{d.month:02d} (todo el día)", True
        except ValueError:
            return f"{start['date']} (todo el día)", True
    iso = start.get("dateTime", "")
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_local_tz())
        return f"{_DIAS[dt.weekday()]} {dt.day:02d}/{dt.month:02d} {dt.hour:02d}:{dt.minute:02d}", False
    except ValueError:
        return iso, False


def _start_key(ev: dict) -> str:
    s = ev.get("start", {})
    return s.get("dateTime") or s.get("date") or ""


def _window(time_range: str, start: str, end: str) -> tuple[str, str, str]:
    """Calcula (timeMin, timeMax, etiqueta) en RFC3339 según un rango natural o ISO."""
    tz = _local_tz()
    now = datetime.now(tz)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tr = _norm(time_range)

    # Rango explícito por ISO (start/end) tiene prioridad.
    ps, pe = _parse_dt(start), _parse_dt(end)
    if ps:
        lo = ps[1] if ps[0] == "dateTime" else datetime.strptime(ps[1], "%Y-%m-%d").replace(tzinfo=tz)
        if pe:
            hi = pe[1] if pe[0] == "dateTime" else datetime.strptime(pe[1], "%Y-%m-%d").replace(tzinfo=tz)
        else:
            hi = lo.replace(hour=0, minute=0) + timedelta(days=1)
        return lo.isoformat(), hi.isoformat(), ""

    if tr in ("hoy", "today"):
        return day0.isoformat(), (day0 + timedelta(days=1)).isoformat(), "hoy"
    if tr in ("manana", "tomorrow"):
        d = day0 + timedelta(days=1)
        return d.isoformat(), (d + timedelta(days=1)).isoformat(), "mañana"
    if tr in ("semana", "week", "esta semana", "this week"):
        return day0.isoformat(), (day0 + timedelta(days=7)).isoformat(), "esta semana"
    if tr in ("mes", "month"):
        return day0.isoformat(), (day0 + timedelta(days=31)).isoformat(), "este mes"
    # Por defecto: desde ahora y los próximos 7 días.
    return now.isoformat(), (day0 + timedelta(days=8)).isoformat(), "los próximos días"


def _list_events(action: str, query: str, time_range: str, start: str, end: str,
                 max_results: int) -> str:
    svc = _service()
    n = max(1, min(int(max_results or _DEFAULT_RESULTS), _MAX_RESULTS_CAP))
    time_min, time_max, label = _window(time_range, start, end)
    params = {
        "calendarId": "primary", "maxResults": n, "singleEvents": True,
        "orderBy": "startTime", "timeMin": time_min, "timeMax": time_max,
    }
    if action == "search" and query:
        params["q"] = query
    resp = svc.events().list(**params).execute()
    items = resp.get("items", [])

    global _last_ids, _last_rows
    _last_ids, _last_rows = [], []
    if not items:
        if action == "search":
            return (f"No encontré eventos para «{query}». Prueba otro término o un rango "
                    "distinto (action='agenda' con time_range='semana').")
        when = f" para {label}" if label else ""
        return f"No tienes eventos{when}."

    lines = []
    for i, ev in enumerate(items, start=1):
        summary = ev.get("summary", "(sin título)")
        when, _allday = _event_when(ev)
        loc = ev.get("location", "")
        _last_ids.append(ev["id"])
        _last_rows.append({"id": ev["id"], "summary": summary, "start": _start_key(ev)})
        lines.append(f"{i}. {when} — {summary}" + (f" ({loc})" if loc else ""))
    head = (f"{len(items)} eventos" + (f" para «{query}»" if action == "search" and query
            else (f" {label}" if label else ""))) + ":"
    return (head + "\n" + "\n".join(lines)
            + "\n(Para borrar/mover uno: usa el NÚMERO mostrado, p.ej. event_id='2'.)")


def _resolve_ref(ref: str) -> tuple[str | None, str]:
    """Resuelve una referencia de usuario a un event_id real (número del último listado,
    id exacto, o coincidencia por título). Devuelve (id, "") o (None, mensaje_accionable)."""
    ref = str(ref or "").strip()
    if not ref:
        return None, ("Indica a qué evento te refieres: un número del último listado o una "
                      "palabra de su título. Si no has listado aún, usa action='agenda'.")
    if ref.isdigit():
        if not _last_ids:
            return None, "Aún no he listado eventos. Usa action='agenda' o 'search' primero."
        idx = int(ref) - 1
        if not (0 <= idx < len(_last_ids)):
            return None, (f"Solo hay {len(_last_ids)} eventos en el último listado "
                          f"(del 1 al {len(_last_ids)}). Di un número de ese rango o vuelve a consultar.")
        return _last_ids[idx], ""
    for row in _last_rows:                       # id exacto de una fila listada
        if row["id"] == ref:
            return ref, ""
    nref = _norm(ref)
    if nref:
        for row in _last_rows:                   # coincidencia por título
            if nref in _norm(row["summary"]):
                return row["id"], ""
    return None, (f"No encuentro «{ref}» entre los eventos listados. "
                  "Di el número que aparece en la lista, o consulta de nuevo.")


def _create_event(summary: str, start: str, end: str, duration_minutes: int,
                  attendees: str, location: str, description: str) -> str:
    if not summary:
        return "Necesito un título para el evento (p.ej. «Dentista»)."
    ps = _parse_dt(start)
    if not ps:
        return ("No entiendo la fecha/hora de inicio. Pásamela en ISO 8601, "
                "p.ej. start='2026-06-12T17:00' (o '2026-06-12' para todo el día).")
    body: dict = {"summary": summary}
    if location:
        body["location"] = location
    if description:
        body["description"] = description

    if ps[0] == "date":
        body["start"] = {"date": ps[1]}
        pe = _parse_dt(end)
        # En todo-el-día, end.date es EXCLUSIVO: por defecto, el día siguiente.
        if pe and pe[0] == "date":
            body["end"] = {"date": pe[1]}
        else:
            d = datetime.strptime(ps[1], "%Y-%m-%d") + timedelta(days=1)
            body["end"] = {"date": d.strftime("%Y-%m-%d")}
    else:
        body["start"] = {"dateTime": ps[1].isoformat(), "timeZone": _tz_name()}
        pe = _parse_dt(end)
        if pe and pe[0] == "dateTime":
            end_dt = pe[1]
        else:
            mins = int(duration_minutes or _DEFAULT_DURATION_MIN)
            end_dt = ps[1] + timedelta(minutes=mins)
        body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": _tz_name()}

    emails = [e.strip() for e in re.split(r"[,\s]+", attendees or "") if "@" in e]
    if emails:
        body["attendees"] = [{"email": e} for e in emails]

    svc = _service()
    params = {"calendarId": "primary", "body": body}
    if emails:
        params["sendUpdates"] = "all"   # avisar por correo a los invitados
    ev = svc.events().insert(**params).execute()
    when, _ = _event_when(ev)
    extra = f", con {len(emails)} invitado(s)" if emails else ""
    return f"Evento creado: «{summary}» el {when}{extra}."


# ---- staging (preparación; NO modifica nada hasta confirmar) ----

def _stage_delete(event_id: str) -> tuple[dict | None, str]:
    mid, err = _resolve_ref(event_id)
    if err:
        return None, err
    svc = _service()
    ev = svc.events().get(calendarId="primary", eventId=mid).execute()
    summary = ev.get("summary", "(sin título)")
    when, _ = _event_when(ev)
    return ({"kind": "delete", "id": mid}, f"¿Quieres que borre el evento «{summary}» del {when}?")


def _stage_update(event_id: str, summary: str, start: str, end: str,
                  duration_minutes: int, location: str, description: str) -> tuple[dict | None, str]:
    mid, err = _resolve_ref(event_id)
    if err:
        return None, err
    patch: dict = {}
    changes: list[str] = []
    if summary:
        patch["summary"] = summary
        changes.append(f"título «{summary}»")
    if location:
        patch["location"] = location
        changes.append(f"lugar «{location}»")
    if description:
        patch["description"] = description
        changes.append("descripción")
    ps = _parse_dt(start)
    if ps:
        if ps[0] == "date":
            patch["start"] = {"date": ps[1]}
            d = datetime.strptime(ps[1], "%Y-%m-%d") + timedelta(days=1)
            patch["end"] = {"date": d.strftime("%Y-%m-%d")}
            changes.append(f"fecha {ps[1]}")
        else:
            patch["start"] = {"dateTime": ps[1].isoformat(), "timeZone": _tz_name()}
            pe = _parse_dt(end)
            end_dt = pe[1] if (pe and pe[0] == "dateTime") else \
                ps[1] + timedelta(minutes=int(duration_minutes or _DEFAULT_DURATION_MIN))
            patch["end"] = {"dateTime": end_dt.isoformat(), "timeZone": _tz_name()}
            changes.append(f"hora {ps[1].strftime('%d/%m %H:%M')}")
    if not patch:
        return None, ("Dime qué cambiar del evento: nueva hora (start), título (summary) o "
                      "lugar (location).")
    svc = _service()
    ev = svc.events().get(calendarId="primary", eventId=mid, fields="summary").execute()
    name = ev.get("summary", "(sin título)")
    return ({"kind": "update", "id": mid, "patch": patch},
            f"¿Cambio en «{name}»: {', '.join(changes)}? ¿Lo aplico?")


# ---- ejecución de la acción pendiente (sí modifica) ----

def _execute(action: dict) -> str:
    svc = _service()
    kind = action.get("kind")
    if kind == "delete":
        svc.events().delete(calendarId="primary", eventId=action["id"], sendUpdates="all").execute()
        return "Evento borrado."
    if kind == "update":
        ev = svc.events().patch(
            calendarId="primary", eventId=action["id"], body=action["patch"],
            sendUpdates="all").execute()
        when, _ = _event_when(ev)
        return f"Evento actualizado: «{ev.get('summary', '(sin título)')}» el {when}."
    return "No había nada que confirmar."


async def run_pending() -> str:
    """Ejecuta la acción de calendario confirmada por el usuario y la limpia."""
    action = peek_pending()
    clear_pending()
    if not action:
        return "No hay ninguna acción de calendario pendiente."
    try:
        return await asyncio.to_thread(_execute, action)
    except GoogleAuthError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error ejecutando acción de calendario {action.get('kind')}: {e}", exc_info=True)
        return f"No pude completar la acción de calendario: {e}"


async def calendar_manager(action: str, query: str = "", event_id: str = "", summary: str = "",
                           start: str = "", end: str = "", duration_minutes: int = 60,
                           attendees: str = "", location: str = "", description: str = "",
                           time_range: str = "", max_results: int = 10) -> str:
    """Gestiona el Google Calendar del usuario: consultar la agenda, crear, mover y borrar eventos.

    action="agenda": muestra los próximos eventos. Acota con time_range="hoy"|"mañana"|"semana"
        o con un rango ISO en start/end.
    action="search": busca eventos cuyo texto contenga `query` (acota igual con time_range).
    action="create": crea un evento con `summary` (título) y `start` en ISO 8601
        (p.ej. "2026-06-12T17:00"; o "2026-06-12" para todo el día). `end` o `duration_minutes`
        fijan el fin; `attendees` son correos separados por comas; `location`/`description` opcionales.
        Se ejecuta directo (no pide confirmación).
    action="delete": borra el evento `event_id` (un número del último listado, p.ej. "2"). Pide confirmación por voz.
    action="update": mueve/edita el evento `event_id`: nueva hora en `start`, o nuevo `summary`/`location`. Pide confirmación por voz.
    """
    act = str(action or "").strip().lower()
    # El modelo a veces pasa números/horas como enteros: normalizar a str evita AttributeError.
    query = str(query or "")
    event_id = str(event_id) if event_id != "" else ""
    summary, start, end = str(summary or ""), str(start or ""), str(end or "")
    attendees, location, description = str(attendees or ""), str(location or ""), str(description or "")
    time_range = str(time_range or "")
    try:
        if act in ("agenda", "list", "today", "hoy", "upcoming", "proximos", "próximos", ""):
            tr = time_range or (act if act in ("today", "hoy") else "")
            return await asyncio.to_thread(_list_events, "agenda", query, tr, start, end, max_results)
        if act in ("search", "buscar", "find"):
            return await asyncio.to_thread(_list_events, "search", query, time_range, start, end, max_results)
        if act in ("create", "crear", "add", "nuevo", "agendar"):
            return await asyncio.to_thread(_create_event, summary or query, start, end,
                                           duration_minutes, attendees, location, description)
        if act in ("delete", "borrar", "eliminar", "cancelar", "cancel"):
            staged, msg = await asyncio.to_thread(_stage_delete, event_id or query)
            return stage_pending(staged, msg) if staged else msg
        if act in ("update", "mover", "editar", "modificar", "reprogramar", "move", "edit"):
            staged, msg = await asyncio.to_thread(_stage_update, event_id or query, summary,
                                                  start, end, duration_minutes, location, description)
            return stage_pending(staged, msg) if staged else msg
        return (f"Acción de calendario no reconocida: '{action}'. "
                "Usa 'agenda', 'search', 'create', 'delete' o 'update'.")
    except GoogleAuthError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 — errores de red/API: informar sin romper el turno
        logger.error(f"Error en calendar_manager({action}): {e}", exc_info=True)
        return f"No pude completar la operación de calendario: {e}"
