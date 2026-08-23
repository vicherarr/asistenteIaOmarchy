"""Tests del puente hacia Hermes Agent (scripts/hermes_bridge.py).

Se carga por ruta porque es un script suelto, no un módulo del paquete: vive fuera de
`src/` a propósito, ya que se ejecuta con el venv de Hermes, no con el de Luka. Importarlo
así no arranca nada: `main()` solo corre bajo `if __name__ == "__main__"`.
"""

import importlib.util
import random
from pathlib import Path

import pytest

_RUTA = Path(__file__).resolve().parent.parent / "scripts" / "hermes_bridge.py"
_spec = importlib.util.spec_from_file_location("hermes_bridge", _RUTA)
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)


def sanear(trozos):
    """Pasa los trozos por el saneador y devuelve el texto hablable resultante."""
    s = hb._Saneador()
    return "".join(s(t) for t in trozos) + s.vaciar()


# --- El saneador de deltas -----------------------------------------------------------
# `HermesEngine.streams_clean_text = True` promete a assistant_service que el stream ya
# viene sin marcado de tool call. El puente no podía cumplirlo —reenvía lo que el
# proveedor ponga en `content`— y el 23/08/2026 el marcado nativo de deepseek-v3.2 llegó
# hasta Kokoro y se pronunció en alto. Aquí es donde se cumple la promesa.

# El caso REAL del log de aquel día, tal cual: DSML anidado dentro de un `arguments`.
_CASO_REAL = ('<｜DSML｜function_calls>\n<｜DSML｜invoke name="tool_call">\n'
              '<｜DSML｜parameter name="arguments" string="false">\t')


@pytest.mark.parametrize("nombre,entrada,esperado", [
    ("caso real del log",      _CASO_REAL,                                            ""),
    ("texto y marcado pegado", 'Déjame usar el tool directamente<｜DSML｜functioncalls>',
                               'Déjame usar el tool directamente'),
    ("qwen con cierre",        'Listo. <tool_call>{"a":1}</tool_call> Ya está.', 'Listo.  Ya está.'),
    ("litert abre y cierra",   'Vale. <|tool_call|>call:x{}<|tool_call|> Hecho.', 'Vale.  Hecho.'),
    ("anthropic anidado",      'Ok. <function_calls><invoke name="a"></invoke></function_calls> Fin.',
                               'Ok.  Fin.'),
])
def test_quita_el_marcado_de_tool_call(nombre, entrada, esperado):
    assert sanear([entrada]) == esperado


@pytest.mark.parametrize("texto", [
    "He creado el script en tu carpeta.",
    "2 < 3 y 5 > 4",                       # un '<' suelto no es un marcador
    "Tienes 3 correos sin leer.",
    "",
])
def test_no_toca_el_texto_hablable(texto):
    """Un falso positivo aquí deja a Luka muda a media frase."""
    assert sanear([texto]) == texto
    assert sanear(list(texto)) == texto    # y troceado igual


def test_marcador_partido_entre_dos_deltas():
    """El stream llega a trozos: un filtro sin estado dejaría pasar esto.

    Retener la cola entera no vale — '<｜DS' precedido de texto no empieza por ningún
    marcador—; hay que buscar el sufijo más largo que sea prefijo de una apertura.
    """
    assert sanear(['Hola <｜DS', 'ML｜function_calls>basura']) == 'Hola '


def test_cierre_partido_entre_deltas():
    """Con el stream llegando carácter a carácter, el cierre también se parte.

    Antes se descartaba el buffer entero mientras se estaba silenciado, así que el cierre
    no se encontraba nunca y se comía el resto del turno.
    """
    assert sanear(list('Listo. <tool_call>{"a":1}</tool_call> Ya.')) == 'Listo.  Ya.'


def test_marcado_sin_cerrar_calla_hasta_el_final():
    """Un modelo que escupe marcado crudo en el content ya no está hablando."""
    assert sanear(list('Texto <｜DSML｜inv')) == 'Texto '


