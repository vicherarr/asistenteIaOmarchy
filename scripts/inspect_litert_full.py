"""Fase 0 — Introspección del API real de litert_lm (sin cargar el modelo).

Vuelca firmas y miembros de las clases que aún no explotamos: SamplerConfig,
Session, Conversation, Responses, Tool, ToolEventHandler, tool_from_function,
Benchmark, y los submódulos tools / interfaces. No instancia Engine.
"""
import inspect
import litert_lm


def show(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def sig(obj, name=None):
    name = name or getattr(obj, "__name__", str(obj))
    try:
        print(f"{name}{inspect.signature(obj)}")
    except (ValueError, TypeError) as e:
        print(f"{name}: (sin firma legible: {e})")
    doc = inspect.getdoc(obj)
    if doc:
        print("  doc:", doc.splitlines()[0][:200])


def dump_members(obj, skip_dunder=True):
    for n, m in inspect.getmembers(obj):
        if skip_dunder and n.startswith("__"):
            continue
        kind = type(m).__name__
        try:
            s = str(inspect.signature(m))
        except (ValueError, TypeError):
            s = ""
        print(f"  {n}{s}  [{kind}]")
        d = inspect.getdoc(m)
        if d:
            print("      ·", d.splitlines()[0][:160])


show("Engine"); sig(litert_lm.Engine)

show("Backend (enum)")
for n in dir(litert_lm.Backend):
    if not n.startswith("__"):
        print("  Backend." + n)

show("LogSeverity (enum)")
for n in dir(litert_lm.LogSeverity):
    if not n.startswith("__"):
        print("  LogSeverity." + n)

show("SamplerConfig"); sig(litert_lm.SamplerConfig); dump_members(litert_lm.SamplerConfig)

show("Conversation (métodos)"); dump_members(litert_lm.Conversation)
show("Session (métodos)"); dump_members(litert_lm.Session)
show("Responses"); sig(litert_lm.Responses); dump_members(litert_lm.Responses)

show("Tool"); dump_members(litert_lm.Tool)
show("ToolEventHandler"); dump_members(litert_lm.ToolEventHandler)
show("tool_from_function"); sig(litert_lm.tool_from_function)

show("Benchmark"); sig(litert_lm.Benchmark)
show("BenchmarkInfo"); dump_members(litert_lm.BenchmarkInfo)

show("submódulo litert_lm.tools")
for n in dir(litert_lm.tools):
    if not n.startswith("__"):
        obj = getattr(litert_lm.tools, n)
        try:
            s = str(inspect.signature(obj))
        except (ValueError, TypeError):
            s = ""
        print(f"  tools.{n}{s}  [{type(obj).__name__}]")

show("submódulo litert_lm.interfaces")
for n in dir(litert_lm.interfaces):
    if not n.startswith("__"):
        obj = getattr(litert_lm.interfaces, n)
        try:
            s = str(inspect.signature(obj))
        except (ValueError, TypeError):
            s = ""
        print(f"  interfaces.{n}{s}  [{type(obj).__name__}]")

show("create_conversation (firma desde la clase Engine interna)")
# La firma de create_conversation suele estar en el objeto Engine instanciado;
# intentamos sacarla del tipo _Engine sin instanciar.
for cand in ("_Engine", "AbstractEngine"):
    cls = getattr(litert_lm, cand, None)
    if cls is None:
        continue
    print(f"-- {cand} --")
    for n, m in inspect.getmembers(cls):
        if n.startswith("__"):
            continue
        try:
            s = str(inspect.signature(m))
        except (ValueError, TypeError):
            s = ""
        print(f"  {n}{s}")

print("\n[fin]")
