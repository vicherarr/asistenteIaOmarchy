"""Puente hacia Hermes Agent, en un subproceso con el venv de Hermes.

Hermes no se puede importar dentro del venv de AsistenteIA: fija sus dependencias a
versión exacta (openai, pydantic, fastapi, uvicorn, httpx) y chocarían con las de Luka
(litert-lm, PySide6, faster-whisper, ctranslate2, kokoro), además del ajuste de
*_NUM_THREADS que src/main.py hace antes de cualquier import para evitar segfaults.
Tampoco publica wheel: se instala clonando el repo. Así que vive aparte, como TabbyAPI,
y se le habla por tuberías — el mismo patrón que src/stt_worker.py.

Protocolo (líneas JSON sobre stdin/stdout). A diferencia del worker de STT, una
petición produce VARIAS líneas de respuesta, porque el turno se emite en streaming:

  Al arrancar:  {"ready": true}
  Petición:     {"prompt": "...", "system_prompt": "...", "history": [...],
                 "task_id": "..."}                  | {"reset": true}
  Respuesta:    {"delta": "texto"}       (0..N, el texto que se hablará)
                {"tool": "nombre"}       (0..N, en cuanto ARRANCA cada herramienta)
                {"done": true, "final": "...", "tools": [...], "error": null}

El razonamiento NO se emite: llega por reasoning_callback y se descarta aquí, que es
lo que queremos en un asistente de voz (y evita que el TTS lea el monólogo interno).
Todo el logging va a stderr para no contaminar stdout, que es el canal del protocolo.
"""

import argparse
import json
import os
import re
import sys
import threading

_OUT_LOCK = threading.Lock()   # los callbacks de Hermes disparan desde su hilo worker

# Meta-herramientas del puente `tool_search` de Hermes. NO son trabajo real: buscan en el
# catálogo, piden un esquema o envuelven la llamada de verdad. Si cuentan como "tools
# ejecutadas", el guardarraíl anti-invención de assistant_service da por buena cualquier
# afirmación del modelo — y el 23/08/2026 pasó exactamente eso: ocho llamadas al puente,
# cero herramientas reales, y las frases de "ya está hecho" salieron por el altavoz.
# Con tool_search apagado no deberían aparecer; esto es el cinturón por si vuelven.
_META_TOOLS = frozenset({"tool_search", "tool_describe", "tool_call"})

# Marcado de llamada a herramienta que algunos modelos emiten como TEXTO en vez de en el
# campo `tool_calls`. Cuando eso pasa, Hermes no lo parsea, va al `content`, y de ahí al
# stream de deltas: lo lee el TTS en alto. Medido con deepseek-v3.2, que suelta su DSML
# nativo —ojo, la barra es U+FF5C (｜), no la ASCII— y además anidado sobre sí mismo.
#
# La lista es deliberadamente amplia: son formatos de varias familias de modelos y el
# coste de un falso positivo es bajo (se calla un fragmento que era marcado igualmente),
# mientras que el de un falso negativo es que se pronuncie basura.
_APERTURAS = (
    "<｜DSML｜",          # DeepSeek (barra fullwidth)
    "<｜tool▁calls",     # DeepSeek, tokens con ▁
    "<|tool_call",       # LiteRT / genérico
    "<|python_tag",      # Llama
    "<tool_call>",       # Qwen
    "<function_calls>",  # estilo Anthropic
    "<invoke ",
    "<parameter ",
)
# El prefijo más largo que puede quedar partido entre dos deltas. Se retiene esa cola
# hasta ver el siguiente trozo, o un marcado a caballo entre chunks se colaría entero.
_MAX_APERTURA = max(len(a) for a in _APERTURAS)

# Cierres que devuelven el stream a texto normal. Solo marcadores INEQUÍVOCOS: un ">' a
# secas cerraría en cuanto acaba la etiqueta de apertura y dejaría suelto el cuerpo de la
# llamada (medido: '<tool_call>{"a":1}</tool_call>' soltaba el JSON entero por el TTS).
# Si no llega ninguno de estos, se calla hasta el final del turno, que es lo correcto: un
# modelo que ha empezado a escupir marcado crudo en el content ya no está hablando.
#
# '</invoke>' NO está: va anidado dentro de '<function_calls>', así que cerrar con él
# devolvería el stream a "hablable" estando todavía dentro del bloque exterior, y el
# '</function_calls>' de después saldría por el altavoz. Sin nivel de anidamiento: si
# aparece suelto, su '<invoke ' ya habrá silenciado hasta el final del turno, que es el
# lado seguro por el que equivocarse.
_CIERRES = ("</function_calls>", "</tool_call>", "<|tool_call|>")
_MAX_CIERRE = max(len(c) for c in _CIERRES)


