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
    
    # Motor de inferencia: "litert" (por defecto) o "exllama" (Fase 2).
    # Selecciona qué backend de LLM construye la factoría (src/engines/factory.py).
    AI_ENGINE: str = "litert"

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

    # --- Motor ExLlamaV3 vía TabbyAPI (AI_ENGINE="exllama") ---
    # Sidecar OpenAI-compatible. Solo se usa si AI_ENGINE="exllama".
    EXLLAMA_BASE_URL: str = "http://127.0.0.1:5000"
    EXLLAMA_MODEL: str = "Qwen3.5-9B-exl3-4.00bpw"
    EXLLAMA_API_KEY: str = ""            # vacío si TabbyAPI corre con disable_auth
    EXLLAMA_TIMEOUT: float = 120.0
    EXLLAMA_MAX_TOKENS: int = 1024
    EXLLAMA_TEMPERATURE: float = 0.6
    # Razonamiento de Qwen3: CONFIGURABLE pero desactivado por defecto (latencia/tokens
    # en un asistente de voz). True => el modelo "piensa" antes de responder.
    EXLLAMA_THINKING: bool = False
    # True si el modelo es multimodal (p.ej. Qwen3-VL): habilita enviar imágenes.
    EXLLAMA_VISION: bool = False
    # Tope de iteraciones del bucle agéntico (modelo→tool→modelo→…). Margen para que un
    # error recuperable (p.ej. referencia de correo que falla y el modelo corrige listando)
    # no agote el presupuesto y deje al usuario sin respuesta.
    EXLLAMA_MAX_TOOL_ROUNDS: int = 8
    # --- Lifecycle del sidecar (lo gestiona la CLI/shell, no la app Python) ---
    # Si True, "asistenteia start" arranca/para TabbyAPI automáticamente cuando
    # AI_ENGINE="exllama" (igual que LiteRT se carga solo dentro del proceso).
    # Ponlo en False para gestionar TabbyAPI a mano. Solo un motor activo a la vez.
    EXLLAMA_AUTOSTART: bool = True
    # Carpeta de la instalación de TabbyAPI (su propio venv py3.11 + main.py + modelo).
    # Vacío => <raíz del proyecto>/exllama/tabbyAPI. La crea el instalador.
    EXLLAMA_TABBY_DIR: str = ""

    # Assistant
    MAX_HISTORY: int = 10
    # Modo enfocado por defecto: lista de tools (nombres, separados por comas) a las
    # que se restringe el asistente al arrancar. Vacío => todas las tools (normal).
    # Lo fija la CLI según el modelo activo (p.ej. LFM2.5 se centra en terminal/código).
    # La UI/web puede cambiar la selección en caliente; esto solo es la semilla inicial.
    ASSISTANT_DEFAULT_TOOLS: str = ""

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
    
    # --- Integraciones Google (Gmail; Calendar/Drive en el futuro) ---
    # Desactivado por defecto: si no hay credenciales, las tools de Google NI SIQUIERA
    # se registran y el asistente funciona igual que siempre (100% retrocompatible).
    # credentials.json (client secret OAuth de "Desktop app") lo aporta el USUARIO desde
    # su propio proyecto de Google Cloud; token.json lo genera 'asistenteia google-auth'.
    # Ambos viven fuera del repo (~/.config/asistenteia/google) y NUNCA se versionan.
    GOOGLE_ENABLED: bool = True
    GOOGLE_CREDENTIALS_PATH: str = "~/.config/asistenteia/google/credentials.json"
    GOOGLE_TOKEN_PATH: str = "~/.config/asistenteia/google/token.json"

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
