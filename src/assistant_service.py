"""Servicio principal del asistente, encapsula la lógica de negocio."""

import asyncio
import inspect
import logging
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional, List

from src.schema import ChatMessage
from src.command_executor import (
    execute_system_command,
    read_log_file,
    clipboard_manager,
    web_search,
    system_diagnostics,
    read_web_page,
    play_specific_music,
    play_youtube_music,
    music_control,
    open_terminal_and_run_command,
    read_terminal_screen,
    control_local_browser,
    send_input_to_terminal,
    interrupt_terminal_command,
    launch_application,
    close_application,
    set_current_utterance,
)
from src.vision_tool import analyze_screen, analyze_clipboard_image, take_screenshot, get_pending_vision
from src.camera_tool import analyze_camera, show_camera_photo
from src.document_tool import create_document, get_pending_document, build_and_save_document
from src.litert_client import LiteRTClient
from src.tts_engine import TTSEngine
from src.stt_engine import STTEngine
from src.utils import strip_markdown
from src.config import settings
from PIL import Image

logger = logging.getLogger(__name__)

# Tools que dejan su resultado en la terminal visible (tmux). Solo si una de estas se
# ejecutó tiene sentido leer el panel para construir la respuesta.
_TERMINAL_TOOL_NAMES = frozenset({
    "execute_system_command",
    "open_terminal_and_run_command",
    "read_terminal_screen",
    "send_input_to_terminal",
    "interrupt_terminal_command",
})

# Frase de respaldo cuando el modelo ejecutó una tool pero su texto quedó inservible
# (residuo de una tool call fugada). Se elige por la tool que realmente corrió.
_FALLBACK_BY_TOOL = {
    "play_youtube_music": "Reproduciendo música en YouTube.",
    "play_specific_music": "Reproduciendo música en Spotify.",
    "music_control": "Hecho.",
    "analyze_screen": "Analizando la pantalla.",
    "take_screenshot": "Captura de pantalla hecha.",
    "analyze_clipboard_image": "Analizando la imagen del portapapeles.",
    "analyze_camera": "Mirando por la cámara.",
    # "Búsqueda web realizada" / "Página leída" son confirmaciones vacías: dicen que la
    # tool corrió pero no lo que el usuario quería saber, que es el contenido. Si el
    # modelo no llegó a resumirlo, más vale decirlo y ofrecer repetir.
    "web_search": "He buscado, pero no he sabido resumirte lo que he encontrado. ¿Lo intento otra vez?",
    "read_web_page": "He leído la página, pero no he sabido resumírtela. ¿Lo intento otra vez?",
    "control_local_browser": "Listo en el navegador.",
    "clipboard_manager": "Portapapeles actualizado.",
    "launch_application": "Aplicación lanzada.",
    "close_application": "Aplicación cerrada.",
    "create_document": "Documento creado.",
    "gmail_manager": "Gestión de correo hecha.",
    "calendar_manager": "Agenda actualizada.",
}


