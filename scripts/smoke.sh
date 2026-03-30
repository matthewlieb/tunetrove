#!/usr/bin/env bash
# Quick checks against a running API (default http://127.0.0.1:8013).
set -euo pipefail
API="${1:-http://127.0.0.1:8013}"
API="${API%/}"

echo "== GET $API/health"
curl -sfS "$API/health"
echo ""
echo "== GET $API/auth/status"
curl -sfS "$API/auth/status"
echo ""
echo "OK — API is reachable. Start Next with: cd apps/web && npm run dev"
