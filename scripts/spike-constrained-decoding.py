#!/usr/bin/env python3
"""Mide si `enable_constrained_decoding` arregla los fallos de parseo de tool calls.

Contexto: el motor LiteRT falla a veces al parsear la llamada que emite el modelo
("INVALID_ARGUMENT: Failed to parse FC tool calls"), casi siempre con herramientas de
VARIOS argumentos. Cuando pasa, el cliente reintenta sin herramientas y el modelo se
queda incapaz de actuar (fue la causa del "te he abierto YouTube" sin abrir nada).

`create_conversation` acepta `enable_constrained_decoding` (por defecto False) y nunca
se ha usado. En teoría restringe la generación a la gramática de tool calls, así que
debería eliminar esos fallos. Este spike lo comprueba fuera del servicio antes de
tocar la configuración desplegada.

Qué mide, para OFF y para ON:
  - nº de llamadas que fallan al parsear
  - nº de llamadas en que el modelo eligió la tool correcta
  - latencia media hasta terminar la respuesta

RESULTADO MEDIDO (2026-08-07, Gemma E4B en GPU, n=10):

                              OFF         ON
    parse errors             0/10       0/10
    llamó a la tool          8/10       8/10
    latencia media (s)       1.35       1.87

CONCLUSIÓN: se queda en False. No se pudo provocar ni un fallo de parseo en 10
intentos (es esporádico: se ve del orden de una vez al día de uso real), pero el
coste de latencia es constante y grande: +38%, consistente prompt a prompt. En un
asistente de voz medio segundo en CADA turno se nota siempre, mientras que el fallo
que evitaría es raro y ya está cubierto: litert_client reintenta con herramientas y,
si aun así se queda sin ellas, el guardarraíl de assistant_service impide que el
modelo afirme algo que no hizo. Cambiar latencia segura por un fallo raro y ya
mitigado no sale a cuenta.

Merecería la pena reabrirlo si los parse errors se volvieran frecuentes (varios al
día) o si una versión futura de litert_lm abarata la decodificación restringida.

Uso:
    venv/bin/python scripts/spike-constrained-decoding.py           # 10 iteraciones
    venv/bin/python scripts/spike-constrained-decoding.py -n 20

Ojo: carga el modelo en GPU, así que conviene parar el servicio antes
(`systemctl --user stop asistenteia.service`) para no competir por la VRAM.
"""

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import litert_lm  # noqa: E402

from src.config import settings  # noqa: E402
from src.litert_client import LiteRTClient  # noqa: E402

# Prompts que en producción llevan al modelo a una tool de varios argumentos, que es
# donde revienta la gramática.
PROMPTS = [
    "pon música de Iron Maiden en YouTube",
    "abre la página de YouTube y busca Madonna",
    "navega a github.com y dime qué ves",
    "escribe 'hola' en el buscador de la web",
    "traduce esta página al español",
]


def herramienta_multiarg(action: str, target: str = "", value: str = "") -> str:
    """Controla el navegador web: navegar, clic, escribir, leer.

    Args:
        action: launch, navigate, click, type o read.
        target: URL o selector CSS según la acción.
        value: texto u otro parámetro según la acción.
    """
    return f"OK action={action} target={target} value={value}"


def una_ronda(engine, prompt: str, constrained: bool) -> tuple[bool, bool, float]:
    """Devuelve (parse_error, llamó_a_la_tool, segundos)."""
    llamadas: list[str] = []

    class Handler(litert_lm.ToolEventHandler):
        def approve_tool_call(self, tool_call: dict) -> bool:
            llamadas.append((tool_call or {}).get("name", "?"))
            return True

        def process_tool_response(self, tool_response: dict) -> dict:
            return tool_response

    kwargs = {
        "messages": [],
        "tools": [herramienta_multiarg],
        "tool_event_handler": Handler(),
    }
    if constrained:
        kwargs["enable_constrained_decoding"] = True

    inicio = time.monotonic()
    try:
        with engine.create_conversation(**kwargs) as conv:
            for _ in conv.send_message_async(
                {"role": "user", "content": [{"type": "text", "text": prompt}]}
            ):
                pass
    except Exception as e:
        err = str(e)
        elapsed = time.monotonic() - inicio
        if "Failed to parse tool calls" in err or "INVALID_ARGUMENT" in err:
            return True, False, elapsed
        print(f"    error inesperado: {err[:160]}")
        return False, False, elapsed

    return False, bool(llamadas), time.monotonic() - inicio


