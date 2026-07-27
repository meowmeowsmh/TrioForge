# gunicorn_conf.py – run app.py under gunicorn WITH HTTPS.
#
# Unlike Waitress, gunicorn has native TLS support (certfile/keyfile),
# so no socket-wrapping is needed here.
#
# NOTE: gunicorn does not run natively on Windows (it relies on
# fork()/POSIX signals). Use this via WSL2, Docker, or a Linux host.
# On native Windows, use serve_https.py (Waitress) instead.
#
# Usage:
#   gunicorn -c gunicorn_conf.py app:app

import os

bind = "0.0.0.0:5001"
workers = 4
threads = 2
worker_class = "sync"

CERT_DIR = "cert_store"
_certfile = os.path.join(CERT_DIR, "localhost+1.pem")
_keyfile = os.path.join(CERT_DIR, "localhost+1-key.pem")

if os.path.exists(_certfile) and os.path.exists(_keyfile):
    certfile = _certfile
    keyfile = _keyfile
    print(f"🔒 gunicorn: HTTPS enabled using {_certfile}")
else:
    print(f"⚠️  gunicorn: certs not found at {_certfile} / {_keyfile} — serving plain HTTP.")
    print("   Run app.py directly once (python app.py) to auto-generate certs via mkcert,")
    print("   or supply your own cert_store/localhost+1.pem and localhost+1-key.pem.")