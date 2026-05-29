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
    # Fase 1 — exprimir el motor:
    # Multi-Token Prediction (MTP / speculative decoding): hasta 2.2x decode en GPU
    # y recomendado para Gemma E4B. Desactívalo (False) solo si en CPU notas penalti.
    LITERT_SPECULATIVE_DECODING: bool = True
    # Directorio de caché de artefactos compilados (acelera el arranque en frío).
    # Vacío => se usa ~/.cache/asistenteia/litert
    LITERT_CACHE_DIR: str = ""
    # Si True, se pasa el system prompt como input_prompt_as_hint para mejorar el TTFT
    # del primer turno (prefijo estable conocido por el motor).
    LITERT_PROMPT_HINT: bool = True

    # --- Fase 2: muestreo (SamplerConfig) ---
    # IMPORTANTE: el .litertlm de Gemma viene afinado en GREEDY (top_k=1), que es lo
    # más fiable para tool-calling y para la fidelidad de visión. Imponer un sampler
    # con top_k alto degrada las llamadas a herramientas. Por eso, por defecto NO se
    # impone sampler (se usa el del motor). Actívalo solo para experimentar con
    # respuestas de texto más variadas; puede volver menos fiables los tool-calls.
    LITERT_SAMPLER_ENABLED: bool = False
    # Perfil "charla": más natural/creativo (solo si LITERT_SAMPLER_ENABLED=True).
    LITERT_TEMPERATURE: float = 0.6
    LITERT_TOP_K: int = 64          # -1 => no fijar (usa default del modelo)
    LITERT_TOP_P: float = 0.95
    LITERT_SEED: int = -1           # -1 => aleatorio; >=0 => reproducible

    # --- Fase 3: contexto y conversación persistente ---
    # Mantener una Conversation viva por sesión para reutilizar la KV-cache
    # (no re-prefillar el historial cada turno). EXPERIMENTAL: por defecto off.
    LITERT_PERSISTENT_CONVERSATION: bool = False

    # --- Fase 4: tools nativas y event handler ---
    # Emite eventos de tool (inicio/fin) al log y permite gating de comandos.
    # EXPERIMENTAL: por defecto off (automatic_tool_calling ya funciona sin él).
    LITERT_TOOL_EVENTS: bool = False

    # Nota Fase 5: la visión en "una sola pasada" para analyze_screen es inviable por
    # reentrancia del motor (no se puede invocar inferencia desde dentro de un tool);
    # el flujo de 2 pasadas es correcto. El single-pass ya funciona cuando la imagen
    # se conoce antes de inferir (image_path en la 1ª llamada).

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
    # Si True, el reconocimiento usa el audio nativo del modelo Gemma (LiteRT)
    # en vez de Whisper. False = Whisper (faster-whisper), el comportamiento actual.
    STT_USE_GEMMA_AUDIO: bool = False
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


def resolve_path(p: Optional[str]) -> Optional[Path]:
    """Resuelve una ruta relativa contra PROJECT_ROOT; deja las absolutas tal cual.

    Garantiza que las rutas configuradas funcionen sin depender del directorio
    de trabajo actual (CWD), independientemente de desde dónde se lance la app.
    """
    if not p:
        return None
    path = Path(p)
    return path if path.is_absolute() else settings.PROJECT_ROOT / path

# Asegurar que los directorios existen
settings.TEMP_DIR.mkdir(parents=True, exist_ok=True)
(settings.PROJECT_ROOT / "models").mkdir(parents=True, exist_ok=True)
