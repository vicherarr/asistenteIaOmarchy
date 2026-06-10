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
from email.utils import parseaddr

from src.integrations.google.auth import GoogleAuthError, build_service

logger = logging.getLogger(__name__)

_MAX_BODY_CHARS = 1500   # tope de cuerpo que se devuelve al modelo (él lo resume para el TTS)
_MAX_RESULTS_CAP = 15    # nunca pedir más de esto al API en un listado

# Caché del último listado: nº (1..N) -> message_id. Permite "lee el 2" tras un "list/search".
_last_ids: list[str] = []


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
    n = max(1, min(int(max_results or 5), _MAX_RESULTS_CAP))
    params = {"userId": "me", "maxResults": n}
    if action == "search":
        params["q"] = query or ""
    else:  # list: bandeja de entrada reciente
        params["labelIds"] = ["INBOX"]
    resp = svc.users().messages().list(**params).execute()
    ids = [m["id"] for m in resp.get("messages", [])]

    global _last_ids
    _last_ids = ids
    if not ids:
        return "No hay correos que coincidan." if action == "search" else "La bandeja de entrada está vacía."

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
        lines.append(f"{i}. {mark}{sender} — {subject}\n   {snippet[:140]}")
    header = f"{len(ids)} correos" + (f" para «{query}»" if action == "search" else " recientes") + ":"
    return header + "\n" + "\n".join(lines) + "\n(Di 'lee el N' para abrir uno.)"


def _read_message(message_id: str) -> str:
    svc = _service()
    mid = message_id.strip()
    # Permitir referirse por número del último listado ("lee el 2").
    if mid.isdigit():
        idx = int(mid) - 1
        if not (0 <= idx < len(_last_ids)):
            return f"No tengo un correo número {mid}. Lista o busca correos primero."
        mid = _last_ids[idx]
    if not mid:
        return "Indica qué correo leer (un número del último listado o un id)."

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


async def gmail_manager(action: str, query: str = "", message_id: str = "", max_results: int = 5) -> str:
    """Gestiona el correo de Gmail del usuario: listar, buscar y leer mensajes.

    action="list": muestra los correos recientes de la bandeja de entrada.
    action="search": busca con `query` (sintaxis de Gmail, p.ej. 'from:ana is:unread').
    action="read": lee un correo indicado en `message_id` (un número del último listado, p.ej. "2").
    """
    act = (action or "").strip().lower()
    try:
        if act in ("list", "inbox", "recientes", ""):
            return await asyncio.to_thread(_list_messages, "list", query, max_results)
        if act in ("search", "buscar", "find"):
            return await asyncio.to_thread(_list_messages, "search", query, max_results)
        if act in ("read", "leer", "open", "abrir"):
            return await asyncio.to_thread(_read_message, message_id or query)
        return (f"Acción de correo no reconocida: '{action}'. "
                "Usa 'list', 'search' (con query) o 'read' (con message_id).")
    except GoogleAuthError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 — errores de red/API: informar sin romper el turno
        logger.error(f"Error en gmail_manager({action}): {e}", exc_info=True)
        return f"No pude completar la operación de correo: {e}"
