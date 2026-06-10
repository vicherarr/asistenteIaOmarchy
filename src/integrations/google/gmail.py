"""Tool de Gmail para Luka: buscar, listar y leer correos (Fase 1, solo lectura).

El envío/borrador/papelera (con confirmación) llegan en la Fase 2.

Diseño:
- Una sola tool multiplexora `gmail_manager(action, ...)` (patrón como clipboard_manager).
- `googleapiclient` es síncrono → cada llamada se ejecuta en un hilo (asyncio.to_thread)
  para no bloquear el event loop.
- Los listados numeran los correos (1..N) y guardan el mapeo nº→id en un caché de proceso,
  para que el usuario pueda decir "lee el 2" por voz sin manejar ids largos.
- Salida pensada para que el modelo RESUMA: cuerpos truncados, sin leer cabeceras técnicas.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
import unicodedata
from email.message import EmailMessage
from email.utils import parseaddr

from src.integrations.google.auth import GoogleAuthError, build_service

logger = logging.getLogger(__name__)

_MAX_BODY_CHARS = 1500   # tope de cuerpo que se devuelve al modelo (él lo resume para el TTS)
_MAX_RESULTS_CAP = 20    # nunca pedir más de esto al API en un listado
_DEFAULT_RESULTS = 8     # cuántos traer si el modelo no especifica

# Caché del último listado. Permite resolver una referencia ("lee el 2", "el de Víctor",
# "el del curriculum") tras un list/search. `_last_ids` mantiene solo los IDs en orden
# (compat); `_last_rows` añade remitente y asunto para resolver por nombre/asunto.
_last_ids: list[str] = []
_last_rows: list[dict] = []   # [{"id", "sender", "subject"}], mismo orden que _last_ids


def _norm(s: str) -> str:
    """Minúsculas sin acentos, para casar referencias por voz ('víctor' ~ 'victor')."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()

# --- Acción pendiente de confirmación (enviar/responder/papelera/archivar) ---------
# Las acciones IRREVERSIBLES no se ejecutan al instante: la tool deja aquí una acción
# ya resuelta (con destinatario/ids reales) y Luka pide confirmación por voz. En el
# turno siguiente, AssistantService interpreta el "sí/no" y llama a run_pending() o
# clear_pending(). Caduca a los _PENDING_TTL s para no ejecutar algo "zombi".
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
    return build_service("gmail", "v1")


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _sender_short(raw: str) -> str:
    """'Nombre Apellido <x@y.com>' -> 'Nombre Apellido' (o el email si no hay nombre)."""
    name, addr = parseaddr(raw)
    return name or addr or raw


