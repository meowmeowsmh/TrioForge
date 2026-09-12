#!/bin/sh
# TrioForge — one-command Docker launcher for Linux / macOS.
#
# Does everything a first-time Docker user needs, in the right order:
#   1. checks Docker + Compose are installed AND the daemon is running
#      (with per-OS instructions if not)
#   2. creates the host folders that get bind-mounted, so Docker never creates
#      root-owned directories in your project
#   3. checks whether a host llama-server is reachable (the container connects
#      to it, since the image does NOT ship llama.cpp)
#   4. builds the image and starts the stack
#   5. prints the exact URL to open
#
# Usage:
#   ./docker/application.sh                 # setup + build (if needed) + start
#   ./docker/application.sh --update        # pull the newest image and recreate
#   ./docker/application.sh --build         # force a rebuild first
#   ./docker/application.sh --no-build      # start without building
#   ./docker/application.sh --foreground    # run attached (Ctrl+C to stop)
#   ./docker/application.sh --logs          # follow logs
#   ./docker/application.sh --status        # show container status
#   ./docker/application.sh --stop          # stop and remove the container
#   ./docker/application.sh --help
#
# Updating: the image is rebuilt by CI on every push to main, so --update is all
# you need. The compose file already uses `restart: unless-stopped`, which means
# the container comes back automatically after a reboot - no extra setup.
#
# First time?  chmod +x docker/application.sh

set -e

# ---- locate the project root (this script lives in <root>/docker/) ----------
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR/.."
ROOT=$(pwd)
COMPOSE_FILE="docker/docker-compose.yml"

echo ""
echo "============================================================"
echo "  TrioForge — Docker setup"
echo "  project: $ROOT"
echo "============================================================"
echo ""

# ---- flags -----------------------------------------------------------------
DO_BUILD="auto"     # auto | force | never
MODE="up-detached"  # up-detached | up-foreground | logs | status | stop

for arg in "$@"; do
    case "$arg" in
        --build)      DO_BUILD="force" ;;
        --no-build)   DO_BUILD="never" ;;
        --update|--pull) MODE="update" ;;
        --foreground|-f) MODE="up-foreground" ;;
        --logs)       MODE="logs" ;;
        --status)     MODE="status" ;;
        --stop|--down) MODE="stop" ;;
        --help|-h)
            sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "[TrioForge] Unknown option: $arg  (try --help)"
            exit 1
            ;;
    esac
done

# ---- 1) Docker + Compose present? ------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "[TrioForge] Docker was not found."
    echo ""
    echo "  macOS:  install Docker Desktop"
    echo "            brew install --cask docker      (or https://www.docker.com/products/docker-desktop)"
    echo "  Linux:  install Docker Engine + the compose plugin"
    echo "            https://docs.docker.com/engine/install/"
    echo "          Debian/Ubuntu one-liner:"
    echo "            sudo apt update && sudo apt install -y docker.io docker-compose-plugin"
    echo "            sudo usermod -aG docker \$USER   # then log out and back in"
    echo ""
    exit 1
fi

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"          # Compose v2 (plugin) — the modern one
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"          # Compose v1 (legacy standalone binary)
else
    echo "[TrioForge] Docker is installed, but Docker Compose is not."
    echo "  macOS:  it ships with Docker Desktop — make sure Docker Desktop is installed."
    echo "  Linux:  sudo apt install -y docker-compose-plugin   (or 'docker-compose' for v1)"
    exit 1
fi
echo "[TrioForge] Using: $COMPOSE"

# ---- …and the daemon actually running? -------------------------------------
if ! docker info >/dev/null 2>&1; then
    echo ""
    echo "[TrioForge] The Docker daemon isn't responding (installed, but not running)."
    echo ""
    echo "  macOS / Windows:  launch Docker Desktop and wait for the whale icon to settle."
    echo "  Linux:            sudo systemctl start docker"
    echo "                    # and make sure you're in the docker group:"
    echo "                    sudo usermod -aG docker \$USER   # then log out and back in"
    echo ""
    exit 1
fi
echo "[TrioForge] Docker daemon: OK"

# ---- 2) host folders that are bind-mounted ---------------------------------
# Created here (owned by YOU, not root) so Docker never makes root-owned dirs
# inside the project, and so the app always has somewhere to write.
echo ""
echo "[TrioForge] Preparing host folders..."
for d in json_configuration sqlite_data static/uploads cert_store logs \
         models video_model universal_models_to_text; do
    mkdir -p "$d"
done
echo "  ok: json_configuration, sqlite_data, static/uploads, cert_store, logs,"
echo "      models/, video_model/, universal_models_to_text/"

