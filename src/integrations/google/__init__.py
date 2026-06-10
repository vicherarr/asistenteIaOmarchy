"""Integraciones con Google (Gmail; Calendar/Drive en el futuro).

`auth.py` centraliza el OAuth2 y la gestión del token, compartido por todas las
APIs de Google. Las credenciales (client secret) las aporta el usuario desde su
propio proyecto de Google Cloud y NUNCA se versionan (ver .gitignore).
"""
