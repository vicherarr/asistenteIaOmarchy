#!/usr/bin/env bash
# =============================================================================
# AsistenteIA - Generador de Certificados SSL/TLS Autofirmados
# =============================================================================

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

CERTS_DIR="config/certs"
mkdir -p "$CERTS_DIR"

echo "-> Generando certificados SSL autofirmados para desarrollo local..."

openssl req -x509 -newkey rsa:4096 \
  -keyout "$CERTS_DIR/key.pem" \
  -out "$CERTS_DIR/cert.pem" \
  -sha256 \
  -days 3650 \
  -nodes \
  -subj "/CN=localhost"

if [ $? -eq 0 ]; then
  echo "✅ Certificados generados con éxito en:"
  echo "   Clave:  $CERTS_DIR/key.pem"
  echo "   Cert:   $CERTS_DIR/cert.pem"
else
  echo "❌ Error al generar los certificados SSL."
  exit 1
fi