class _Saneador:
    """Filtra marcado de tool call de un stream de deltas, con estado entre trozos.

    `HermesEngine.streams_clean_text` promete a assistant_service que lo que sale de aquí
    es texto hablable, sin llamadas ni marcadores. El puente no podía cumplirlo: reenvía
    lo que el proveedor ponga en `content`. Esta clase es donde se cumple, que es el sitio
    correcto — assistant_service tendría que conocer el marcado de cada familia de modelo.
    """

    def __init__(self) -> None:
        self._pendiente = ""      # cola retenida por si es un marcador partido
        self._silenciado = False  # dentro de marcado: se tira todo hasta el cierre

    def __call__(self, delta: str) -> str:
        """Devuelve la parte hablable de `delta` (puede ser cadena vacía)."""
        buf = self._pendiente + (delta or "")
        self._pendiente = ""
        salida = []

        while buf:
            if self._silenciado:
                corte = self._buscar_cierre(buf)
                if corte is None:
                    # Sigue dentro del marcado: se tira, PERO se retiene la última cola
                    # por si el cierre viene partido entre deltas. Sin esto, con el
                    # stream llegando carácter a carácter el cierre no se encontraba
                    # nunca y se comía el resto del turno (medido con el formato Qwen).
                    n = min(len(buf), _MAX_CIERRE - 1)
                    self._pendiente = buf[-n:] if n else ""
                    return "".join(salida)
                buf = buf[corte:]
                self._silenciado = False
                continue

            pos = self._primera_apertura(buf)
            if pos is None:
                # Sin marcador entero. Se retiene SOLO el sufijo que podría ser el
                # principio de uno partido entre dos deltas; el resto sale ya.
                n = self._cola_a_retener(buf)
                if n:
                    salida.append(buf[:-n])
                    self._pendiente = buf[-n:]
                else:
                    salida.append(buf)
                break

            pos, largo = pos
            # Se salta la etiqueta de apertura ANTES de buscar el cierre. Si no, un
            # delimitador que hace de las dos cosas —'<|tool_call|>' de LiteRT abre y
            # cierra— se cerraría a sí mismo al instante y el cuerpo de la llamada
            # saldría hablado (medido: soltaba 'call:x{}').
            salida.append(buf[:pos])
            buf = buf[pos + largo:]
            self._silenciado = True

        return "".join(salida)

    def vaciar(self) -> str:
        """Suelta lo retenido al acabar el turno. Si quedó marcado a medias, se descarta."""
        resto = "" if self._silenciado else self._pendiente
        self._pendiente = ""
        self._silenciado = False
        return resto

    @staticmethod
    def _primera_apertura(texto: str):
        """(posición, longitud) de la primera apertura, o None.

        Con empate en la posición gana la MÁS LARGA, para saltarse la etiqueta entera.
        """
        encontradas = [(texto.find(a), len(a)) for a in _APERTURAS]
        encontradas = [(p, n) for p, n in encontradas if p >= 0]
        if not encontradas:
            return None
        primera = min(p for p, _ in encontradas)
        return primera, max(n for p, n in encontradas if p == primera)

    @staticmethod
    def _buscar_cierre(texto: str):
        """Índice justo tras el primer cierre, o None si aún no ha llegado."""
        posiciones = [texto.find(c) + len(c) for c in _CIERRES if c in texto]
        return min(posiciones) if posiciones else None

    @staticmethod
    def _cola_a_retener(texto: str) -> int:
        """Cuántos caracteres del final hay que retener por si son un marcador partido.

        Es el sufijo MÁS LARGO de `texto` que sea prefijo estricto de alguna apertura.
        Comprobar la cola entera contra las aperturas no vale: '<｜DS' precedido de texto
        ("Hola <｜DS") no empieza por ningún marcador, así que se soltaba todo y el
        marcador se colaba al llegar el resto en el delta siguiente.
        """
        for n in range(min(len(texto), _MAX_APERTURA), 0, -1):
            cola = texto[-n:]
            if any(a.startswith(cola) and n < len(a) for a in _APERTURAS):
                return n
        return 0


