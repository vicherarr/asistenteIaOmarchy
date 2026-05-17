"""Utilidades generales del proyecto."""

import re


def strip_markdown(text: str) -> str:
    """Elimina formato markdown del texto para que suene natural en TTS."""
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = re.sub(r"`{3}[\s\S]*?`{3}", "", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"^[#\->]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()

# --- Estado temporal para comunicación entre Tools y Orquestador ---

_pending_image_path: str | None = None

def set_pending_image(path: str):
    """Guarda una ruta de imagen generada por una tool."""
    global _pending_image_path
    _pending_image_path = path

def get_pending_image() -> str | None:
    """Recupera y limpia la ruta de la imagen pendiente."""
    global _pending_image_path
    path = _pending_image_path
    _pending_image_path = None
    return path
