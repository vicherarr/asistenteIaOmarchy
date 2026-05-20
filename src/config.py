"""Configuración centralizada de la aplicación usando Pydantic Settings."""

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuraciones globales cargadas desde variables de entorno o archivo .env."""
    
    # API Server
    HOST: str = "127.0.0.1"
    PORT: int = 8765
    DEBUG: bool = False
    
    # Ollama
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "ministral-3:3b"
    OLLAMA_TIMEOUT: float = 120.0
    OLLAMA_TEMPERATURE: float = 0.3
    OLLAMA_NUM_CTX: int = 4096

    # LiteRT
    LITERT_MODEL_PATH: str = "models/gemma-4-e4b.litertlm"

    # Assistant
    MAX_HISTORY: int = 10
    
    # TTS
    KOKORO_VOICE: str = "em_alex"
    KOKORO_LANG: str = "e"
    
    # Paths
    PROJECT_ROOT: Path = Path(__file__).parent.parent
    TEMP_DIR: Path = Path(os.getenv("TMPDIR", "/tmp")) / "asistenteia"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )


# Instancia global de configuración
settings = Settings()

# Asegurar que los directorios existen
settings.TEMP_DIR.mkdir(parents=True, exist_ok=True)
(settings.PROJECT_ROOT / "models").mkdir(parents=True, exist_ok=True)