class AssistantService:
    """Orquesta la comunicación entre LLM, herramientas y TTS usando LiteRT."""

    def __init__(
        self,
        litert_client: LiteRTClient,
        tts_engine: TTSEngine,
        stt_engine: STTEngine,
    ):
        self.litert = litert_client
        self.tts = tts_engine
        self.stt = stt_engine
        self._current_tts_task: Optional[asyncio.Task] = None
        self._current_play_task: Optional[asyncio.Task] = None
        # Salida de voz: "pc" (altavoces del ordenador, comportamiento de siempre),
        # "device" (altavoz del satélite ESP32) o "both". Mientras no haya ningún
        # dispositivo conectado esto vale "pc" y `audio_sink` es None, así que el
        # camino de audio es exactamente el de antes.
        self.audio_target: str = "pc"
        # Callable async (audio_np, sample_rate) -> None. Lo instala device_gateway.
        self.audio_sink: Optional[Callable] = None
        # Callable async () -> None: aviso de «no viene más audio» para el
        # dispositivo. Lo instala device_gateway junto a `audio_sink`.
        self.audio_sink_end: Optional[Callable] = None
        # Lista de herramientas para LiteRT
        self.tools = [
            execute_system_command,
            read_log_file,
            clipboard_manager,
            web_search,
            system_diagnostics,
            read_web_page,
            play_specific_music,
            play_youtube_music,
            music_control,
            open_terminal_and_run_command,
            read_terminal_screen,
            control_local_browser,
            send_input_to_terminal,
            interrupt_terminal_command,
            launch_application,
            close_application,
            analyze_screen,
            analyze_clipboard_image,
            take_screenshot,
            create_document,
            # Los ojos del satélite. Se registran siempre, no solo con el
            # dispositivo conectado: si no está, la tool lo dice con una frase
            # útil. Registrarlas condicionalmente haría que el modelo negara
            # tener cámara justo cuando el aparato acaba de reconectar.
            analyze_camera,
            show_camera_photo,
        ]
        # Tool de Gmail: solo se registra si Google está habilitado y hay credenciales
        # del usuario (~/.config/asistenteia/google/credentials.json). Si no, ni aparece,
        # y el asistente funciona igual que siempre (retrocompatible).
        self._gmail_enabled = False
        self._calendar_enabled = False
        if getattr(settings, "GOOGLE_ENABLED", False):
            try:
                from src.integrations.google.auth import is_configured
                if is_configured():
                    from src.integrations.google.gmail import gmail_manager
                    from src.integrations.google.calendar import calendar_manager
                    self.tools.append(gmail_manager)
                    self._gmail_enabled = True
                    self.tools.append(calendar_manager)
                    self._calendar_enabled = True
                    logger.info("Tools de Gmail y Calendar registradas (Google configurado).")
                else:
                    logger.info("Google no registrado: faltan credenciales de Google.")
            except Exception as e:  # noqa: BLE001 — deps de Google ausentes u otro fallo
                logger.warning(f"No se pudieron registrar las tools de Google: {e}")
        # Selección activa de herramientas (vacío = todas → comportamiento normal).
        # Si el usuario elige un subconjunto desde la web/GUI, el modelo solo recibe
        # el contexto (esquemas) de esas tools y se le instruye a usar solo esas.
        # Semilla inicial: ASSISTANT_DEFAULT_TOOLS del .env (lo fija la CLI según el
        # modelo activo; vacío = sin restricción). La validación contra los nombres
        # reales se hace más abajo, ya construido self._tool_names.
        self.active_tool_names: set[str] = set()
        # Etiquetas legibles para la UI (fallback: docstring / nombre).
        self._tool_labels = {
            "execute_system_command": "Ejecuta comandos en la terminal",
            "read_log_file": "Lee logs de systemd",
            "clipboard_manager": "Portapapeles (leer/escribir)",
            "web_search": "Búsqueda en internet",
            "system_diagnostics": "Diagnóstico del sistema",
            "read_web_page": "Lee y extrae una página web",
            "play_specific_music": "Reproduce música en Spotify",
            "play_youtube_music": "Reproduce música o vídeos de YouTube",
            "music_control": "Control de reproducción (siguiente, pausa, play)",
            "open_terminal_and_run_command": "Abre terminal y ejecuta",
            "read_terminal_screen": "Lee la pantalla de la terminal",
            "control_local_browser": "Controla el navegador (Chromium)",
            "send_input_to_terminal": "Responde prompts de la terminal",
            "interrupt_terminal_command": "Interrumpe la terminal (Ctrl+C)",
            "launch_application": "Lanza una aplicación",
            "close_application": "Cierra una aplicación",
            "analyze_screen": "Analiza la pantalla (visión)",
            "analyze_clipboard_image": "Analiza imagen del portapapeles",
            "take_screenshot": "Captura de pantalla",
            "analyze_camera": "Mira por la cámara del dispositivo",
            "show_camera_photo": "Enseña la última foto de la cámara",
            "create_document": "Genera un documento ODT",
            "gmail_manager": "Correo de Gmail (leer/enviar/responder)",
            "calendar_manager": "Agenda de Google Calendar (consultar/crear/mover/borrar)",
        }
        # Grupos funcionales para el selector de la UI (menos opciones que 18 tools).
        # La selección efectiva sigue siendo por nombre de tool; los grupos solo agrupan.
        self._tool_groups = [
            {"key": "sistema", "label": "Terminal y sistema", "icon": "🖥️",
             "tools": ["execute_system_command", "open_terminal_and_run_command",
                       "read_terminal_screen", "send_input_to_terminal",
                       "interrupt_terminal_command", "read_log_file", "system_diagnostics"]},
            {"key": "web", "label": "Web e internet", "icon": "🌐",
             "tools": ["web_search", "read_web_page", "control_local_browser"]},
            {"key": "apps", "label": "Aplicaciones", "icon": "🚀",
             "tools": ["launch_application", "close_application"]},
            {"key": "vision", "label": "Pantalla y visión", "icon": "🖼️",
             "tools": ["analyze_screen", "analyze_clipboard_image", "take_screenshot",
                       "analyze_camera", "show_camera_photo"]},
            {"key": "docs", "label": "Documentos y portapapeles", "icon": "📄",
             "tools": ["create_document", "clipboard_manager"]},
            {"key": "musica", "label": "Música", "icon": "🎵",
             "tools": ["play_specific_music", "play_youtube_music", "music_control"]},
        ]
        if self._gmail_enabled:
            self._tool_groups.append(
                {"key": "correo", "label": "Correo (Gmail)", "icon": "📧",
                 "tools": ["gmail_manager"]})
        if self._calendar_enabled:
            self._tool_groups.append(
                {"key": "agenda", "label": "Agenda (Calendar)", "icon": "📅",
                 "tools": ["calendar_manager"]})
        # Para suprimir tool calls que el modelo a veces emite como TEXTO plano (sintaxis
        # Python: nombre(args)) en lugar del formato del motor. Nunca deben llegar al TTS.
        self._tool_names = {t.__name__ for t in self.tools}
        _names = '|'.join(re.escape(n) for n in self._tool_names)
        # Detector (para no hablar): dispara con "nombre_tool(" aunque el paréntesis no
        # se haya cerrado todavía (el stream llega token a token).
        self._tool_leak_re = re.compile(r'\b(?:' + _names + r')\s*\(')
        # Removedor (para el texto visible): quita la llamada completa o su residuo.
        self._tool_call_re = re.compile(r'\b(?:' + _names + r')\s*\([^)]*\)?')
        # Llamada genérica que ocupa todo el fragmento: "algo(arg=...)" / "algo(".
        self._generic_call_re = re.compile(r'^\s*[A-Za-z_]\w*\s*\([^)]*\)?\s*$')
        # Razonamiento del modelo: se MUESTRA en pantalla pero NUNCA se habla. Gemma-4
        # lo delimita con tokens de canal <|channel>...<channel|>; otros modelos usan
        # <think>...</think>. Cubrimos ambos pares (bloques cerrados) y guardamos las
        # aperturas para el caso en streaming donde el cierre aún no llegó.
        self._reason_opens = ("<think>", "<|channel>")
        self._think_re = re.compile(
            r'<think>.*?</think>|<\|channel>.*?<channel\|>', re.DOTALL)

        # Semilla del modo enfocado desde el .env (ASSISTANT_DEFAULT_TOOLS). Sintaxis:
        #   - nombres sueltos => lista blanca (solo esas).
        #   - nombres con prefijo '-' => lista negra (todas MENOS esas).
        #   - vacío => sin restricción (todas, retrocompatible).
        # Solo se aplican nombres válidos; el '-' permite p.ej. "todas menos visión".
        entries = [n.strip() for n in (settings.ASSISTANT_DEFAULT_TOOLS or "").split(",") if n.strip()]
        excludes = {e[1:] for e in entries if e.startswith("-")}
        includes = {e for e in entries if not e.startswith("-")}
        if excludes and not includes:
            self.active_tool_names = {n for n in self._tool_names if n not in excludes}
        else:
            self.active_tool_names = {n for n in includes if n in self._tool_names}
        if self.active_tool_names:
            logger.info(f"Modo enfocado por modelo (semilla .env): {sorted(self.active_tool_names)}")

    # ------------------------------------------------------------------
    # Selección de herramientas (modo enfocado)
    # ------------------------------------------------------------------
    def tool_catalog(self) -> list[dict]:
        """Devuelve [{name, description, active}] para que la UI pinte el selector."""
        catalog = []
        for t in self.tools:
            name = t.__name__
            desc = self._tool_labels.get(name)
            if not desc:
                doc = (inspect.getdoc(t) or "").strip()
                desc = doc.split("\n", 1)[0] if doc else name
            catalog.append({
                "name": name,
                "description": desc,
                "active": name in self.active_tool_names,
            })
        return catalog

    def tool_groups(self) -> list[dict]:
        """Grupos funcionales para el selector. Un grupo está 'active' si TODAS sus
        tools están en la selección activa (vacío global = ninguno marcado = todas)."""
        groups = []
        for g in self._tool_groups:
            members = [
                {"name": n, "description": self._tool_labels.get(n, n)}
                for n in g["tools"] if n in self._tool_names
            ]
            names = [m["name"] for m in members]
            active = bool(names) and all(n in self.active_tool_names for n in names)
            groups.append({
                "key": g["key"], "label": g["label"], "icon": g["icon"],
                "tools": names, "members": members, "active": active,
            })
        return groups

    def get_active_tools(self) -> list[str]:
        """Lista de nombres activos (vacía = todas)."""
        return sorted(self.active_tool_names)

    def set_active_tools(self, names) -> list[str]:
        """Fija la selección activa, ignorando nombres desconocidos.

        Lista vacía (o None) => sin restricción (todas las tools, comportamiento normal).
        Devuelve la selección efectiva resultante.
        """
        valid = {n for n in (names or []) if n in self._tool_names}
        self.active_tool_names = valid
        if valid:
            logger.info(f"Modo enfocado: tools activas = {sorted(valid)}")
        else:
            logger.info("Selección de tools limpiada: todas disponibles.")
        return self.get_active_tools()

    def _selected_tools(self):
        """Lista de funciones tool a pasar al motor según la selección activa."""
        if not self.active_tool_names:
            return self.tools
        return [t for t in self.tools if t.__name__ in self.active_tool_names]

    def _focused_system_prompt(self, base_prompt: str) -> str:
        """Si hay selección activa, añade una instrucción de modo enfocado al prompt."""
        if not self.active_tool_names:
            return base_prompt
        allowed = ", ".join(sorted(self.active_tool_names))
        nota = (
            "\n\n[MODO ENFOCADO] El usuario ha limitado las herramientas disponibles a: "
            f"{allowed}. Usa EXCLUSIVAMENTE estas herramientas y céntrate en esa tarea. "
            "No menciones ni intentes usar otras capacidades; si la petición no encaja "
            "con ellas, dilo con claridad y brevedad."
        )
        return base_prompt + nota

    def _load_system_prompt(self) -> str:
        """System prompt del motor activo, ya con fecha y modo enfocado aplicados.

        Hay DOS prompts porque hay dos repartos de trabajo distintos:

        - `system_prompt.txt` — el de siempre. Un árbol de decisión que enruta petición
          -> nombre de tool ("comando de terminal -> execute_system_command"). Vale
          cuando el bucle lo lleva Luka y es él quien pasa esas tools al modelo.
        - `system_prompt_hermes.txt` — solo persona y voz: quién es Luka, que lo que
          escriba se va a pronunciar, seguridad y no inventarse acciones. Sin enrutado.

        El enrutado es de quien lleva el bucle. Con Hermes lo lleva Hermes, con SU
        catálogo, así que un árbol que nombra las tools de Luka le manda llamar a cosas
        que no tiene delante. Medido el 23/08/2026: el modelo se pasó dos turnos enteros
        buscando `execute_system_command` sin ejecutar nada.

        Y esto se arregla AQUÍ, eligiendo el fichero, no editando el árbol: tocarlo rompe
        tool calls sin relación en los otros motores, que llevan afinados a base de medir.
        """
        hermes = getattr(self.litert, "owns_agent_loop", False)
        nombre = "system_prompt_hermes.txt" if hermes else "system_prompt.txt"
        try:
            system_prompt = (settings.PROJECT_ROOT / "config" / nombre).read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Error cargando system prompt ({nombre}): {e}")
            system_prompt = "Eres un asistente de voz para Linux llamado Luka."
        # Fecha/hora ACTUAL (zona local): imprescindible para resolver "hoy", "mañana",
        # "el viernes a las 5" a ISO 8601 al crear/mover eventos de calendario.
        system_prompt += self._now_context()
        # Modo enfocado: si hay tools seleccionadas, instruir al modelo a usar solo esas.
        return self._focused_system_prompt(system_prompt)

    def _extract_sentences(self, text_buffer: str) -> tuple[List[str], str]:
        """
        Extrae frases completas del buffer basadas en signos de puntuación (.!?:\n)
        seguidos de un espacio o fin de cadena, protegiendo decimales y abreviaciones comunes.
        También corta por coma cuando el buffer supera ~80 caracteres, pero SOLO si
        el fragmento resultante parece texto natural (no código/JSON).
        """
        # Si el buffer es largo, también cortar por coma para frases más cortas
        if len(text_buffer) > 80:
            pattern = re.compile(r'([.!?:])(?=\s|$)|(\n)|,(?=\s)')
        else:
            pattern = re.compile(r'([.!?:])(?=\s|$)|(\n)')
        
        sentences = []
        start = 0
        for match in pattern.finditer(text_buffer):
            end = match.end()
            sentence = text_buffer[start:end].strip()
            if sentence:
                sentences.append(sentence)
            start = end
            
        remaining = text_buffer[start:]
        return sentences, remaining

    async def _synth_worker(self, queue_text: asyncio.Queue, queue_audio: asyncio.Queue) -> None:
        """
        Consumidor de frases de texto: sintetiza cada frase a numpy array
        y lo encola para reproducción. Permite solapamiento con _play_worker.
        """
        logger.info("Worker de síntesis TTS iniciado")
        try:
            while True:
                sentence = await queue_text.get()
                if sentence is None:
                    queue_text.task_done()
                    logger.info("Worker de síntesis recibió señal de finalización")
                    await queue_audio.put(None)
                    break
                
                logger.info(f"Sintetizando: '{sentence}'")
                try:
                    audio_np = await self.tts.synthesize_only(sentence)
                    if audio_np is not None:
                        await queue_audio.put(audio_np)
                except Exception as e:
                    logger.error(f"Error sintetizando '{sentence}': {e}")
                finally:
                    queue_text.task_done()
        except asyncio.CancelledError:
            logger.info("Worker de síntesis cancelado activamente")
            raise
        except Exception as e:
            logger.error(f"Error inesperado en worker de síntesis: {e}")

    async def _play_worker(self, queue_audio: asyncio.Queue) -> None:
        """
        Consumidor de audio: reproduce arrays numpy usando un OutputStream persistente.
        Se ejecuta en paralelo con _synth_worker para eliminar gaps entre frases.
        """
        logger.info("Worker de reproducción TTS iniciado")
        # Si en este turno salió audio hacia el dispositivo, al cerrar hay que
        # avisarle de que no viene más —tanto si la cola se agota como si nos
        # cancelan—: sin el TTS_END el aparato se quedaría «hablando» hasta el
        # timeout del firmware, sordo mientras tanto.
        sent_to_device = False
        try:
            while True:
                audio_np = await queue_audio.get()
                if audio_np is None:
                    queue_audio.task_done()
                    logger.info("Worker de reproducción recibió señal de finalización")
                    break

                try:
                    # Dispositivo satélite primero: se manda por red mientras el PC
                    # reproduce, en vez de después, para no sumar latencias.
                    if self.audio_sink is not None and self.audio_target in ("device", "both"):
                        try:
                            await self.audio_sink(audio_np, TTSEngine.SAMPLE_RATE)
                            sent_to_device = True
                        except Exception as e:  # noqa: BLE001 — el enlace no debe tumbar el TTS
                            logger.warning(f"No se pudo enviar audio al dispositivo: {e}")
                    if self.audio_target in ("pc", "both"):
                        await self.tts.play_audio_array(audio_np)
                except Exception as e:
                    logger.error(f"Error reproduciendo audio: {e}")
                finally:
                    queue_audio.task_done()
        except asyncio.CancelledError:
            logger.info("Worker de reproducción cancelado activamente")
            raise
        except Exception as e:
            logger.error(f"Error inesperado en worker de reproducción: {e}")
        finally:
            if sent_to_device and self.audio_sink_end is not None:
                try:
                    await self.audio_sink_end()
                except Exception as e:  # noqa: BLE001 — el aviso no debe tumbar el worker
                    logger.warning(f"No se pudo avisar del fin de audio al dispositivo: {e}")
            # Cerrar el stream persistente al terminar
            self.tts.close_persistent_stream()

    async def process_audio(
        self,
        audio_path: Path,
        conversation_history: list[ChatMessage],
        sink_id: Optional[str] = None,
        max_history: int = 10,
        on_transcription: Optional[Callable[[str], None]] = None
    ) -> dict:
        """Transcribe el audio y procesa el texto resultante.

        on_transcription se invoca en cuanto el STT termina (antes de generar la
        respuesta), para que la UI pueda mostrar lo que dijo el usuario al instante."""
        await self.send_notification_async("Procesando audio...")

        text = await self.stt.transcribe(audio_path)

        if not text or len(text.strip()) < 2:
            await self.send_notification_async("No se detectó voz o el mensaje es muy corto.")
            return {"status": "error", "message": "No se detectó voz", "transcribed_text": None}

        # La notificación "Has dicho" la emite el hook on_transcription en el
        # instante exacto del reconocimiento (ver main.py).
        if on_transcription:
            on_transcription(text)
        result = await self.process_transcription(text, conversation_history, sink_id, max_history)
        result["transcribed_text"] = text
        return result

    def send_notification(self, message: str, title: str = "AsistenteIA") -> None:
        """Envía una notificación de escritorio y emite un bip si es inicio de escucha."""
        try:
            subprocess.Popen(["notify-send", title, message])

            # Si el mensaje es de inicio de escucha, emitir un bip sonoro
            if message == "Escuchando...":
                # Intentar reproducir un sonido de sistema estándar
                beep_sound = "/usr/share/sounds/freedesktop/stereo/message.oga"
                if not Path(beep_sound).exists():
                    beep_sound = "/usr/share/sounds/freedesktop/stereo/complete.oga"

                if Path(beep_sound).exists():
                    subprocess.Popen(["paplay", beep_sound],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.warning(f"No se pudo enviar notificación o bip: {e}")

    async def send_notification_async(self, message: str, title: str = "AsistenteIA") -> None:
        """Envía una notificación de escritorio de forma asíncrona (no bloquea el loop)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", title, message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            # No esperamos a que termine, solo lo lanzamos y seguimos
            asyncio.create_task(proc.wait())

            if message == "Escuchando...":
                beep_sound = "/usr/share/sounds/freedesktop/stereo/message.oga"
                if not Path(beep_sound).exists():
                    beep_sound = "/usr/share/sounds/freedesktop/stereo/complete.oga"

                if Path(beep_sound).exists():
                    proc_sound = await asyncio.create_subprocess_exec(
                        "paplay", beep_sound,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    asyncio.create_task(proc_sound.wait())
        except Exception as e:
            logger.warning(f"No se pudo enviar notificación o bip: {e}")

    def _is_speakable(self, text: str) -> bool:
        """Filtra fragmentos que parecen código/JSON y no deben ir al TTS."""
        if not text or len(text) < 3:
            return False
        # Rechazar fragmentos que empiezan con caracteres de código
        if text[0] in ('[', '{', '(', '<', '`', '}', ']', ')', '>', '*', '#', '-', '|', '\\'):
            return False
        # Rechazar si tiene más símbolos que letras (código/JSON)
        alpha_count = sum(1 for c in text if c.isalpha())
        symbol_count = sum(1 for c in text if c in '[]{}()<>`\'"=;:,')
        if symbol_count > alpha_count:
            return False
        # Rechazar si es solo números y puntuación
        if not any(c.isalpha() for c in text):
            return False
        # Rechazar tool calls fugadas como texto plano (p.ej. take_screenshot(target='x'))
        if self._tool_leak_re.search(text) or self._generic_call_re.match(text):
            return False
        # Rechazar identificadores snake_case: el habla en español/inglés nunca lleva
        # guion bajo, así que esto delata nombres de función fugados (analyze_screen, etc.)
        if re.search(r'\w_\w', text):
            return False
        return True

    # --- Guardarraíl anti-invención -----------------------------------------------
    # El modelo a veces afirma en primera persona haber hecho algo sin haber llamado a
    # ninguna herramienta: pasó de verdad con "Te he abierto la página de YouTube" tras
    # un fallo de parseo que dejó al motor sin tools. Estas frases se RETIENEN hasta
    # saber si alguna tool se ejecutó; si no se ejecutó ninguna, no se hablan.
    _ACTION_CLAIM_RE = re.compile(
        r"\b("
        r"(?:te\s+)?(?:lo\s+|la\s+|los\s+|las\s+)?he\s+"
        r"(?:abierto|lanzado|ejecutado|puesto|enviado|cerrado|guardado|creado|"
        r"borrado|reproducido|copiado|buscado|instalado|a[ñn]adido|movido|archivado)"
        r"|ya\s+(?:lo|la|los|las)\s+(?:tienes|he\s+|est[áa])"
        r"|ya\s+est[áa]\s+(?:abierto|abierta|hecho|hecha|listo|lista|sonando|puesto)"
        r"|est(?:oy|á)\s+(?:abriendo|reproduci[ée]ndose|ejecut[áa]ndose|son[áa]ndo)"
        r"|reproduciendo\b"
        r")",
        re.IGNORECASE,
    )
    # "Hecho." / "Listo." como frase entera SÍ es una confirmación; "de hecho" dentro de
    # una explicación no lo es, así que no vale buscar la palabra suelta.
    _BARE_CONFIRM_RE = re.compile(
        r"^\s*(hecho|listo|ya est[áa]|vale)\s*[.!]?\s*$", re.IGNORECASE
    )
    # Lo que se dice cuando el modelo afirmó una acción que nunca se ejecutó.
    _NO_ACTION_MSG = "No he podido ejecutar la acción. ¿Lo intento otra vez?"

    def _claims_action(self, text: str) -> bool:
        """¿Esta frase afirma que una acción ya se ha realizado?"""
        if not text:
            return False
        return (self._ACTION_CLAIM_RE.search(text) is not None
                or self._BARE_CONFIRM_RE.match(text) is not None)

    def _tool_truth(self) -> tuple[bool, bool, bool]:
        """(se_puede_verificar, se_ejecutó_alguna_tool, el_motor_se_quedó_sin_tools).

        Si el motor no puede informar de las tools ejecutadas, el guardarraíl se
        desactiva: dar por inventado todo turno sería mucho peor que el problema.
        """
        supported = bool(getattr(self.litert, "tracks_tool_usage", False))
        used = bool(getattr(self.litert, "last_turn_tools_used", None))
        disabled = bool(getattr(self.litert, "last_turn_tools_disabled", False))
        return supported, used, disabled

    # Sí/no para confirmar acciones de correo irreversibles (enviar/responder/papelera/
    # archivar). Se comprueba ANTES de llamar al modelo: si hay una acción de Gmail
    # pendiente y el usuario afirma o niega, se resuelve aquí sin pasar por el LLM.
    _YES_RE = re.compile(
        r"\b(s[ií]|vale|venga|adelante|claro|correcto|confirmo|confirmado|hazlo|"
        r"env[ií]al[oa]|m[áa]ndal[oa]|de acuerdo|por supuesto|ok|okay|dale|perfecto)\b",
        re.IGNORECASE)
    _NO_RE = re.compile(
        r"\b(no|nop|para|cancela|cancelar|d[ée]jal[oa]|olv[ií]dal[oa]|mejor no|"
        r"espera|negativo|anula|anular)\b",
        re.IGNORECASE)

    @staticmethod
    def _now_context() -> str:
        """Línea de fecha/hora local para el system prompt (ancla las fechas relativas)."""
        from datetime import datetime
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        now = datetime.now().astimezone()
        fecha = f"{dias[now.weekday()]} {now.day:02d}/{now.month:02d}/{now.year}"
        return ("\n\nFECHA Y HORA ACTUAL (zona local del usuario): "
                f"{fecha}, {now.hour:02d}:{now.minute:02d} (ISO: {now.isoformat(timespec='minutes')}). "
                "Úsala para resolver fechas relativas (\"hoy\", \"mañana\", \"el viernes a las 5\") a "
                "ISO 8601 al usar el calendario.")

    async def _resolve_pending(self, text, peek, run, clear) -> Optional[str]:
        """Interpreta el sí/no del usuario para UNA acción Google pendiente (Gmail o Calendar).

        Devuelve el texto de resultado (a hablar/mostrar) si se resuelve, o None si no hay
        nada pendiente o el turno es ambiguo (el flujo normal sigue y la acción caduca sola).
        """
        if peek() is None:
            return None
        # Negar tiene prioridad: "no, mejor no" no debe ejecutar nada.
        if self._NO_RE.search(text):
            clear()
            return "De acuerdo, lo dejo."
        if self._YES_RE.search(text):
            return await run()
        return None

    async def _resolve_google_confirmation(self, text: str) -> Optional[str]:
        """Resuelve una confirmación pendiente de Gmail o de Calendar (la que haya).

        En la práctica solo una está pendiente a la vez; se comprueban en orden y se
        devuelve el primer resultado no nulo.
        """
        if self._gmail_enabled:
            from src.integrations.google import gmail
            res = await self._resolve_pending(
                text, gmail.peek_pending, gmail.run_pending, gmail.clear_pending)
            if res is not None:
                return res
        if self._calendar_enabled:
            from src.integrations.google import calendar
            res = await self._resolve_pending(
                text, calendar.peek_pending, calendar.run_pending, calendar.clear_pending)
            if res is not None:
                return res
        return None

    def without_reasoning(self, text: str) -> str:
        """El texto sin el razonamiento del modelo, para enviarlo fuera del proceso.

        Misma lógica que se usa para decidir qué se habla, pero con nombre público
        porque el destinatario no siempre es el TTS: la pasarela de dispositivos
        también tiene que limpiar la trama REPLY. Tener dos filtros distintos ya
        salió mal una vez —el del gateway era un regex sobre bloques cerrados que
        no cubría el razonamiento implícito—, así que hay uno solo.
        """
        return self._strip_think_for_tts(text)

    def _strip_think_for_tts(self, text: str) -> str:
        """Devuelve solo el texto hablable, quitando el razonamiento del modelo.

        Modelos con razonamiento EXPLÍCITO en el stream emiten el razonamiento entre
        marcadores (`<|channel>...<channel|>` en Gemma-4, `<think>...</think>` en otros)
        seguido de la respuesta. El razonamiento se muestra en la GUI pero no debe
        hablarse. Pensado para llamarse con el texto acumulado en cada chunk: el
        resultado crece de forma monótona como prefijo, así que el llamante solo
        necesita reproducir lo nuevo.
        """
        # Fuera los bloques de razonamiento ya cerrados (de cualquier par conocido).
        text = self._think_re.sub("", text)
        # ¿Alguna apertura sin su cierre? (razonamiento aún en streaming): cortar desde
        # la primera que aparezca; todo lo posterior es razonamiento todavía no cerrado.
        cut = len(text)
        for opener in self._reason_opens:
            i = text.find(opener)
            if i != -1:
                cut = min(cut, i)
        text = text[:cut]
        # Retener un marcador de apertura partido entre chunks (p.ej. la cola "...<|chan")
        # para no hablar medio token ni adelantar texto que aún podría ser razonamiento.
        for opener in self._reason_opens:
            for k in range(len(opener) - 1, 0, -1):
                if text.endswith(opener[:k]):
                    return text[: len(text) - k]
        return text

    @staticmethod
    def _is_meaningful_response(cleaned_text: str, clean_stream: bool) -> bool:
        """¿El texto del modelo es una respuesta real (no residuo de tool call)?

        Con un motor de stream limpio (ExLlama: ejecuta las tools internamente y
        emite solo el texto final) cualquier respuesta no vacía vale. Con LiteRT,
        que fuga las tool calls como texto, tras limpiarlas puede quedar un residuo
        corto; por eso se exige longitud mínima para no hablar basura. Ese umbral NO
        debe aplicarse a ExLlama: confundiría respuestas cortas válidas ("42", "París")
        con residuos y dispararía un fallback espurio.
        """
        t = (cleaned_text or "").strip()
        if not t:
            return False
        if clean_stream:
            return True
        if len(t) > 15:
            return True

        # Corta, pero puede ser perfectamente válida: "Eres Victor.", "Son las
        # tres.", "Sí.". Medir solo la longitud las confundía con residuo y
        # disparaba el fallback, que además interpreta como orden de terminal
        # cualquier frase con "qué" o "dime" y contesta "Ejecutando comando en la
        # terminal". Una respuesta buena acababa sustituida por un disparate.
        #
        # Lo que distingue una frase de un residuo no es el largo: es estar
        # terminada. Un resto de tool call se corta a media palabra o arrastra
        # paréntesis y marcadores; una respuesta acaba en punto.
        residuo = "(" in t or "call" in t.lower() or "<|" in t
        return not residuo and t[-1] in ".!?"

    async def process_transcription_stream(
        self,
        text: str,
        conversation_history: list[ChatMessage],
        sink_id: Optional[str] = None,
        max_history: int = 10
    ):
        """
        Procesa el texto del usuario usando el motor activo y herramientas de forma reactiva,
        transmitiendo los chunks resultantes en tiempo real y sintetizando voz frase a frase
        con pipeline de doble cola (síntesis || reproducción en paralelo).
        """
        # El nombre del motor va en el log porque es la línea que se mira para saber
        # QUIÉN está respondiendo; decir siempre "LiteRT" hacía dudar de si el cambio
        # de motor había surtido efecto.
        motor = getattr(self.litert, "name", "motor")
        logger.info(f"Procesando transcripción con {motor} (streaming): {text[:100]}...")

        # Las tools necesitan saber qué ha pedido el usuario, no solo sus argumentos:
        # web_search se niega a buscar si no se lo han pedido explícitamente.
        set_current_utterance(text)

        # Cancelar TTS previo si existe
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
        if self._current_play_task and not self._current_play_task.done():
            self._current_play_task.cancel()
        self.tts.stop()   # Asegurar parada inmediata del proceso
        self.tts.rearm()  # Baja la señal de corte del turno anterior y reactiva síntesis

        # Pipeline de doble cola: texto → síntesis → audio → reproducción
        queue_text = asyncio.Queue()
        queue_audio = asyncio.Queue()
        
        # Arrancar ambos workers en paralelo
        synth_task = asyncio.create_task(self._synth_worker(queue_text, queue_audio))
        play_task = asyncio.create_task(self._play_worker(queue_audio))
        self._current_tts_task = synth_task
        self._current_play_task = play_task

        # Añadir mensaje del usuario al historial
        conversation_history.append(ChatMessage(role="user", content=text))
        if len(conversation_history) > max_history:
            conversation_history[:] = conversation_history[-max_history:]

        # Confirmación de acción de Google pendiente: si en el turno anterior una tool
        # (Gmail: enviar/responder/papelera/archivar; Calendar: borrar/mover) dejó algo a
        # confirmar, este turno es el sí/no. Se resuelve sin pasar por el modelo.
        google_confirm = await self._resolve_google_confirmation(text)
        if google_confirm is not None:
            conversation_history.append(ChatMessage(role="assistant", content=google_confirm))
            if self._is_speakable(google_confirm):
                await queue_text.put(google_confirm)
            await queue_text.put(None)  # cerrar el pipeline de síntesis
            yield google_confirm
            return

        # System prompt del motor activo (árbol de decisión, o solo persona si el motor
        # lleva su propio bucle agéntico). Ver _load_system_prompt.
        system_prompt = self._load_system_prompt()

        accumulated_text = ""
        sentence_buffer = ""
        chunk_count = 0
        used_fallback = False
        # Modelos "thinking" (Qwen3.5...): el contenido empieza con el razonamiento y un
        # </think> lo cierra antes de la respuesta. El razonamiento SE MUESTRA en pantalla
        # pero NO se habla: el TTS queda cerrado hasta pasar </think>. Para el resto de
        # motores/modelos, tts_open=True desde el principio (comportamiento de siempre).
        leads_reasoning = getattr(self.litert, "leads_with_reasoning", False)
        tts_open = not leads_reasoning
        think_open_sent = False  # apertura <think> sintética para el display (ver más abajo)
        speakable_fed = 0  # chars de texto hablable ya pasados al pipeline de TTS (modelos
        # con razonamiento explícito <think>...</think> en el stream, p.ej. LiteRT)
        # Frases que afirman una acción y todavía no se pueden dar por buenas. El TTS
        # habla frase a frase mientras llega el stream, así que comprobar al final no
        # sirve: la mentira ya se habría oído. Se retienen SOLO estas (respuestas de una
        # línea), no el resto, para no añadir latencia a las respuestas largas.
        held_claims: list[str] = []

        try:
            # Primera llamada al modelo
            async for chunk in self.litert.chat_stream(
                prompt=text,
                tools=self._selected_tools(),
                system_prompt=system_prompt,
                history=conversation_history[:-1] # Pasamos el historial previo
            ):
                chunk_count += 1
                accumulated_text += chunk
                
                # Filtrar tool calls antes de procesar para TTS
                is_tool_call = (
                    "<|tool_call|>" in chunk or "call:" in chunk
                    or self._tool_leak_re.search(accumulated_text) is not None
                )
                
                if not is_tool_call:
                    if leads_reasoning:
                        # Razonamiento IMPLÍCITO: el stream EMPIEZA dentro del <think> (la
                        # apertura la inyecta la plantilla) y solo llega el cierre </think>.
                        # No se habla nada hasta pasarlo; entonces se siembra con la respuesta.
                        if not tts_open and "</think>" in accumulated_text:
                            tts_open = True
                            sentence_buffer = accumulated_text.split("</think>", 1)[1]
                        elif tts_open:
                            sentence_buffer += chunk
                    else:
                        # Razonamiento EXPLÍCITO (<think>...</think> en el stream) o sin
                        # razonamiento: reconstruimos el texto hablable sin los bloques de
                        # razonamiento y alimentamos solo la parte nueva.
                        speakable = self._strip_think_for_tts(accumulated_text)
                        if len(speakable) > speakable_fed:
                            sentence_buffer += speakable[speakable_fed:]
                            speakable_fed = len(speakable)
                    if tts_open:
                        sentences, sentence_buffer = self._extract_sentences(sentence_buffer)
                        supported, tools_used, _ = self._tool_truth()
                        for s in sentences:
                            clean = strip_markdown(s)
                            if not self._is_speakable(clean):
                                continue
                            # Afirma una acción y todavía no consta ninguna tool
                            # ejecutada: se retiene hasta el final del stream.
                            if supported and not tools_used and self._claims_action(clean):
                                logger.info(f"Afirmación retenida hasta verificar tools: {clean!r}")
                                held_claims.append(clean)
                                continue
                            await queue_text.put(clean)

                # Yield al cliente (sin tool calls). Se conserva el </think> que cierra el
                # razonamiento: la interfaz lo usa como frontera para mostrar el razonamiento
                # PLEGADO y la respuesta destacada. Qwen3.5 no emite el <think> de apertura
                # (lo inyecta la plantilla), así que lo sintetizamos una vez al principio para
                # que el front pueda plegar el razonamiento desde el primer token.
                clean_chunk = self._clean_tool_calls(chunk, strip=False)
                if clean_chunk:
                    if leads_reasoning and not think_open_sent:
                        clean_chunk = "<think>" + clean_chunk
                        think_open_sent = True
                    yield clean_chunk

            # Diagnóstico: qué marcadores de razonamiento/tool trae el stream crudo.
            # Sirve para afinar el filtrado de TTS si algún razonamiento se cuela.
            supported, tools_used, tools_disabled = self._tool_truth()
            logger.info(
                "TTS markers crudos: think=%s channel=%s tool=%s | tools=%s | head=%r",
                "<think>" in accumulated_text,
                ("<|channel>" in accumulated_text or "<channel|>" in accumulated_text),
                ("call:" in accumulated_text or "<|tool_call>" in accumulated_text),
                getattr(self.litert, "last_turn_tools_used", None),
                accumulated_text[:300],
            )

            # Veredicto sobre las afirmaciones retenidas: ya se sabe si alguna tool se
            # ejecutó de verdad en este turno.
            fabricated_claim = False
            if held_claims:
                if tools_used:
                    for clean in held_claims:
                        await queue_text.put(clean)
                else:
                    razon = ("el motor se quedó sin herramientas tras fallar el parseo"
                             if tools_disabled else "el modelo no llamó a ninguna herramienta")
                    logger.warning(
                        f"Afirmación inventada descartada ({razon}): {held_claims!r}"
                    )
                    fabricated_claim = True
                    await queue_text.put(self._NO_ACTION_MSG)
                    yield "\n" + self._NO_ACTION_MSG
                    # El texto del modelo no se guarda: si la mentira entra en el
                    # historial, el turno siguiente la imita y confirma sin ni siquiera
                    # intentar la llamada (pasó tal cual en producción).
                    accumulated_text = self._NO_ACTION_MSG
                held_claims = []

            # --- Segunda pasada de visión ---
            # Si analyze_screen capturó una imagen, el motor (LLM en GPU, visión en CPU)
            # no puede invocarse de forma reentrante desde el tool, así que la describimos
            # aquí, en una segunda llamada con la imagen adjunta. Esta respuesta sustituye
            # a la de la primera pasada (que solo disparó el tool).
            # --- Segunda pasada de documento ---
            # create_document solo stagea el título; el CONTENIDO se redacta aquí como
            # texto normal (fiable), no como argumento de tool (que el modelo malforma).
            pending_doc = get_pending_document()
            pending = get_pending_vision()
            if pending_doc:
                logger.info(f"Documento pendiente. Segunda pasada para redactar: {pending_doc}")
                doc_system = (
                    "Eres un redactor experto. Devuelve SOLO el contenido del documento "
                    "solicitado en Markdown (usa #, ##, listas con - y **negrita** donde "
                    "convenga). Nada de saludos, comentarios ni explicaciones: solo el documento."
                )
                doc_prompt = f"Documento titulado '{pending_doc}'. Redáctalo según esta petición del usuario: {text}"
                sentence_buffer = ""  # descartar residuo de la 1ª pasada (tool call)
                markdown = ""
                async for dchunk in self.litert.chat_stream(
                    prompt=doc_prompt,
                    tools=None,
                    system_prompt=doc_system,
                    history=None,
                ):
                    markdown += dchunk
                # El contenido NO se habla; se construye el .odt y se confirma en breve.
                result = await build_and_save_document(pending_doc, markdown)
                accumulated_text = result
                # Confirmación hablada fija y limpia: el nombre de archivo lleva guiones
                # bajos que el filtro de TTS descarta, así que solo va en el texto del chat.
                ok = result.startswith("Documento '")
                spoken = "Listo, he creado el documento y lo he abierto en LibreOffice." if ok else result
                if self._is_speakable(spoken):
                    await queue_text.put(spoken)
                yield result
                has_meaningful_response = True
            elif pending:
                pending_image, prompt_hint = pending
                logger.info(f"Captura pendiente detectada. Segunda pasada de visión: {pending_image}")
                vision_system = (
                    "Eres Luka. Describe de forma clara, concisa y útil lo que aparece en "
                    "la captura del usuario, respondiendo a su petición en español. "
                    "Básate únicamente en lo que realmente se ve en la imagen."
                )
                vision_prompt = prompt_hint or text
                sentence_buffer = ""  # descartar residuo de la primera pasada (tool call)
                vision_answer = ""
                try:
                    async for vchunk in self.litert.chat_stream(
                        prompt=vision_prompt,
                        tools=None,
                        system_prompt=vision_system,
                        history=None,
                        image_path=pending_image,
                    ):
                        vision_answer += vchunk
                        sentence_buffer += vchunk
                        sentences, sentence_buffer = self._extract_sentences(sentence_buffer)
                        for s in sentences:
                            clean = strip_markdown(s)
                            if self._is_speakable(clean):
                                await queue_text.put(clean)
                        clean_chunk = self._clean_tool_calls(vchunk, strip=False)
                        if clean_chunk:
                            yield clean_chunk
                    accumulated_text = vision_answer
                finally:
                    Path(pending_image).unlink(missing_ok=True)
                has_meaningful_response = True
            else:
                # Verificar si la respuesta es significativa. Si no, generar fallback.
                accumulated_clean = self._clean_tool_calls(accumulated_text)
                clean_stream = getattr(self.litert, "streams_clean_text", False)
                has_meaningful_response = self._is_meaningful_response(accumulated_clean, clean_stream)

                # Seguridad: modelo "thinking" que no llegó a emitir </think> (raro), por lo
                # que no se habló nada. Hablamos la respuesta para no quedar mudos.
                if leads_reasoning and not tts_open and has_meaningful_response:
                    spoken = strip_markdown(accumulated_clean).replace("<think>", "").replace("</think>", "")
                    if self._is_speakable(spoken):
                        await queue_text.put(spoken)
                    tts_open = True

            if not has_meaningful_response:
                used_fallback = True
                logger.info("Respuesta insuficiente. Generando fallback automático...")
                lower_text = text.lower()

                # Rama de terminal: se decide por la tool que SE EJECUTÓ, no por las
                # palabras del usuario. Los indicadores de antes incluían "dime" o "qué",
                # así que cualquier pregunta acababa raspando el panel de tmux y
                # reportando el resultado de un comando anterior sin relación.
                tools_run = set(getattr(self.litert, "last_turn_tools_used", None) or ())
                is_terminal = bool(tools_run & _TERMINAL_TOOL_NAMES)

                # Si no se ejecutó ninguna herramienta, no hay nada que confirmar: las
                # frases de éxito de más abajo se deducían de las palabras del USUARIO,
                # así que afirmaban acciones que nunca ocurrieron.
                if supported and not tools_used:
                    logger.warning(
                        "Fallback sin ninguna tool ejecutada: se responde con la verdad "
                        "en vez de confirmar una acción inexistente."
                    )
                    fallback = self._NO_ACTION_MSG
                    fabricated_claim = True
                elif is_terminal:
                    yield "⏳ Ejecutando comando en la terminal..."
                    await queue_text.put("Ejecutando comando en la terminal.")
                    await asyncio.sleep(3.0)
                    
                    try:
                        from src.command_executor import _run_tmux_cmd
                        ok, screen_output = await _run_tmux_cmd(["capture-pane", "-p", "-t", "asistenteia", "-S", "-200"])
                        
                        if not ok or not screen_output:
                            fallback = "No se pudo leer la salida de la terminal."
                        else:
                            clean_output = re.sub(r'\x1b\[[0-9;]*m', '', screen_output)
                            clean_output = re.sub(r'\033\[[0-9;]*m', '', clean_output)
                            
                            logger.info(f"Salida de terminal tras tool calling (últimas 500 chars): {clean_output[-500:]}...")
                            
                            has_error = ("ERROR" in clean_output and "❌" in clean_output)
                            has_success = ("OK" in clean_output and "✅" in clean_output)
                            
                            if has_error:
                                error_match = re.search(r"❌\s+(\S+)\s+ERROR\s+(\d+)", clean_output)
                                if error_match:
                                    cmd_name = error_match.group(1)
                                    exit_code = int(error_match.group(2))
                                    fallback = f"El comando '{cmd_name}' falló con código {exit_code}. "
                                    fallback += {127: "Comando no encontrado.", 13: "Permiso denegado.", 126: "Sin permisos."}.get(exit_code, "Revisa el comando.")
                                else:
                                    fallback = "El comando falló en la terminal."
                            elif has_success:
                                success_match = re.search(r"✅\s+(\S+)\s+OK", clean_output)
                                if success_match:
                                    cmd_name = success_match.group(1)
                                    lines = clean_output.splitlines()
                                    relevant = []
                                    skip_patterns = [
                                        "AsistenteIA:", "CONTENIDO VISIBLE", "❯", "bash /tmp/",
                                        "~/", "SALIDA DE LA TERMINAL", "cmd_", "Éxito:",
                                        "comando ejecutado", "Se ha enviado", "Qué significa:",
                                        "✅", "❌", "│", "├", "└", "┌", "─",
                                        "run.sh", "EXIT_CODE", "echo", "if [", "fi", "else",
                                        "test $?", "finalizado correctamente", "completado exitosamente",
                                        "OK]", "ERROR"
                                    ]
                                    for l in lines:
                                        stripped = l.strip()
                                        if stripped and len(stripped) > 1 and not any(p in l for p in skip_patterns):
                                            relevant.append(stripped)
                                    
                                    if relevant:
                                        output_lines = relevant[-5:]
                                        output_text = " ".join(output_lines)
                                        info_keywords = ["versión", "version", "ip", "dirección", "característica", 
                                                         "cuánto", "cuánta", "qué", "cual", "lista", "listado",
                                                         "espacio", "disco", "memoria", "cpu", "gpu", "kernel"]
                                        is_info_query = any(kw in lower_text for kw in info_keywords)
                                        fallback = output_text if is_info_query else f"El comando '{cmd_name}' se ejecutó correctamente. Resultado: {output_text}"
                                    else:
                                        fallback = f"El comando '{cmd_name}' se ejecutó correctamente."
                                else:
                                    fallback = "Comando ejecutado correctamente en la terminal."
                            else:
                                lines = clean_output.splitlines()
                                relevant = []
                                skip_patterns = [
                                    "AsistenteIA:", "CONTENIDO VISIBLE", "❯", "bash /tmp/",
                                    "~/", "SALIDA DE LA TERMINAL", "cmd_", "Éxito:",
                                    "comando ejecutado", "Se ha enviado", "Qué significa:",
                                    "✅", "❌", "│", "├", "└", "┌", "─",
                                    "run.sh", "EXIT_CODE", "echo", "if [", "fi", "else",
                                    "test $?", "OK]", "ERROR"
                                ]
                                for l in lines:
                                    stripped = l.strip()
                                    if stripped and len(stripped) > 1 and not any(p in l for p in skip_patterns):
                                        relevant.append(stripped)
                                
                                if relevant:
                                    output_lines = relevant[-5:]
                                    output_text = " ".join(output_lines)
                                    info_keywords = ["versión", "version", "ip", "dirección", "característica", 
                                                     "cuánto", "cuánta", "qué", "cual", "lista", "listado",
                                                     "espacio", "disco", "memoria", "cpu", "gpu", "kernel"]
                                    is_info_query = any(kw in lower_text for kw in info_keywords)
                                    fallback = output_text if is_info_query else f"Comando ejecutado. Resultado: {output_text}"
                                else:
                                    fallback = "Comando ejecutado en la terminal."
                    except Exception as e:
                        logger.error(f"Error leyendo terminal tras tool calling: {e}")
                        fallback = "Comando ejecutado en la terminal."
                else:
                    # Se describe la tool que se ejecutó, no lo que el usuario dijo:
                    # antes "pon" bastaba para anunciar "Reproduciendo música" aunque
                    # la tool que hubiera corrido fuese otra (o ninguna).
                    fallback = next(
                        (msg for name, msg in _FALLBACK_BY_TOOL.items() if name in tools_run),
                        "Acción ejecutada correctamente.",
                    )

                accumulated_text = fallback
                yield fallback
                await queue_text.put(fallback)

            # Encolar lo que quede en el buffer. Si usamos fallback, el residuo es basura
            # de la tool call fugada (el fallback ya es la respuesta hablada): descartarlo.
            # Con una afirmación inventada descartada, el residuo es parte de esa misma
            # mentira: tampoco se habla.
            if sentence_buffer.strip() and not used_fallback and not fabricated_claim:
                clean = strip_markdown(sentence_buffer)
                if self._is_speakable(clean):
                    await queue_text.put(clean)

            # Guardar en el historial lo REALMENTE dicho. Si el guardarraíl sustituyó
            # una afirmación inventada, `accumulated_text` ya es la versión honesta:
            # meter la mentira aquí hacía que el turno siguiente la imitase.
            cleaned_final = self._clean_tool_calls(accumulated_text)
            conversation_history.append(ChatMessage(role="assistant", content=cleaned_final))

        except asyncio.CancelledError:
            logger.info("process_transcription_stream cancelado. Deteniendo workers de TTS...")
            if self._current_tts_task and not self._current_tts_task.done():
                self._current_tts_task.cancel()
            if self._current_play_task and not self._current_play_task.done():
                self._current_play_task.cancel()
            self.tts.stop()
            raise
        except Exception as e:
            logger.error(f"Error en process_transcription_stream: {e}")
            self.tts.stop()
            raise
        finally:
            # Enviar señal de fin a workers pero NO esperarlos
            # El stream debe cerrarse inmediatamente para que la GUI actualice el texto
            await queue_text.put(None)
            # Los workers continúan en background reproduciendo audio pendiente

    def _clean_tool_calls(self, text: str, strip: bool = True) -> str:
        """Elimina tokens de tool call del texto visible al usuario.
        
        LiteRT genera tool calls como: <|tool_call|>call:func{args}<|tool_call|>
        """
        import re
        cleaned = re.sub(r'<\|tool_call\|>.*?<\|tool_call\|>', '', text, flags=re.DOTALL)
        cleaned = re.sub(r'<\|tool_call\|>.*', '', cleaned, flags=re.DOTALL)
        cleaned = re.sub(r'.*<\|tool_call\|>', '', cleaned, flags=re.DOTALL)
        # Tool calls que el modelo emite como texto plano (sintaxis Python).
        cleaned = self._tool_call_re.sub('', cleaned)
        return cleaned.strip() if strip else cleaned

    async def process_transcription(
        self,
        text: str,
        conversation_history: list[ChatMessage],
        sink_id: Optional[str] = None,
        max_history: int = 10
    ) -> dict:
        """
        Versión síncrona para compatibilidad: usa chat_stream para TTS en tiempo real
        pero obtiene la respuesta final formateada correctamente via chat().
        """
        # Ejecutar streaming para TTS en tiempo real
        response_text = ""
        async for chunk in self.process_transcription_stream(
            text=text,
            conversation_history=conversation_history,
            sink_id=sink_id,
            max_history=max_history
        ):
            response_text += chunk
        
        # Limpiar tool calls del texto visible
        visible_text = self._clean_tool_calls(response_text)
        
        # Si el texto no tiene espacios (bug de LiteRT), obtener respuesta limpia via chat()
        if visible_text and " " not in visible_text and len(visible_text) > 10:
            logger.info("Respuesta sin espacios, obteniendo versión limpia via chat()...")
            try:
                system_prompt = self._load_system_prompt()
                clean_response = await self.litert.chat(
                    prompt=text,
                    tools=self._selected_tools(),
                    system_prompt=system_prompt,
                    history=conversation_history[:-1]
                )
                visible_text = self._clean_tool_calls(clean_response)
                # Actualizar historial con texto limpio
                if conversation_history and conversation_history[-1].role == "assistant":
                    conversation_history[-1].content = visible_text
            except Exception as e:
                logger.error(f"Error obteniendo respuesta limpia: {e}")

        # Si el texto visible está vacío pero se ejecutaron tools, generar fallback
        if not visible_text and response_text:
            import re
            supported, tools_used, _ = self._tool_truth()
            tool_calls = re.findall(r'call:(\w+)\{', response_text)
            if tool_calls:
                tool_names = ", ".join(sorted(set(tool_calls)))
                visible_text = f"He ejecutado: {tool_names}."
            elif supported and not tools_used:
                # Sin tool call en el texto y sin ninguna ejecutada: "Acción ejecutada
                # correctamente" era una invención pura.
                visible_text = self._NO_ACTION_MSG
            else:
                visible_text = "Acción ejecutada correctamente."

        # Actualizar historial con texto limpio
        if conversation_history and conversation_history[-1].role == "assistant":
            conversation_history[-1].content = visible_text

        return {
            "status": "success",
            "response_text": visible_text,
            "commands_executed": 1 if "Éxito" in visible_text or "Exito" in visible_text else 0,
            "audio_file": None,
        }

    async def cancel_audio_tasks(self) -> None:
        """Cancela activamente las tareas de audio y síntesis en curso."""
        cancelled = False
        if self._current_tts_task and not self._current_tts_task.done():
            logger.info("Cancelando tarea de síntesis TTS en AssistantService...")
            self._current_tts_task.cancel()
            cancelled = True
            
        if self._current_play_task and not self._current_play_task.done():
            logger.info("Cancelando tarea de reproducción TTS en AssistantService...")
            self._current_play_task.cancel()
            cancelled = True
            
        # Esperar a que terminen si es posible
        if cancelled:
            try:
                tasks = [t for t in (self._current_tts_task, self._current_play_task) if t and not t.done()]
                if tasks:
                    await asyncio.wait(tasks, timeout=2.0)
            except Exception as e:
                logger.warning(f"Error esperando cancelación de tareas de audio: {e}")

    async def wait_for_tts_complete(self) -> None:
        """Espera hasta que la reproducción de voz del TTS actual haya terminado por completo."""
        if self._current_play_task and not self._current_play_task.done():
            try:
                await self._current_play_task
                logger.info("Espera de reproducción de voz completada.")
            except asyncio.CancelledError:
                logger.info("La espera de TTS completo fue cancelada.")
            except Exception as e:
                logger.error(f"Error esperando a que termine el TTS: {e}")

    async def cleanup(self) -> None:
        """Cierra de forma segura todos los recursos del servicio de negocio."""
        logger.info("Iniciando cleanup de AssistantService...")
        await self.cancel_audio_tasks()
        if self.tts:
            self.tts.stop()
            self.tts.close_persistent_stream()

