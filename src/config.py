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
    
    # Seguridad y Cifrado
    API_TOKEN: Optional[str] = None
    SSL_KEYFILE: Optional[str] = None
    SSL_CERTFILE: Optional[str] = None
    
    # LiteRT (motor único: LLM en GPU, visión y audio en CPU)
    LITERT_MODEL_PATH: str = "models/gemma-4-E4B-it.litertlm"
    LITERT_BACKEND: str = "gpu"
    LITERT_TIMEOUT: float = 35.0

    # Assistant
    MAX_HISTORY: int = 10
    
    # TTS
    KOKORO_VOICE: str = "em_alex"
    KOKORO_LANG: str = "e"

    # STT (faster-whisper / CTranslate2 en CPU)
    STT_MODEL: str = "large-v3-turbo"
    STT_DEVICE: str = "cpu"
    STT_COMPUTE_TYPE: str = "int8"
    STT_LANGUAGE: str = "es"
    STT_THREADS: int = 8
    STT_VAD: bool = True
    STT_PROMPT: str = (
        "Comandos de voz en español para el asistente Luka: música, volumen, "
        "captura de pantalla, documentos, búsqueda en internet y notas de Obsidian."
    )
    
    # Wake Word (Sherpa-ONNX KWS)
    WAKE_WORD_ENABLED: bool = True
    WAKE_WORD_KEYWORDS_FILE: str = "models/sherpa-kws/keywords.txt"
    WAKE_WORD_MODEL_DIR: str = "models/sherpa-kws"
    WAKE_WORD_THRESHOLD: float = 0.10
    
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