def _clasificar_tool(name: str, args: dict):
    """(herramienta_real, meta_usada) para una llamada que arranca.

    `herramienta_real` es lo que cuenta como VERDAD DE CAMPO para el guardarraíl
    anti-invención de assistant_service; None si no se ejecutó nada de verdad.
    `meta_usada` es el nombre del puente si se pasó por él, para poder avisar.

    `tool_call` es el caso con miga: sí ejecuta una herramienta real, pero envuelta —el
    nombre de verdad viene en el argumento `name`—, así que se desenvuelve para no perder
    la ejecución. `tool_search` y `tool_describe` solo consultan el catálogo: no son
    trabajo, y contarlas como tal es lo que dejó pasar las frases de "ya está hecho" el
    23/08/2026 sin que se hubiera tocado nada.
    """
    name = (name or "").strip()
    if not name:
        return None, None
    if name not in _META_TOOLS:
        return name, None
    if name != "tool_call":
        return None, name
    interno = str((args or {}).get("name") or "").strip()
    if not interno or interno in _META_TOOLS:
        return None, name
    return interno, name


def _limpiar_final(texto: str) -> str:
    """Quita marcado del texto final del turno (el que va al historial y a la UI).

    El final llega de una vez, no por trozos, así que basta una pasada de regex. Va al
    historial de conversación: si la basura entra ahí, el turno siguiente la imita.
    """
    if not texto:
        return texto
    for apertura in _APERTURAS:
        if apertura in texto:
            texto = texto.split(apertura, 1)[0]
    # Repaso para tokens sueltos que quedaran detrás. Se EXIGE la barra (| o ｜, esta
    # última U+FF5C): sin ella la regex se comía texto legítimo entre ángulos, y en un
    # asistente que habla lo que escribe eso es peor que dejar pasar un token raro.
    return re.sub(r"<[|｜][a-zA-Z_▁]+[|｜]?>", "", texto).strip()


def _emit(obj: dict) -> None:
    """Escribe una línea JSON en stdout. Con lock: los callbacks vienen de otro hilo."""
    with _OUT_LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _log(msg: str) -> None:
    sys.stderr.write(f"hermes_bridge: {msg}\n")
    sys.stderr.flush()


