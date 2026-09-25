#!/usr/bin/env bash
# Push TrioForge to GitHub over SSH.
# Run this AFTER the public key below has been added at
# https://github.com/settings/ssh/new
set -uo pipefail
cd "$(dirname "$0")"

PUB="$(cat "$HOME/.ssh/id_ed25519.pub" 2>/dev/null || true)"
if [ -z "$PUB" ]; then
  echo "No SSH key found. Run:  ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 -C limcherng1@gmail.com"
  exit 1
fi

echo "───────────────────────────────────────────────────────────────"
echo " The key that must be registered on GitHub:"
echo "   $PUB"
echo " Add it at: https://github.com/settings/ssh/new"
echo "───────────────────────────────────────────────────────────────"
echo

printf 'Testing ssh to github.com ... '
out="$(ssh -o ConnectTimeout=10 -o BatchMode=yes -T git@github.com 2>&1 || true)"
if printf '%s' "$out" | grep -q "successfully authenticated"; then
  echo "OK"
  echo "  $out"
else
  echo "FAILED"
  echo "  $out"
  echo
  echo "The key is not on GitHub yet. Open this link, paste the key, save:"
  echo "    https://github.com/settings/ssh/new"
  echo
  echo "Then run this script again."
  exit 1
fi

echo
echo "Pushing $(git rev-list --count origin/main..HEAD 2>/dev/null || echo '?') commit(s) to origin/main ..."
if git push origin main; then
  echo
  echo "Pushed. Current state:"
  git status -sb | head -3
  echo
  echo "Your code is now at: https://github.com/meowmeowsmh/TrioForge"
else
  echo
  echo "Push failed. Try: git push origin main"
  exit 1
fi
