.PHONY: help kill-ports start-local stop-local fix-api fix-dev-env install install-web smoke check-imports

help:
	@echo "spotify-llm — common commands"
	@echo "  make start-local  — kill 8013/3003, start API + Next in background (.dev/*.log)"
	@echo "  make fix-api      — reinstall FastAPI/Starlette (broken venv)"
	@echo "  make fix-dev-env  — fix-api + clean apps/web node_modules + npm install"
	@echo "  make stop-local   — stop PIDs from start-local + free ports"
	@echo "  make kill-ports   — free TCP 8013 and 3003"
	@echo "  make install      — pip install -e . (from repo root, venv active)"
	@echo "  make install-web  — npm ci in apps/web"
	@echo "  make check-imports — fast API import (no agent)"
	@echo "  make smoke        — curl /health + /auth/status (set API=http://127.0.0.1:8013)"
	@echo ""
	@echo "Run in two terminals:"
	@echo "  PYTHONPATH=. uvicorn src.web.app:app --host 0.0.0.0 --port 8013"
	@echo "  cd apps/web && npm run dev"
	@echo "Open http://127.0.0.1:3003"

kill-ports:
	@./scripts/kill-dev-ports.sh

start-local:
	@./scripts/start-local.sh

stop-local:
	@./scripts/stop-local.sh

fix-api:
	@./scripts/fix-dev-env.sh

fix-dev-env:
	@./scripts/fix-dev-env.sh --clean-web

install:
	python -m pip install -e .

install-web:
	cd apps/web && npm ci

check-imports:
	@PYTHONPATH=. python -c "import src.web.app; print('api import ok')"

smoke:
	@API=$${API:-http://127.0.0.1:8013}; \
	echo "GET $$API/health"; \
	curl -sfS "$$API/health" && echo ""; \
	echo "GET $$API/auth/status"; \
	curl -sfS "$$API/auth/status" && echo ""
