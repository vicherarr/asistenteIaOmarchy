#!/usr/bin/env bash
# =============================================================================
# Copia el certificado TLS del asistente al firmware, para fijarlo (pinning).
#
# El firmware NO confía en ninguna CA: solo acepta exactamente este certificado.
# Eso hace innecesario que el certificado sea "válido" en el sentido habitual —
# de hecho el del asistente es autofirmado y con CN=localhost, así que jamás
# pasaría una validación normal contra una IP.
#
# OJO con de dónde se copia: el repo de desarrollo y el deploy (~/.asistenteia)
# tienen certificados DISTINTOS. El que importa es el del deploy, porque es el
# que usa el servidor que de verdad está escuchando. Por eso ese es el default.
# =============================================================================
set -euo pipefail

FIRMWARE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_CERT="${HOME}/.asistenteia/config/certs/cert.pem"
DEST="${FIRMWARE_DIR}/certs/server.pem"

SRC="${1:-$DEPLOY_CERT}"

if [ ! -f "$SRC" ]; then
    echo "✗ No existe el certificado: $SRC" >&2
    echo "  ¿Está instalado el asistente? Genera los certificados con:" >&2
    echo "      ~/.asistenteia/scripts/generate-certs.sh" >&2
    exit 1
fi

if ! openssl x509 -in "$SRC" -noout 2>/dev/null; then
    echo "✗ $SRC no es un certificado X.509 válido." >&2
    exit 1
fi

# Aviso si el servidor está corriendo desde otra ruta: sería fijar el certificado
# equivocado, y el síntoma (un handshake que falla) no apunta a esta causa.
if [ "$SRC" = "$DEPLOY_CERT" ] && ! pgrep -f "\.asistenteia/venv/bin/python -m src\.main" >/dev/null 2>&1; then
    echo "⚠ El asistente no parece estar corriendo desde ~/.asistenteia."
    echo "  Comprueba que el certificado que fijas es el del servidor real."
fi

mkdir -p "$(dirname "$DEST")"
cp "$SRC" "$DEST"

echo "✓ Certificado fijado en firmware/certs/server.pem"
echo "  origen:   $SRC"
echo "  asunto:   $(openssl x509 -in "$DEST" -noout -subject | sed 's/^subject=//')"
echo "  caduca:   $(openssl x509 -in "$DEST" -noout -enddate | cut -d= -f2)"
echo "  SHA-256:  $(openssl x509 -in "$DEST" -noout -fingerprint -sha256 | cut -d= -f2)"
echo
echo "  Recompila el firmware para que el cambio surta efecto."
echo "  Si algún día regeneras los certificados del asistente, vuelve a ejecutar"
echo "  este script: el firmware dejaría de conectar hasta entonces."
