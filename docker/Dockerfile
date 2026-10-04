# python:3.12-slim is multi-arch (linux/amd64 + linux/arm64), so this single
# Dockerfile builds on Linux x86_64 AND Apple Silicon with no changes.
FROM python:3.12-slim

WORKDIR /app

# System tools:
#   gcc/g++  — build wheels for any dep without a prebuilt
#   openssl  — self-signed TLS cert (only when TRIOFORGE_SSL=1)
#   ffmpeg   — video-to-text / audio-to-text (py/video_to_text.py)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ openssl ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional semantic-RAG stack (torch ~2 GB). Left out by default so the image
# stays small; uncomment to bake it in (or install it at runtime):
# COPY requirements-ml.txt .
# RUN pip install --no-cache-dir -r requirements-ml.txt

# Copy the rest of the code
COPY . .

# Directories used as volumes, plus the model capability folders (the app also
# creates these on startup, but pre-creating keeps mounted volumes predictable).
RUN mkdir -p json_configuration sqlite_data cert_store static/uploads logs \
             models video_model universal_models_to_text

# Internal gunicorn port. Override with TRIOFORGE_PORT and adjust the compose
# port mapping to match, e.g. "5003:5003" + TRIOFORGE_PORT=5003.
EXPOSE 5001

# No curl in the image; Python's stdlib does the liveness check. Uses the same
# TRIOFORGE_PORT the server was told to bind, so a port override stays in sync.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; p=os.environ.get('TRIOFORGE_PORT','5001'); urllib.request.urlopen('http://127.0.0.1:'+p+'/', timeout=4)" || exit 1

# The entrypoint only generates a self-signed cert when TRIOFORGE_SSL=1; by
# default it serves plain HTTP (localhost is a secure context — no scary
# browser warning, and mic/clipboard still work).
ENTRYPOINT ["/bin/sh", "docker/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "py/gunicorn_conf.py", "py.app:app"]
