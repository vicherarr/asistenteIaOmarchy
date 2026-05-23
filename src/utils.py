"""Utilidades generales del proyecto."""

import re

# Nota: _pending_image_path ha sido movido a AppState.pending_image_path (main.py).
# Estas funciones se mantienen por compatibilidad con código existente pero están DEPRECATED.
# El loop multimodal (analyze_screen → segunda pasada) aún no está implementado.

_pending_image_path: str | None = None


def strip_markdown(text: str) -> str:
    """Elimina formato markdown del texto para que suene natural en TTS."""
    # Eliminar bloques de código primero (antes de otros patrones)
    text = re.sub(r"`{3}[\s\S]*?`{3}", "", text)
    # Eliminar código inline
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Eliminar bold triple y doble (cerrado o no)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*\*", "", text)  # bold sin cerrar
    # Eliminar italic (cerrado o no)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_", "", text)  # italic sin cerrar
    # Eliminar headings y blockquotes
    text = re.sub(r"^[#\->]+\s*", "", text, flags=re.MULTILINE)
    # Eliminar links pero conservar texto
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Eliminar horizontal rules
    text = re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Normalizar espacios
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def set_pending_image(path: str):
    """DEPRECATED: Usa AppState.pending_image_path en su lugar."""
    global _pending_image_path
    _pending_image_path = path


def get_pending_image() -> str | None:
    """DEPRECATED: Usa AppState.pending_image_path en su lugar."""
    global _pending_image_path
    path = _pending_image_path
    _pending_image_path = None
    return path
