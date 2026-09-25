#!/usr/bin/env bash
# Ask TrioForge about the current screen (local vision model - nothing leaves the PC).
# Bind to a keyboard shortcut, or run:  tools/screen-ask.sh "your question"
#
# First run after a reboot loads the model, so it can take a while - you get a
# "Looking at your screen..." notice, then the answer.
PORT="${TRIOFORGE_PORT:-5003}"
Q="${*:-Describe what is on my screen and point out anything important.}"

_notify() { command -v notify-send >/dev/null 2>&1 && notify-send -a TrioForge "$1" "$2"; }

if ! (echo >/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
    _notify "TrioForge is not running" "Start TrioForge first, then try again."
    echo "TrioForge is not running on port $PORT"
    exit 1
fi

_notify "🖥️ Looking at your screen…" "The local vision model is reading it. This can take up to a minute."

BODY=$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$Q")
RESP=$(curl -s -m 300 -X POST "http://127.0.0.1:${PORT}/api/screen/ask" \
        -H 'Content-Type: application/json' --data-binary "$BODY")

ANSWER=$(printf '%s' "$RESP" | python3 -c '
import sys, json
raw = sys.stdin.read().strip()
if not raw:
    print("No response from TrioForge (timed out, or the app stopped)."); raise SystemExit
try:
    d = json.loads(raw)
except Exception:
    print("Unexpected reply: " + raw[:400]); raise SystemExit
print((d.get("answer") or d.get("error") or "").strip() or "(empty answer)")
')

if command -v zenity >/dev/null 2>&1; then
    printf '%s\n' "$ANSWER" | zenity --text-info --title="TrioForge — screen" \
        --width=680 --height=460 2>/dev/null
else
    _notify "TrioForge — screen" "$ANSWER"
    printf '%s\n' "$ANSWER"
fi
