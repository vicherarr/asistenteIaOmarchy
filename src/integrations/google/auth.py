"""OAuth2 de Google centralizado: carga, refresco y alta del token.

Modelo de credenciales (BYO – Bring Your Own):
  - `credentials.json`: el *client secret* OAuth de tipo "Desktop app" que el USUARIO
    descarga de SU propio proyecto de Google Cloud. Nunca se versiona ni se distribuye.
  - `token.json`: la sesión del usuario (access + refresh token). La genera el flujo
    interactivo `asistenteia google-auth` una sola vez; luego se refresca solo.

Ambos archivos viven por defecto en ~/.config/asistenteia/google/ (fuera del repo).

En PRODUCCIÓN el servicio nunca abre un navegador: solo `run_local_auth()` (el comando
de alta) lo hace. El resto del código usa `build_service()`, que carga el token ya
existente y lo refresca si caducó.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Sequence

from src.config import settings

logger = logging.getLogger(__name__)

# Scopes para "lectura + escritura con confirmación":
#   - gmail.modify  -> leer, buscar, etiquetar y mover a la papelera (NO borrado permanente)
#   - gmail.compose -> crear/editar borradores y ENVIAR mensajes
# (Calendar/Drive añadirán los suyos aquí cuando se implementen.)
GMAIL_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]


class GoogleAuthError(RuntimeError):
    """Problema de autenticación/configuración de Google (mensaje apto para el usuario)."""


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def credentials_path() -> Path:
    return _expand(settings.GOOGLE_CREDENTIALS_PATH)


def token_path() -> Path:
    return _expand(settings.GOOGLE_TOKEN_PATH)


def is_configured() -> bool:
    """¿Existe el credentials.json del usuario? (requisito mínimo para usar Google)."""
    return credentials_path().is_file()


def _save_token(creds) -> None:
    """Guarda el token con permisos 0600 (solo el usuario)."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # p.ej. sistemas de ficheros sin permisos POSIX
        pass


def load_credentials(scopes: Sequence[str] = GMAIL_SCOPES):
    """Carga el token guardado y lo refresca si hace falta. None si no hay sesión válida.

    NO lanza el flujo interactivo: en el servicio nunca debe abrirse un navegador.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    tpath = token_path()
    if not tpath.is_file():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(tpath), list(scopes))
    except Exception as e:  # noqa: BLE001 — token corrupto/incompatible
        logger.warning(f"Token de Google ilegible ({e}); habrá que re-autenticar.")
        return None

    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception as e:  # noqa: BLE001 — refresh revocado/caducado
            logger.warning(f"No se pudo refrescar el token de Google: {e}")
            return None
    return None


def build_service(api: str, version: str, scopes: Sequence[str] = GMAIL_SCOPES):
    """Construye un cliente de la API de Google (p.ej. ('gmail','v1')).

    Lanza GoogleAuthError con un mensaje claro si falta configuración o sesión, para
    que la tool lo devuelva como texto natural y Luka pueda explicárselo al usuario.
    """
    if not is_configured():
        raise GoogleAuthError(
            "Google no está configurado: falta el archivo de credenciales en "
            f"{credentials_path()}. Sigue la guía del README para crearlo."
        )
    creds = load_credentials(scopes)
    if creds is None:
        raise GoogleAuthError(
            "No hay sesión de Google activa. Ejecuta 'asistenteia google-auth' "
            "para iniciar sesión una vez."
        )
    from googleapiclient.discovery import build

    # cache_discovery=False evita warnings y escrituras de caché en disco.
    return build(api, version, credentials=creds, cache_discovery=False)


def run_local_auth(scopes: Sequence[str] = GMAIL_SCOPES):
    """Flujo interactivo de alta (abre el navegador). Solo lo usa 'asistenteia google-auth'.

    Devuelve las credenciales y guarda token.json. Lanza GoogleAuthError si falta el
    credentials.json del usuario.
    """
    if not is_configured():
        raise GoogleAuthError(
            f"Falta el archivo de credenciales en {credentials_path()}.\n"
            "Crea un proyecto en Google Cloud, habilita la Gmail API, crea un OAuth "
            "client ID de tipo 'Desktop app' y descarga el JSON ahí. Ver README."
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path()), list(scopes))
    # port=0 -> puerto libre; abre el navegador para el consentimiento.
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    return creds


def main() -> int:
    """Punto de entrada de 'asistenteia google-auth': alta interactiva del token."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("→ Iniciando sesión con Google. Se abrirá el navegador para dar consentimiento…")
    try:
        run_local_auth()
    except GoogleAuthError as e:
        print(f"\n✗ {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ Falló el inicio de sesión: {e}")
        return 1
    print(f"\n✓ Sesión de Google guardada en {token_path()}")
    print("  Ya puedes usar las funciones de Gmail. (No compartas ese token.json.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
