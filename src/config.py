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
    
    # LiteRT
    LITERT_MODEL_PATH: str = "models/gemma-4-E4B-it.litertlm"
    LITERT_BACKEND: str = "cpu"

    # Assistant
    MAX_HISTORY: int = 10
    
    # TTS
    KOKORO_VOICE: str = "em_alex"
    KOKORO_LANG: str = "e"
    
    # Wake Word (Sherpa-ONNX KWS)
    WAKE_WORD_ENABLED: bool = True
    WAKE_WORD_KEYWORDS_FILE: str = "models/sherpa-kws/keywords.txt"
    WAKE_WORD_MODEL_DIR: str = "models/sherpa-kws"
    WAKE_WORD_THRESHOLD: float = 0.30
    
    # Paths
    PROJECT_ROOT: Path = Path(__file__).parent.parent
    TEMP_DIR: Path = Path(os.getenv("TMPDIR", "/tmp")) / "asistenteia"
    OBSIDIAN_VAULT: Path = Path(os.path.expanduser("~/Documentos/Obsidian Vault"))
    OBSIDIAN_CLIPPINGS: Path = Path(os.path.expanduser("~/Documentos/Obsidian Vault/Clippings"))

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
