#!/usr/bin/env python3
"""Verifica que el login de Google/Gmail funciona (Fase 0).

Uso (desde la raíz del proyecto):
    venv/bin/python scripts/test-gmail-auth.py

No envía ni modifica nada: solo lee el perfil y el asunto del último correo.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.integrations.google import auth  # noqa: E402


def main() -> int:
    print(f"credentials.json: {auth.credentials_path()}  (existe: {auth.is_configured()})")
    print(f"token.json:       {auth.token_path()}  (existe: {auth.token_path().is_file()})")
    try:
        service = auth.build_service("gmail", "v1")
    except auth.GoogleAuthError as e:
        print(f"\n✗ {e}")
        return 1

    profile = service.users().getProfile(userId="me").execute()
    print(f"\n✓ Conectado como: {profile.get('emailAddress')}")
    print(f"  Total de mensajes en el buzón: {profile.get('messagesTotal')}")

    msgs = service.users().messages().list(userId="me", maxResults=1).execute()
    ids = msgs.get("messages", [])
    if ids:
        msg = service.users().messages().get(
            userId="me", id=ids[0]["id"], format="metadata",
            metadataHeaders=["Subject", "From"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        print(f"  Último correo: «{headers.get('Subject', '(sin asunto)')}» "
              f"de {headers.get('From', '?')}")
    print("\nFase 0 verificada: el acceso a Gmail funciona.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
