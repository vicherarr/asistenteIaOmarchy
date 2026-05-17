"""Esquemas de datos compartidos para el asistente."""

from pydantic import BaseModel, Field

class ChatMessage(BaseModel):
    """Representación genérica de un mensaje en la conversación."""
    role: str  # "user", "assistant", "system"
    content: str
    images: list[str] = Field(default_factory=list)  # Base64 o rutas
