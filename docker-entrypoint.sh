#!/bin/sh
# docker-entrypoint.sh
#
# app.py's ensure_certificates() only auto-generates certs on Windows
# (it uses mkcert). Inside this Linux container that path never runs,
# so if you don't mount your own certs, we generate a self-signed one
# here with openssl instead. Either way, gunicorn_conf.py picks up
# whatever ends up in cert_store/ and enables HTTPS automatically.

set -e

CERT_DIR="cert_store"
CERT_FILE="$CERT_DIR/localhost+1.pem"
KEY_FILE="$CERT_DIR/localhost+1-key.pem"

mkdir -p "$CERT_DIR"

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "🔑 No certificate found in $CERT_DIR — generating a self-signed one..."
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout "$KEY_FILE" \
        -out "$CERT_FILE" \
        -days 825 \
        -subj "/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
    echo "✅ Self-signed cert created at $CERT_FILE"
    echo "   (Browsers will warn it's not trusted — that's expected for a"
    echo "   self-signed cert. Mount your own mkcert-issued cert_store/ as"
    echo "   a volume instead if you want a browser-trusted cert.)"
else
    echo "🔒 Using existing certificate found in $CERT_DIR"
fi

exec "$@"
