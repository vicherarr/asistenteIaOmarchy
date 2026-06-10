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
    open_terminal_and_run_command,
    read_terminal_screen,
    control_local_browser,
    send_input_to_terminal,
    interrupt_terminal_command,
    launch_application,
    close_application
)
from src.vision_tool import analyze_screen, analyze_clipboard_image, take_screenshot, get_pending_vision
from src.document_tool import create_document, get_pending_document, build_and_save_document
from src.litert_client import LiteRTClient
from src.tts_engine import TTSEngine
from src.stt_engine import STTEngine
from src.utils import strip_markdown
from src.config import settings
from PIL import Image

logger = logging.getLogger(__name__)


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
        # Lista de herramientas para LiteRT
        self.tools = [
            execute_system_command,
            read_log_file,
            clipboard_manager,
            web_search,
            system_diagnostics,
            read_web_page,
            play_specific_music,
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
        ]
        # Tool de Gmail: solo se registra si Google está habilitado y hay credenciales
        # del usuario (~/.config/asistenteia/google/credentials.json). Si no, ni aparece,
        # y el asistente funciona igual que siempre (retrocompatible).
        self._gmail_enabled = False
        if getattr(settings, "GOOGLE_ENABLED", False):
            try:
                from src.integrations.google.auth import is_configured
                from src.integrations.google.gmail import gmail_manager
                if is_configured():
                    self.tools.append(gmail_manager)
                    self._gmail_enabled = True
                    logger.info("Tool de Gmail registrada (Google configurado).")
                else:
                    logger.info("Gmail no registrado: faltan credenciales de Google.")
            except Exception as e:  # noqa: BLE001 — deps de Google ausentes u otro fallo
                logger.warning(f"No se pudo registrar la tool de Gmail: {e}")
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
            "create_document": "Genera un documento ODT",
            "gmail_manager": "Correo de Gmail (leer/enviar/responder)",
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
             "tools": ["analyze_screen", "analyze_clipboard_image", "take_screenshot"]},
            {"key": "docs", "label": "Documentos y portapapeles", "icon": "📄",
             "tools": ["create_document", "clipboard_manager"]},
            {"key": "musica", "label": "Música", "icon": "🎵",
             "tools": ["play_specific_music"]},
        ]
        if self._gmail_enabled:
            self._tool_groups.append(
                {"key": "correo", "label": "Correo (Gmail)", "icon": "📧",
                 "tools": ["gmail_manager"]})
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
        try:
            while True:
                audio_np = await queue_audio.get()
                if audio_np is None:
                    queue_audio.task_done()
                    logger.info("Worker de reproducción recibió señal de finalización")
                    break
                
                try:
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

    async def _resolve_gmail_confirmation(self, text: str) -> Optional[str]:
        """Si hay una acción de Gmail pendiente, interpreta el sí/no del usuario.

        Devuelve el texto de resultado (a hablar/mostrar) si la confirmación se
        resuelve, o None si no hay nada pendiente o el turno es ambiguo (en cuyo
        caso el flujo normal sigue y la acción pendiente caduca sola en ~2 min).
        """
        if not self._gmail_enabled:
            return None
        from src.integrations.google.gmail import peek_pending, run_pending, clear_pending
        if peek_pending() is None:
            return None
        # Negar tiene prioridad: "no, mejor no" no debe ejecutar nada.
        if self._NO_RE.search(text):
            clear_pending()
            return "De acuerdo, lo dejo."
        if self._YES_RE.search(text):
            return await run_pending()
        return None

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
        return len(t) > 15

    async def process_transcription_stream(
        self,
        text: str,
        conversation_history: list[ChatMessage],
        sink_id: Optional[str] = None,
        max_history: int = 10
    ):
        """
        Procesa el texto del usuario usando LiteRT y herramientas de forma reactiva,
        transmitiendo los chunks resultantes en tiempo real y sintetizando voz frase a frase
        con pipeline de doble cola (síntesis || reproducción en paralelo).
        """
        logger.info(f"Procesando transcripción con LiteRT (streaming): {text[:100]}...")

        # Cancelar TTS previo si existe
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
        if self._current_play_task and not self._current_play_task.done():
            self._current_play_task.cancel()
        self.tts.stop() # Asegurar parada inmediata del proceso
        self.tts._is_playing = True  # Re-activar para nueva síntesis

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

        # Confirmación de acción de correo pendiente: si en el turno anterior la tool
        # de Gmail dejó algo a confirmar (enviar/responder/papelera/archivar), este turno
        # es el sí/no. Se resuelve sin pasar por el modelo y se responde directamente.
        gmail_confirm = await self._resolve_gmail_confirmation(text)
        if gmail_confirm is not None:
            conversation_history.append(ChatMessage(role="assistant", content=gmail_confirm))
            if self._is_speakable(gmail_confirm):
                await queue_text.put(gmail_confirm)
            await queue_text.put(None)  # cerrar el pipeline de síntesis
            yield gmail_confirm
            return

        # Cargar system prompt desde archivo
        prompt_path = settings.PROJECT_ROOT / "config" / "system_prompt.txt"
        try:
            system_prompt = prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Error cargando system prompt: {e}")
            system_prompt = "Eres un asistente de voz para Linux llamado AsistenteIA."
        # Modo enfocado: si hay tools seleccionadas, instruir al modelo a usar solo esas.
        system_prompt = self._focused_system_prompt(system_prompt)

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
                        for s in sentences:
                            clean = strip_markdown(s)
                            if self._is_speakable(clean):
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
            logger.info(
                "TTS markers crudos: think=%s channel=%s tool=%s | head=%r",
                "<think>" in accumulated_text,
                ("<|channel>" in accumulated_text or "<channel|>" in accumulated_text),
                ("call:" in accumulated_text or "<|tool_call>" in accumulated_text),
                accumulated_text[:300],
            )

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
                
                # Detectar si el prompt involucra terminal/comandos
                terminal_indicators = [
                    "abre", "abrir", "lanza", "ejecuta", "terminal", "comando", "consola",
                    "dime", "qué", "cuál", "característica", "info", "información",
                    "versión", "version", "instala", "instalada", "instalado",
                    "java", "node", "python", "dotnet", "ruby", "go", "rust",
                    "php", "docker", "git", "npm", "cargo", "pip"
                ]
                is_terminal = any(kw in lower_text for kw in terminal_indicators)
                
                if is_terminal:
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
                elif any(kw in lower_text for kw in ["música", "canción", "play", "spotify", "pon"]):
                    fallback = "Reproduciendo música."
                elif any(kw in lower_text for kw in ["pantalla", "captura", "ver", "mira"]):
                    fallback = "Analizando la pantalla."
                elif any(kw in lower_text for kw in ["busca", "buscar", "investiga", "web"]):
                    fallback = "Búsqueda web realizada."
                elif any(kw in lower_text for kw in ["copia", "pegar", "portapapeles"]):
                    fallback = "Portapapeles actualizado."
                elif any(kw in lower_text for kw in ["ventana", "cerrar", "enfocar", "workspace"]):
                    fallback = "Ventanas gestionadas."
                else:
                    fallback = "Acción ejecutada correctamente."
                
                accumulated_text = fallback
                yield fallback
                await queue_text.put(fallback)

            # Encolar lo que quede en el buffer. Si usamos fallback, el residuo es basura
            # de la tool call fugada (el fallback ya es la respuesta hablada): descartarlo.
            if sentence_buffer.strip() and not used_fallback:
                clean = strip_markdown(sentence_buffer)
                if self._is_speakable(clean):
                    await queue_text.put(clean)

            # Guardar respuesta final acumulada en el historial
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
                prompt_path = settings.PROJECT_ROOT / "config" / "system_prompt.txt"
                system_prompt = self._focused_system_prompt(prompt_path.read_text(encoding="utf-8"))
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
            tool_calls = re.findall(r'call:(\w+)\{', response_text)
            if tool_calls:
                tool_names = ", ".join(sorted(set(tool_calls)))
                visible_text = f"He ejecutado: {tool_names}."
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