def medir(engine, iteraciones: int, constrained: bool) -> dict:
    etiqueta = "ON " if constrained else "OFF"
    parse_errors = 0
    con_tool = 0
    latencias = []

    print(f"\n--- constrained_decoding {etiqueta} ({iteraciones} iteraciones) ---")
    for i in range(iteraciones):
        prompt = PROMPTS[i % len(PROMPTS)]
        error, llamo, segundos = una_ronda(engine, prompt, constrained)
        latencias.append(segundos)
        parse_errors += int(error)
        con_tool += int(llamo)
        estado = "PARSE ERROR" if error else ("tool ok" if llamo else "sin tool")
        print(f"  {i+1:2}/{iteraciones}  {segundos:5.2f}s  {estado:12}  {prompt[:40]}")

    return {
        "parse_errors": parse_errors,
        "con_tool": con_tool,
        "latencia_media": statistics.mean(latencias) if latencias else 0.0,
        "iteraciones": iteraciones,
    }


def servicio_activo() -> bool:
    """¿Está el servicio corriendo? Si lo está, tiene el modelo en VRAM y no cabe otro."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", "asistenteia.service"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() == "active"
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--iteraciones", type=int, default=10)
    parser.add_argument("--igualmente", action="store_true",
                        help="Ejecuta aunque el servicio esté activo (probablemente falle por VRAM).")
    args = parser.parse_args()

    if servicio_activo() and not args.igualmente:
        print("El servicio asistenteia está ACTIVO y tiene el modelo cargado en la GPU.")
        print("No hay VRAM para una segunda copia. Párualo, ejecuta el spike y arráncalo:")
        print()
        print("  systemctl --user stop asistenteia.service")
        print(f"  venv/bin/python scripts/spike-constrained-decoding.py -n {args.iteraciones}")
        print("  systemctl --user start asistenteia.service")
        print()
        print("(o pasa --igualmente para intentarlo de todos modos)")
        return 1

    modelo = Path(settings.LITERT_MODEL_PATH)
    if not modelo.is_absolute():
        modelo = settings.PROJECT_ROOT / modelo
    if not modelo.exists():
        print(f"No encuentro el modelo en {modelo}")
        return 1

    # Se carga con LiteRTClient en vez de construir el Engine a mano: él ya aplica la
    # estrategia real de backend (GPU para el LLM, CPU para visión/audio) y las
    # optimizaciones del .env. Duplicar eso aquí mediría un motor distinto al de
    # producción, que es justo lo que no queremos.
    print(f"Cargando {modelo.name} (backend {settings.LITERT_BACKEND})...")
    client = LiteRTClient()
    if not client.engine:
        print("El motor no se pudo cargar. ¿Está el servicio corriendo y ocupando la VRAM?")
        print("Párualo con:  systemctl --user stop asistenteia.service")
        return 1
    engine = client.engine

    off = medir(engine, args.iteraciones, constrained=False)
    on = medir(engine, args.iteraciones, constrained=True)

    n = args.iteraciones
    print("\n================ RESULTADO ================")
    print(f"{'':22} {'OFF':>10} {'ON':>10}")
    print(f"{'parse errors':22} {off['parse_errors']:>7}/{n} {on['parse_errors']:>7}/{n}")
    print(f"{'llamó a la tool':22} {off['con_tool']:>7}/{n} {on['con_tool']:>7}/{n}")
    print(f"{'latencia media (s)':22} {off['latencia_media']:>10.2f} {on['latencia_media']:>10.2f}")

    mejora_parseo = on["parse_errors"] < off["parse_errors"]
    penaliza = on["latencia_media"] > off["latencia_media"] * 1.25
    print()
    if mejora_parseo and not penaliza:
        print("VEREDICTO: merece la pena → LITERT_CONSTRAINED_DECODING=True en el .env")
    elif mejora_parseo and penaliza:
        print("VEREDICTO: arregla el parseo pero cuesta >25% de latencia. Decide tú.")
    elif off["parse_errors"] == 0 and on["parse_errors"] == 0:
        print("VEREDICTO: sin fallos en ninguno de los dos; sube -n para provocarlos.")
    else:
        print("VEREDICTO: no mejora → dejarlo en False.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
