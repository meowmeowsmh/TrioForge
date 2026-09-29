#!/usr/bin/env bash
# ============================================================
#  refresh-integrity.sh — regenerate integrity-manifest.json.
#
#  Run this after ADDING or REMOVING app files (anything under py/,
#  templates/, static/ or docker/). The manifest is a SHA-256 of every app
#  file, and CI fails with "a fresh clone must verify at 0%" until it matches
#  the tree. Forgetting this is the only way that check ever goes red.
#
#      ./refresh-integrity.sh
#      ./refresh-integrity.sh "baseline after the widget rework"
#
#  Then commit integrity-manifest.json with the code change.
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$ROOT"

PY="$ROOT/.venv-linux/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

NOTE="${1:-baseline refreshed by refresh-integrity.sh}"

PYTHONPATH=py "$PY" - "$NOTE" <<'PY'
import sys
from tools import integrity

note = sys.argv[1] if len(sys.argv) > 1 else "baseline refreshed"
manifest = integrity.write_baseline(".", note=note)
report = integrity.check(".")

print("manifest files:", len(manifest["files"]))
print("risk:", str(report["risk_pct"]) + "%",
      "| modified:", len(report["modified"]),
      "| missing:", len(report["missing"]),
      "| added:", len(report["added"]))

if report["risk_pct"] != 0:
    print()
    print("Still not clean - look at:")
    for label in ("modified", "missing", "added"):
        for path in report[label][:20]:
            print(f"  {label:9} {path}")
    raise SystemExit(1)

print()
print("Clean. Now commit integrity-manifest.json:")
print("    git add integrity-manifest.json")
PY
