"""El .env no puede traer claves que Settings no declare.

pydantic-settings valida con extra='forbid': una sola clave desconocida en el .env
hace que el asistente entero NO arranque. Ya pasó dos veces: con `EXLLAMA_CTX`, que
la CLI escribe al elegir modelo de exllama (tumbó el servicio en bucle de reinicio), y
con `HERMES_CTX` / `HERMES_CACHE_MODE`, que venían en el propio `.env.example`.

Este test ata el contrato: si alguien añade una clave solo-shell, tiene que declararla
en `Settings` (aunque la app no la use) o el arranque se rompe.
"""

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


def _claves_env_example() -> set[str]:
    claves = set()
    for linea in (RAIZ / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", linea)
        if m:
            claves.add(m.group(1))
    return claves


def test_env_example_solo_declara_claves_que_settings_conoce():
    from src.config import Settings

    claves = _claves_env_example()
    assert claves, "no se ha podido leer ninguna clave del .env.example"
    sin_declarar = sorted(claves - set(Settings.model_fields))
    assert not sin_declarar, (
        "claves del .env.example que Settings no declara; con extra=forbid el "
        f"asistente no arrancaría: {sin_declarar}"
    )


@pytest.mark.parametrize(
    "clave,valor",
    [
        ("EXLLAMA_CTX", "8192"),      # la escriben 'engine model' y 'engine use'
        ("HERMES_CTX", "65536"),      # perfil del sidecar para Hermes local
        ("HERMES_CACHE_MODE", "Q4"),
    ],
)
def test_settings_acepta_las_claves_que_escribe_la_cli(clave, valor):
    """El caso real: .env con esta clave y la app arrancando sin ValidationError."""
    from src.config import Settings

    s = Settings(_env_file=None, **{clave: valor})
    assert str(getattr(s, clave)) == valor
