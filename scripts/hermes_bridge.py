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
import sys
import threading

_OUT_LOCK = threading.Lock()   # los callbacks de Hermes disparan desde su hilo worker


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

    def _on_tool_start(*a) -> None:
        # Firma real: (call_id, tool_name, args). Es la VERDAD DE CAMPO del turno:
        # assistant_service la usa para no dejar que el modelo afirme acciones que no
        # ejecutó, así que se registra en cuanto arranca la herramienta.
        name = a[1] if len(a) > 1 else None
        if name:
            tools_this_turn.append(str(name))
            _emit({"tool": str(name)})

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
            stream_delta_callback=lambda t: t and _emit({"delta": t}),
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
        try:
            res = agent.run_conversation(
                user_message=req.get("prompt", ""),
                system_message=req.get("system_prompt") or None,
                conversation_history=req.get("history") or None,
                task_id=req.get("task_id") or None,
            )
            final = str(res.get("final_response") or "")
            # Cinturón y tirantes: si por lo que sea no llegó ningún callback, se
            # reconstruye la lista recorriendo los mensajes del turno.
            tools = list(tools_this_turn) or _tools_from_messages(res.get("messages"))
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