def _decode_body(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_plain_text(payload: dict) -> str:
    """Recorre el árbol MIME y devuelve el primer texto legible (prefiere text/plain)."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return _decode_body(body["data"])
    # multipart u otros: buscar recursivamente
    plain, html = "", ""
    for part in payload.get("parts", []) or []:
        txt = _extract_plain_text(part)
        if not txt:
            continue
        if part.get("mimeType") == "text/plain":
            plain = plain or txt
        elif part.get("mimeType") == "text/html":
            html = html or txt
    if plain:
        return plain
    if html:  # último recurso: quitar etiquetas a lo bruto
        import re
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _list_messages(action: str, query: str, max_results: int) -> str:
    svc = _service()
    n = max(1, min(int(max_results or _DEFAULT_RESULTS), _MAX_RESULTS_CAP))
    params = {"userId": "me", "maxResults": n}
    if action == "search":
        params["q"] = query or ""
    else:  # list: bandeja de entrada reciente
        params["labelIds"] = ["INBOX"]
    resp = svc.users().messages().list(**params).execute()
    ids = [m["id"] for m in resp.get("messages", [])]

    global _last_ids, _last_rows
    _last_ids = ids
    _last_rows = []
    if not ids:
        if action == "search":
            return (f"No encontré correos para «{query}». Prueba con términos más amplios o "
                    "distintos (un nombre de remitente, una palabra del asunto, o sin filtros).")
        return "La bandeja de entrada está vacía."

    lines = []
    for i, mid in enumerate(ids, start=1):
        msg = svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        p = msg.get("payload", {})
        unread = "UNREAD" in msg.get("labelIds", [])
        mark = "● " if unread else ""  # ● = no leído
        sender = _sender_short(_header(p, "From"))
        subject = _header(p, "Subject") or "(sin asunto)"
        snippet = (msg.get("snippet", "") or "").strip()
        _last_rows.append({"id": mid, "sender": sender, "subject": subject})
        lines.append(f"{i}. {mark}{sender} — {subject}\n   {snippet[:140]}")
    header = f"{len(ids)} correos" + (f" para «{query}»" if action == "search" else " recientes") + ":"
    return (header + "\n" + "\n".join(lines)
            + "\n(Para abrir uno: action='read' con el NÚMERO mostrado, p.ej. message_id='2'.)")


def _resolve_ref(ref: str) -> tuple[str | None, str]:
    """Resuelve una referencia de usuario a un message_id real.

    Acepta, por orden: número del último listado ("2"), el id real de Gmail, el id
    exacto de una fila listada, o una coincidencia por remitente/asunto ("el de Víctor",
    "el del curriculum"). Devuelve (id, "") si resuelve, o (None, mensaje_accionable)
    para que el modelo sepa exactamente qué hacer (no un error técnico que reintente).
    """
    ref = str(ref or "").strip()
    if not ref:
        return None, ("Indica a qué correo te refieres: un número del último listado, "
                      "el remitente o una palabra del asunto.")
    # 1) Número del último listado.
    if ref.isdigit():
        if not _last_ids:
            return None, "Aún no he listado correos. Usa action='list' o 'search' primero."
        idx = int(ref) - 1
        if not (0 <= idx < len(_last_ids)):
            return None, (f"Solo hay {len(_last_ids)} correos en el último listado "
                          f"(del 1 al {len(_last_ids)}). Di un número de ese rango o vuelve a buscar.")
        return _last_ids[idx], ""
    # 2) Id exacto de una fila ya listada.
    for row in _last_rows:
        if row["id"] == ref:
            return ref, ""
    # 3) Coincidencia por remitente o asunto del último listado (sin acentos, parcial).
    nref = _norm(ref)
    if nref:
        for row in _last_rows:                       # prioridad: remitente
            if nref in _norm(row["sender"]):
                return row["id"], ""
        for row in _last_rows:                       # luego: asunto
            if nref in _norm(row["subject"]):
                return row["id"], ""
    # 4) Parece un id real de Gmail (hex largo) aunque no esté en el listado.
    if len(ref) >= 12 and all(c in "0123456789abcdef" for c in ref.lower()):
        return ref, ""
    return None, (f"No encuentro «{ref}» entre los correos listados. "
                  "Di el número que aparece en la lista, o busca de nuevo.")


def _read_message(message_id: str) -> str:
    svc = _service()
    mid, err = _resolve_ref(message_id)
    if err:
        return err

    msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
    p = msg.get("payload", {})
    sender = _header(p, "From")
    subject = _header(p, "Subject") or "(sin asunto)"
    date = _header(p, "Date")
    body = _extract_plain_text(p).strip()
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "\n[…correo truncado…]"
    return (f"De: {sender}\nAsunto: {subject}\nFecha: {date}\n\n{body}"
            if body else
            f"De: {sender}\nAsunto: {subject}\nFecha: {date}\n\n(Sin cuerpo de texto legible.)")


def _raw(to: str, subject: str, body: str, in_reply_to: str = "", references: str = "") -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body or "")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


# ---- staging (solo lectura/preparación; NO modifican nada) ----

def _stage_send(to: str, subject: str, body: str) -> tuple[dict | None, str]:
    if not to or "@" not in to:
        return None, "Necesito una dirección de correo válida (con @) para enviar."
    subject = subject or "(sin asunto)"
    action = {"kind": "send", "to": to, "subject": subject, "body": body or ""}
    return action, f"He preparado un correo para {to}, asunto «{subject}». ¿Lo envío?"


def _stage_reply(message_id: str, body: str) -> tuple[dict | None, str]:
    mid, err = _resolve_ref(message_id)
    if err:
        return None, err
    svc = _service()
    orig = svc.users().messages().get(
        userId="me", id=mid, format="metadata",
        metadataHeaders=["From", "Subject", "Message-ID", "References"],
    ).execute()
    p = orig.get("payload", {})
    to = _header(p, "From")
    subject = _header(p, "Subject") or "(sin asunto)"
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    msgid = _header(p, "Message-ID")
    refs = (_header(p, "References") + " " + msgid).strip() if _header(p, "References") else msgid
    action = {"kind": "send", "to": to, "subject": subject, "body": body or "",
              "thread_id": orig.get("threadId"), "in_reply_to": msgid, "references": refs}
    return action, f"He preparado la respuesta a {_sender_short(to)}, asunto «{subject}». ¿La envío?"


def _stage_label(kind: str, message_id: str) -> tuple[dict | None, str]:
    mid, err = _resolve_ref(message_id)
    if err:
        return None, err
    verb = "mover a la papelera" if kind == "trash" else "archivar"
    svc = _service()
    meta = svc.users().messages().get(
        userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject"],
    ).execute()
    p = meta.get("payload", {})
    desc = f"«{_header(p, 'Subject') or '(sin asunto)'}» de {_sender_short(_header(p, 'From'))}"
    return {"kind": kind, "id": mid}, f"¿Quieres que vaya a {verb} el correo {desc}?"


def _do_draft(to: str, subject: str, body: str) -> str:
    if not to or "@" not in to:
        return "Necesito una dirección de correo válida (con @) para el borrador."
    svc = _service()
    raw = _raw(to, subject or "(sin asunto)", body)
    svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return f"Borrador guardado para {to}."


# ---- ejecución de la acción pendiente (sí modifica) ----

def _execute(action: dict) -> str:
    svc = _service()
    kind = action.get("kind")
    if kind == "send":
        raw = _raw(action["to"], action["subject"], action.get("body", ""),
                   action.get("in_reply_to", ""), action.get("references", ""))
        body = {"raw": raw}
        if action.get("thread_id"):
            body["threadId"] = action["thread_id"]
        svc.users().messages().send(userId="me", body=body).execute()
        return f"Correo enviado a {_sender_short(action['to'])}."
    if kind == "trash":
        svc.users().messages().trash(userId="me", id=action["id"]).execute()
        return "Correo movido a la papelera."
    if kind == "archive":
        svc.users().messages().modify(
            userId="me", id=action["id"], body={"removeLabelIds": ["INBOX"]}).execute()
        return "Correo archivado."
    return "No había nada que confirmar."


async def run_pending() -> str:
    """Ejecuta la acción pendiente confirmada por el usuario y la limpia."""
    action = peek_pending()
    clear_pending()
    if not action:
        return "No hay ninguna acción de correo pendiente."
    try:
        return await asyncio.to_thread(_execute, action)
    except GoogleAuthError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error ejecutando acción de correo {action.get('kind')}: {e}", exc_info=True)
        return f"No pude completar la acción de correo: {e}"


async def gmail_manager(action: str, query: str = "", message_id: str = "", max_results: int = 5,
                        to: str = "", subject: str = "", body: str = "") -> str:
    """Gestiona el correo de Gmail del usuario: listar, buscar y leer mensajes.

    action="list": muestra los correos recientes de la bandeja de entrada.
    action="search": busca con `query` (sintaxis de Gmail, p.ej. 'from:ana is:unread').
    action="read": lee un correo indicado en `message_id` (un número del último listado, p.ej. "2").
    action="send": envía un correo a `to` con `subject` y `body`. Pide confirmación por voz.
    action="reply": responde al correo `message_id` con `body`. Pide confirmación por voz.
    action="draft": guarda un borrador a `to` con `subject` y `body` (no envía nada).
    action="trash": mueve a la papelera el correo `message_id`. Pide confirmación por voz.
    action="archive": archiva (saca de la bandeja) el correo `message_id`. Pide confirmación por voz.
    """
    act = str(action or "").strip().lower()
    # El modelo a veces pasa el nº de correo como entero (message_id=4) en vez de "4";
    # normalizar a str evita que helpers que hacen .strip() revienten con AttributeError.
    query = str(query or "")
    message_id = str(message_id) if message_id != "" else ""
    to, subject, body = str(to or ""), str(subject or ""), str(body or "")
    try:
        if act in ("list", "inbox", "recientes", ""):
            return await asyncio.to_thread(_list_messages, "list", query, max_results)
        if act in ("search", "buscar", "find"):
            return await asyncio.to_thread(_list_messages, "search", query, max_results)
        if act in ("read", "leer", "open", "abrir"):
            return await asyncio.to_thread(_read_message, message_id or query)
        if act in ("send", "enviar", "mandar"):
            staged, msg = await asyncio.to_thread(_stage_send, to or query, subject, body)
            return stage_pending(staged, msg) if staged else msg
        if act in ("reply", "responder", "contestar"):
            staged, msg = await asyncio.to_thread(_stage_reply, message_id or query, body)
            return stage_pending(staged, msg) if staged else msg
        if act in ("draft", "borrador"):
            return await asyncio.to_thread(_do_draft, to or query, subject, body)
        if act in ("trash", "papelera", "borrar", "eliminar"):
            staged, msg = await asyncio.to_thread(_stage_label, "trash", message_id or query)
            return stage_pending(staged, msg) if staged else msg
        if act in ("archive", "archivar"):
            staged, msg = await asyncio.to_thread(_stage_label, "archive", message_id or query)
            return stage_pending(staged, msg) if staged else msg
        return (f"Acción de correo no reconocida: '{action}'. "
                "Usa 'list', 'search', 'read', 'send', 'reply', 'draft', 'trash' o 'archive'.")
    except GoogleAuthError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 — errores de red/API: informar sin romper el turno
        logger.error(f"Error en gmail_manager({action}): {e}", exc_info=True)
        return f"No pude completar la operación de correo: {e}"
