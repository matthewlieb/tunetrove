#!/usr/bin/env bash
# Free local dev ports for spotify-llm (API + Next.js).
set -euo pipefail
for port in 8013 3003; do
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  if [[ -n "${pids}" ]]; then
    echo "Killing PIDs on port $port: $pids"
    kill -9 $pids 2>/dev/null || true
  else
    echo "Nothing listening on port $port"
  fi
done
