#!/usr/bin/env bash
# Repair common local dev breakages (broken FastAPI install, bad Next node_modules).
# Usage: ./scripts/fix-dev-env.sh        — Python venv + optional web reinstall
#        ./scripts/fix-dev-env.sh --clean-web   — also rm -rf apps/web/node_modules && npm install
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CLEAN_WEB=0
for a in "$@"; do
  if [[ "$a" == "--clean-web" ]]; then CLEAN_WEB=1; fi
done

if [[ ! -x "$ROOT/.venv/bin/pip" ]]; then
  echo "No .venv — run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ." >&2
  exit 1
fi

echo "== Python: reinstall FastAPI + Starlette (fixes missing fastapi.openapi.models) =="
"$ROOT/.venv/bin/pip" install -U pip
"$ROOT/.venv/bin/pip" install --force-reinstall "fastapi>=0.115.6,<0.116" "starlette>=0.40.0,<0.42"
"$ROOT/.venv/bin/pip" install -e "$ROOT"
"$ROOT/.venv/bin/python" -c "import fastapi.openapi.models; import fastapi; print('fastapi', fastapi.__version__, 'OK')"

if [[ "$CLEAN_WEB" -eq 1 ]]; then
  echo "== Web: clean install (fixes loadEnvConfig is not a function) =="
  command -v node >/dev/null 2>&1 || { echo "Install Node >= 20.19 (or 22 LTS)."; exit 1; }
  cd "$ROOT/apps/web"
  rm -rf node_modules
  npm install
  echo "Web deps OK. Run: npm run dev"
fi

echo "Done. Start stack: ./scripts/start-local.sh"
