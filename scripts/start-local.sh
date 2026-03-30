#!/usr/bin/env bash
# Free dev ports, then start FastAPI (8013) + Next.js (3003) in the background.
# Logs: .dev/api.log and .dev/web.log — stop with: ./scripts/stop-local.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

"$ROOT/scripts/kill-dev-ports.sh"

mkdir -p "$ROOT/.dev"
UVICORN="${ROOT}/.venv/bin/uvicorn"
if [[ ! -x "$UVICORN" ]]; then
  echo "Missing $UVICORN — create venv and install: python3 -m venv .venv && source .venv/bin/activate && pip install -e ." >&2
  exit 1
fi

node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  local ver major minor
  ver=$(node -v 2>/dev/null | sed 's/^v//')
  major=${ver%%.*}
  local rest=${ver#*.}
  minor=${rest%%.*}
  [[ "$major" =~ ^[0-9]+$ ]] && [[ "$minor" =~ ^[0-9]+$ ]] || return 1
  if (( major > 20 )); then return 0; fi
  if (( major < 20 )); then return 1; fi
  if (( minor >= 19 )); then return 0; fi
  return 1
}
if ! node_ok; then
  echo "Node $(command -v node >/dev/null && node -v || echo missing) is too old for Next 15 (need >= 20.19)." >&2
  echo "Use nvm: nvm install && nvm use (see apps/web/.nvmrc), then: cd apps/web && rm -rf node_modules && npm install" >&2
  exit 1
fi

: >"$ROOT/.dev/api.log"
: >"$ROOT/.dev/web.log"

PYTHONPATH="$ROOT" nohup "$UVICORN" src.web.app:app --host 0.0.0.0 --port 8013 >>"$ROOT/.dev/api.log" 2>&1 &
echo $! >"$ROOT/.dev/api.pid"

cd "$ROOT/apps/web"
nohup npm run dev >>"$ROOT/.dev/web.log" 2>&1 &
echo $! >"$ROOT/.dev/web.pid"
cd "$ROOT"

echo ""
echo "  API pid $(cat "$ROOT/.dev/api.pid")  →  http://127.0.0.1:8013"
echo "  Web pid $(cat "$ROOT/.dev/web.pid")  →  http://127.0.0.1:3003"
echo "  Logs:  tail -f .dev/api.log .dev/web.log"
echo "  Stop:  ./scripts/stop-local.sh"
echo ""
echo "  Note: Next.js dev sends no HTML until the first compile finishes (often 30–90s)."
echo "  A background request pre-warms GET /; watch .dev/web.log."
echo ""

# Kick page compile early so the browser is less likely to sit on a blank tab first open.
(
  for i in $(seq 1 180); do
    if curl -sfS --max-time 10 http://127.0.0.1:3003/ >/dev/null 2>&1; then
      echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") warm-web: GET / OK after ${i}s" >>"$ROOT/.dev/web.log"
      exit 0
    fi
    sleep 1
  done
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") warm-web: timeout waiting for GET / (see above)" >>"$ROOT/.dev/web.log"
) &

# First import of the app can be slow on cold machines; wait up to ~3 minutes.
for i in $(seq 1 180); do
  if curl -sfS http://127.0.0.1:8013/health >/dev/null 2>&1; then
    echo "  API /health OK."
    break
  fi
  if (( i % 30 == 0 )); then
    echo "  … still waiting for API (/health), ${i}s — large Python import or agent prewarm; see .dev/api.log" >&2
  fi
  sleep 1
done

if ! curl -sfS http://127.0.0.1:8013/health >/dev/null 2>&1; then
  echo "  Warning: API did not respond on /health yet — see .dev/api.log" >&2
fi
