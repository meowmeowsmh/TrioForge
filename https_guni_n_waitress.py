# serve_https.py – run the Flask app under Waitress WITH HTTPS.
#
# Waitress has no built-in TLS support, so this creates a normal TCP
# socket, wraps it in an SSLContext using the same mkcert certs app.py
# already generates, and hands that wrapped socket to waitress.serve().
#
# Usage (from the same folder as app.py):
#   python serve_https.py
#
# This replaces the command:
#   python -m waitress --host=0.0.0.0 --port=5001 app:app
# which only gives you plain HTTP.

import os
import socket
import ssl
import sys
import logging

from waitress import serve

logger = logging.getLogger(__name__)

# Reuse app.py's certificate logic so this stays in sync with it.
from app import app, ensure_certificates

HOST = "0.0.0.0"
PORT = 5001

CERT_DIR = "cert_store"
CERT_FILE = os.path.join(CERT_DIR, "localhost+1.pem")
KEY_FILE = os.path.join(CERT_DIR, "localhost+1-key.pem")


def main():
    if not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)):
        logger.info("Certificates not found, generating (Windows + mkcert only)...")
        if not ensure_certificates():
            logger.error("Could not obtain certificates. Falling back to plain HTTP.")
            logger.info("http://%s:%s", HOST, PORT)
            serve(app, host=HOST, port=PORT)
            return

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)
    except Exception as e:
        logger.error("Failed to load certificates: %s", e)
        sys.exit(1)

    # Optional but recommended: modern TLS only.
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2

    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raw_sock.bind((HOST, PORT))
    raw_sock.listen(1024)

    wrapped_sock = ssl_context.wrap_socket(raw_sock, server_side=True)

    logger.info("Running with HTTPS (SSL enabled) via Waitress")
    logger.info("Open your browser at: https://localhost:%s", PORT)

    serve(app, sockets=[wrapped_sock])


if __name__ == "__main__":
    main()