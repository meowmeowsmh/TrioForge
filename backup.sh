#!/usr/bin/env bash
# =============================================================================
# backup.sh — back up your TrioForge (code + settings + data) into one tarball.
#
#   ./backup.sh                    # code + your config/chat data (NOT the models)
#   ./backup.sh --all              # also include uploads + generated media
#   ./backup.sh --restore <file>   # unpack a backup back into this folder
#
# Deliberately left OUT — huge or re-downloadable, so a backup stays small:
#   models/ · video_model/ · universal_models_to_text/   (multi-GB weights)
#   .venv*  · tools/llama.cpp/ · tools/ffmpeg/           (self-rebuild/auto-fetch)
#   logs/  · backups/
#
# What it DOES capture:
#   every tracked source file (py/, templates/, static/, docker/, scripts),
#   plus your json_configuration/ (settings, keys) and sqlite_data/ (chat,
#   notes, corkboard). --all adds static/uploads and generated media.
#
# The archive is written to backups/ (git-ignored) and is NEVER committed: it
# contains your API keys and conversations.
# =============================================================================
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

if [[ "${1:-}" == "--restore" ]]; then
    ARCHIVE="${2:-}"
    [[ -n "$ARCHIVE" && -f "$ARCHIVE" ]] || { echo "usage: $0 --restore <archive.tar.gz>" >&2; exit 1; }
    tar -xzf "$ARCHIVE"
    echo "restored: $ARCHIVE  ->  $(pwd)"
    exit 0
fi

ALL=0
[[ "${1:-}" == "--all" ]] && ALL=1

STAMP="$(date '+%Y%m%d-%H%M%S')"
mkdir -p backups
OUT="backups/trioforge-backup-${STAMP}.tar.gz"
TMP="$(mktemp)"
trap 'rm -f "$TMP" "$TMP.gz"' EXIT

# 1) The code. In a git checkout this is exactly the tracked files, so models,
#    venvs and the auto-installed llama.cpp (all git-ignored) can never sneak
#    in. Without git, fall back to a tar of the folder with the heavy bits
#    excluded by name.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git archive --format=tar -o "$TMP" HEAD
else
    tar -cf "$TMP" \
        --exclude='models' --exclude='video_model' --exclude='universal_models_to_text' \
        --exclude='.venv' --exclude='.venv-linux' --exclude='tools/llama.cpp' \
        --exclude='tools/ffmpeg' --exclude='logs' --exclude='backups' \
        --exclude='*.gguf' --exclude='*.safetensors' .
fi

# 2) Your data — git-ignored, so step 1 did not include it.
for d in json_configuration sqlite_data; do
    [[ -d "$d" ]] && tar -rf "$TMP" "$d"
done

# 3) Optional: uploads + generated media (can be large).
if [[ "$ALL" -eq 1 ]]; then
    for d in static/uploads static/generated static/generated_video; do
        [[ -d "$d" ]] && tar -rf "$TMP" "$d"
    done
fi

gzip -9 "$TMP"
mv "$TMP.gz" "$OUT"
echo "backup -> $OUT  ($(du -h "$OUT" | cut -f1))"
echo "restore with:  ./backup.sh --restore $OUT"
