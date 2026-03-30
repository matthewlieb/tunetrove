#!/usr/bin/env bash
# Stop background API/Web from start-local.sh, then free dev ports.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for f in "$ROOT/.dev/api.pid" "$ROOT/.dev/web.pid"; do
  if [[ -f "$f" ]]; then
    pid="$(cat "$f" || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "Killing PID $pid ($(basename "$f"))"
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$f"
  fi
done

"$ROOT/scripts/kill-dev-ports.sh"
echo "Stopped (ports 8013 / 3003 should be free)."
