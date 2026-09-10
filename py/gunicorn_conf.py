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
import sys
import logging

# Make this "py/" directory importable so `gunicorn py.app:app` works
# regardless of the current working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Bind host/port. Default 5001 matches the compose mapping ("5002:5001"); set
# TRIOFORGE_PORT (and update the mapping, e.g. "5003:5003") to change it.
bind = "0.0.0.0:%s" % os.environ.get("TRIOFORGE_PORT", "5001")

# Keep the worker count LOW. TrioForge keeps per-process state (an SQLite
# connection, the selected model, in-memory caches, the llama.cpp handle), and
# SQLite writes are only guarded by a per-process lock — so several workers can
# hit "database is locked". Two workers is plenty for a personal local app.
# Override with TRIOFORGE_WORKERS if you know what you're doing.
try:
    workers = max(1, int(os.environ.get("TRIOFORGE_WORKERS", "2") or 2))
except (TypeError, ValueError):
    workers = 2
threads = 4
worker_class = "sync"
timeout = 300

CERT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cert_store")
_certfile = os.path.join(CERT_DIR, "localhost+1.pem")
_keyfile = os.path.join(CERT_DIR, "localhost+1-key.pem")

# Default to plain HTTP on localhost (a "secure context" with no scary browser
# warning). HTTPS only when TRIOFORGE_SSL=1 AND certs exist (e.g. you mount your
# own trusted cert_store/), since a generated self-signed cert scares users.
_ssl_env = os.environ.get("TRIOFORGE_SSL", "").strip().lower()
_want_https = _ssl_env in ("1", "true", "on")

if _want_https and os.path.exists(_certfile) and os.path.exists(_keyfile):
    certfile = _certfile
    keyfile = _keyfile
    logger.info("gunicorn: HTTPS enabled using %s", _certfile)
elif _want_https:
    logger.warning("gunicorn: TRIOFORGE_SSL=1 but no certs at %s — serving plain HTTP.", _certfile)
else:
    logger.info("gunicorn: serving plain HTTP (set TRIOFORGE_SSL=1 for HTTPS).")