def _split_csv(value: str):
    items = [v.strip() for v in (value or "").split(",") if v.strip()]
    return items or None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hermes-dir", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--api-key", default="x")
    p.add_argument("--model", required=True)
    p.add_argument("--max-iterations", type=int, default=8)
    # Generoso a propósito: los modelos "thinking" emiten TODO el razonamiento antes de
    # la respuesta, así que con poco presupuesto el content sale vacío (medido: 655
    # caracteres de razonamiento para calcular 3+4).
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--enabled-toolsets", default="")
    p.add_argument("--disabled-toolsets", default="")
    p.add_argument("--fallback-models", default="")
    p.add_argument("--skip-memory", action="store_true")
    args = p.parse_args()

    # Hermes se importa desde su propio árbol y espera correr con ese cwd.
    sys.path.insert(0, args.hermes_dir)
    os.chdir(args.hermes_dir)

    from run_agent import AIAgent  # noqa: E402 — depende del sys.path de arriba

    _register_mcp_servers()

    tools_this_turn: list[str] = []
    meta_this_turn: list[str] = []
    saneador = _Saneador()

    def _on_tool_start(*a) -> None:
        # Firma real: (call_id, tool_name, args). Es la VERDAD DE CAMPO del turno:
        # assistant_service la usa para no dejar que el modelo afirme acciones que no
        # ejecutó, así que se registra en cuanto arranca la herramienta.
        name = str(a[1]) if len(a) > 1 and a[1] else ""
        args = a[2] if len(a) > 2 and isinstance(a[2], dict) else {}
        real, meta = _clasificar_tool(name, args)
        if meta:
            meta_this_turn.append(meta)
        if real:
            tools_this_turn.append(real)
            _emit({"tool": real})

    def _on_delta(t: str) -> None:
        limpio = saneador(t)
        if limpio:
            _emit({"delta": limpio})

    def _build() -> "AIAgent":
        fb_entries = []
        if args.fallback_models:
            for fb in _split_csv(args.fallback_models):
                if fb:
                    fb_entries.append({
                        "provider": "custom",
                        "model": fb,
                        "base_url": args.base_url,
                        "api_key": args.api_key or "x",
                        "api_mode": "chat_completions",
                    })

        return AIAgent(
            base_url=args.base_url,
            api_key=args.api_key or "x",
            provider="custom",
            api_mode="chat_completions",
            model=args.model,
            fallback_model=fb_entries or None,
            quiet_mode=True,          # imprescindible: stdout es el canal del protocolo
            skip_context_files=True,  # nada de AGENTS.md/CLAUDE.md del directorio
            skip_memory=args.skip_memory,
            load_soul_identity=False,
            save_trajectories=False,
            max_iterations=args.max_iterations,
            max_tokens=args.max_tokens,
            enabled_toolsets=_split_csv(args.enabled_toolsets),
            disabled_toolsets=_split_csv(args.disabled_toolsets),
            stream_delta_callback=_on_delta,
            tool_start_callback=_on_tool_start,
            # reasoning_callback se deja SIN conectar: el razonamiento no debe hablarse.
        )

    agent = _build()
    _log(f"listo (modelo {args.model} en {args.base_url})")
    _emit({"ready": True})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _emit({"done": True, "final": "", "tools": [], "error": f"JSON inválido: {e}"})
            continue

        if req.get("reset"):
            # Instancia nueva: los docs de Hermes piden una por tarea/hilo para que no
            # se contamine el estado entre conversaciones.
            agent = _build()
            _emit({"done": True, "final": "", "tools": [], "error": None})
            continue

        tools_this_turn.clear()
        meta_this_turn.clear()
        saneador = _Saneador()
        try:
            res = agent.run_conversation(
                user_message=req.get("prompt", ""),
                system_message=req.get("system_prompt") or None,
                conversation_history=req.get("history") or None,
                task_id=req.get("task_id") or None,
            )
            # Lo que quedó retenido en el saneador: si era marcado a medias, se descarta.
            cola = saneador.vaciar()
            if cola:
                _emit({"delta": cola})
            final = _limpiar_final(str(res.get("final_response") or ""))
            # Cinturón y tirantes: si por lo que sea no llegó ningún callback, se
            # reconstruye la lista recorriendo los mensajes del turno.
            tools = list(tools_this_turn) or _tools_from_messages(res.get("messages"))

            # Turno que solo dio vueltas por el puente: ninguna herramienta real. No
            # lanza excepción —Hermes devuelve su resumen tan tranquilo—, así que hay
            # que detectarlo aquí.
            #
            # Devolver `tools` vacío YA es la mitad del arreglo: el guardarraíl
            # anti-invención de assistant_service ve que no corrió nada y sustituye las
            # frases de "ya está hecho" por su propio aviso. Solo se marca error cuando
            # además no quedó texto aprovechable, que es el turno roto del todo: sin
            # esto Luka se quedaría muda sin explicar por qué.
            if meta_this_turn and not tools:
                _log(f"turno sin herramientas reales: solo {sorted(set(meta_this_turn))}")
                if not final:
                    _emit({"done": True, "final": "", "tools": [],
                           "error": "el modelo no llegó a ejecutar ninguna herramienta"})
                    continue

            _emit({"done": True, "final": final, "tools": tools, "error": None})
        except Exception as e:  # noqa: BLE001 — cualquier fallo debe volver como respuesta
            _log(f"error en el turno: {type(e).__name__}: {e}")
            _emit({"done": True, "final": "", "tools": list(tools_this_turn),
                   "error": f"{type(e).__name__}: {e}"})


def _register_mcp_servers() -> None:
    """Conecta los servidores MCP de ~/.hermes/config.yaml y registra sus herramientas.

    Hay que pedirlo explícitamente: construir un AIAgent NO lo hace. Es la CLI (y el
    adaptador ACP) quien llama a register_mcp_servers al arrancar, así que un agente
    embebido como este se queda sin ellas si no lo replica. Sin esto, Hermes busca
    'music_control' con tool_search, no la encuentra, y responde que no tiene acceso.

    Aquí es donde entran las herramientas de Luka (src/mcp_server.py).
    """
    try:
        from tools.mcp_tool import _load_mcp_config, register_mcp_servers
    except ImportError as e:
        # El soporte MCP de Hermes es un extra opcional ('uv sync --extra mcp').
        _log(f"sin soporte MCP ({e}); Hermes irá solo con sus herramientas")
        return
    try:
        servers = _load_mcp_config()
        if not servers:
            _log("no hay servidores MCP configurados")
            return
        names = register_mcp_servers(servers)
        _log(f"MCP: {len(names)} herramientas de {sorted(servers)} -> {sorted(names)[:12]}")
    except Exception as e:  # noqa: BLE001 — un MCP caído no debe impedir el turno
        _log(f"no se pudieron registrar los servidores MCP: {type(e).__name__}: {e}")


def _tools_from_messages(messages) -> list[str]:
    """Nombres de las tools ejecutadas, leídos del historial que devuelve Hermes."""
    names: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            if fn.get("name"):
                names.append(str(fn["name"]))
    return names


if __name__ == "__main__":
    main()