# ---- 3) llama.cpp lives on the HOST (the image doesn't ship it) ------------
echo ""
echo "[TrioForge] Checking for a host llama-server (needed for local models)..."
LLAMA_EXE=""
for cand in \
    "$(command -v llama-server 2>/dev/null || true)" \
    "/opt/homebrew/bin/llama-server" \
    "/usr/local/bin/llama-server" \
    "/usr/bin/llama-server" \
    "$HOME/llama.cpp/build/bin/llama-server" \
    "$HOME/llama.cpp/bin/llama-server" \
    "$ROOT/tools/llama.cpp"
do
    if [ -n "$cand" ] && [ -f "$cand" ]; then LLAMA_EXE="$cand"; break; fi
done
if [ -z "$LLAMA_EXE" ]; then
    # also look inside the in-app auto-installer's tree
    LLAMA_EXE=$(find "$ROOT/tools/llama.cpp" -name llama-server -type f 2>/dev/null | head -1 || true)
fi

if [ -n "$LLAMA_EXE" ]; then
    echo "  found llama-server: $LLAMA_EXE"
else
    echo "  no llama-server on the host (fine — cloud providers work without it)."
    echo "  For local models:  brew install llama.cpp   (macOS)"
    echo "                     or build/download it and drop the binary in ~/llama.cpp/build/bin/"
fi

# Is something already answering on 8080?
if command -v curl >/dev/null 2>&1; then
    if curl -fsS "http://127.0.0.1:8080/health" >/dev/null 2>&1 \
       || curl -fsS "http://127.0.0.1:8080/v1/models" >/dev/null 2>&1; then
        echo "  llama-server is RUNNING on the host at :8080 — the container will use it."
    elif [ -n "$LLAMA_EXE" ]; then
        echo "  llama-server is NOT running yet. Start it before chatting with local models:"
        echo "      \"$LLAMA_EXE\" -m models/<your-model>.gguf --port 8080"
    fi
fi

# ---- read the host port out of the compose file ----------------------------
HOST_PORT=$(grep -m1 -oE '"[0-9]+:[0-9]+"' "$COMPOSE_FILE" 2>/dev/null | tr -d '"' | cut -d: -f1 || true)
[ -n "$HOST_PORT" ] || HOST_PORT=5002

# ---- status / stop / logs short-circuits -----------------------------------
case "$MODE" in
    status)
        echo ""
        cd docker && $COMPOSE ps
        exit 0
        ;;
    stop)
        echo ""
        echo "[TrioForge] Stopping the stack..."
        cd docker && $COMPOSE down
        echo "[TrioForge] Stopped."
        exit 0
        ;;
    logs)
        echo ""
        echo "[TrioForge] Following logs (Ctrl+C to stop watching — the app keeps running)..."
        cd docker && $COMPOSE logs -f
        exit 0
        ;;
    update)
        # The image is rebuilt by CI on every push to main, so updating is a pull
        # plus a recreate. Data lives in the bind mounts, so nothing is lost.
        echo ""
        echo "[TrioForge] Pulling the newest image..."
        cd docker
        $COMPOSE pull || echo "[TrioForge] No published image to pull (built locally)."
        $COMPOSE up -d --remove-orphans
        echo ""
        echo "[TrioForge] Updated and running. Open http://localhost:$HOST_PORT"
        exit 0
        ;;
esac

# ---- 4) build ---------------------------------------------------------------
cd docker
if [ "$DO_BUILD" = "force" ]; then
    echo ""
    echo "[TrioForge] Building the image (forced)..."
    $COMPOSE build
elif [ "$DO_BUILD" = "auto" ]; then
    if ! docker image inspect trio-forge >/dev/null 2>&1; then
        echo ""
        echo "[TrioForge] First run — building the image (this takes a few minutes)..."
        $COMPOSE build
    else
        echo ""
        echo "[TrioForge] Image already built (use --build to rebuild)."
    fi
fi

# ---- 5) start ---------------------------------------------------------------
echo ""
if [ "$MODE" = "up-foreground" ]; then
    echo "[TrioForge] Starting (attached — Ctrl+C stops it)..."
    echo "[TrioForge] Open http://localhost:$HOST_PORT"
    echo ""
    $COMPOSE up
else
    echo "[TrioForge] Starting in the background..."
    $COMPOSE up -d
    echo ""
    $COMPOSE ps
    echo ""
    echo "============================================================"
    echo "  ✅ TrioForge is running"
    echo ""
    echo "     Open:      http://localhost:$HOST_PORT"
    echo "     Logs:      ./docker/application.sh --logs"
    echo "     Status:    ./docker/application.sh --status"
    echo "     Stop:      ./docker/application.sh --stop"
    echo ""
    echo "  Local models: run llama-server ON THE HOST (the container connects"
    echo "  to it via LLAMA_HOST=host.docker.internal:8080):"
    echo "      llama-server -m models/<your-model>.gguf --port 8080"
    echo ""
    echo "  Models: drop .gguf files into models/ on the host — they appear in"
    echo "  the container automatically (the folder is bind-mounted)."
    echo "============================================================"
    echo ""
fi