def test_el_troceado_no_cambia_el_resultado():
    """Invariante: el resultado no puede depender de por dónde parta el proveedor.

    Es la propiedad que de verdad importa — los casos concretos de arriba son ejemplos,
    esto cubre las combinaciones que no se me ocurrieron.
    """
    random.seed(7)
    for entero in [_CASO_REAL,
                   'Listo. <tool_call>{"a":1}</tool_call> Ya está.',
                   'Vale. <|tool_call|>call:x{}<|tool_call|> Hecho.',
                   'Ok. <function_calls><invoke name="a"></invoke></function_calls> Fin.',
                   'He creado el script.', '2 < 3 y 5 > 4']:
        referencia = sanear([entero])
        for _ in range(60):
            cortes = sorted(random.sample(range(len(entero) + 1), k=min(5, len(entero))))
            piezas, prev = [], 0
            for c in [*cortes, len(entero)]:
                piezas.append(entero[prev:c])
                prev = c
            assert sanear(piezas) == referencia, f"troceo {piezas!r} cambia el resultado"


def test_limpiar_final_para_el_historial():
    """El texto final va al historial: si entra basura, el turno siguiente la imita."""
    assert hb._limpiar_final('Hecho.<｜DSML｜function_calls>\n<invoke name="x">') == 'Hecho.'
    assert hb._limpiar_final('Todo correcto.') == 'Todo correcto.'
    assert hb._limpiar_final('') == ''


# --- Verdad de campo: qué cuenta como herramienta ejecutada --------------------------
# assistant_service usa esta lista para no dejar que el modelo afirme acciones que no
# hizo. Si las meta-tools del puente cuentan, el guardarraíl da por buena cualquier
# afirmación — pasó el 23/08/2026: ocho llamadas al puente, cero herramientas, y las
# frases de "ya está hecho" salieron por el altavoz.

def test_una_tool_normal_cuenta_como_trabajo_real():
    assert hb._clasificar_tool("music_control", {}) == ("music_control", None)


@pytest.mark.parametrize("meta", ["tool_search", "tool_describe"])
def test_buscar_en_el_catalogo_no_es_trabajo(meta):
    real, usada = hb._clasificar_tool(meta, {"query": "musica"})
    assert real is None          # no cuenta como verdad de campo
    assert usada == meta         # pero se registra para poder avisar


def test_tool_call_se_desenvuelve():
    """`tool_call` SÍ ejecuta algo real, pero envuelto: el nombre va en los argumentos."""
    real, usada = hb._clasificar_tool(
        "tool_call", {"name": "mcp__luka__music_control", "arguments": {}})
    assert real == "mcp__luka__music_control"
    assert usada == "tool_call"


@pytest.mark.parametrize("args", [
    {},                              # sin nombre: no llegó a invocar nada
    {"name": ""},
    {"name": "tool_search"},         # el puente se niega a invocarse a sí mismo
])
def test_tool_call_sin_destino_no_cuenta(args):
    real, usada = hb._clasificar_tool("tool_call", args)
    assert real is None
    assert usada == "tool_call"


def test_nombre_vacio_no_cuenta():
    assert hb._clasificar_tool("", {}) == (None, None)
    assert hb._clasificar_tool(None, {}) == (None, None)


@pytest.mark.parametrize("entrada,esperado", [
    # El final NO es un stream: se trunca en el primer marcador, porque lo que va detrás
    # es el cuerpo de la llamada, no texto.
    ('Hecho.<｜DSML｜function_calls>\n<invoke name="x">', 'Hecho.'),
    ('Listo <|tool_call|> ya.',                          'Listo'),
    ('Todo correcto.',                                   'Todo correcto.'),
    # Pero no debe comerse texto legítimo entre ángulos: esto se habla en alto.
    ('Renombra el fichero a <nombre>.',                  'Renombra el fichero a <nombre>.'),
    ('El resultado es 2 < 3.',                           'El resultado es 2 < 3.'),
    ('Guarda <config> y <datos>.',                       'Guarda <config> y <datos>.'),
])
def test_limpiar_final_no_se_come_texto_legitimo(entrada, esperado):
    assert hb._limpiar_final(entrada) == esperado
