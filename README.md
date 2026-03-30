# spotify-llm (TempoTrove)

**Site:** [tempotrove.com](https://tempotrove.com) (domain on Cloudflare; point DNS at your Vercel deployment when ready.)

**GitHub:** [github.com/matthewlieb/tempotrove](https://github.com/matthewlieb/tempotrove). If your local remote still says `tunetrove`, run:  
`git remote set-url origin https://github.com/matthewlieb/tempotrove.git`

[Deep Agents](https://github.com/langchain-ai/deepagents)-powered music discovery (v1-lite): Spotify OAuth + chat + Spotify read/write + web research. Product overview: [Deep Agents docs](https://docs.langchain.com/oss/python/deepagents/overview).

| Doc | Purpose |
|-----|---------|
| `docs/V1_LITE_SPEC.md` | Product spec + acceptance checklist |
| `docs/DEPLOYMENT.md` | HTTPS, Vercel, Spotify redirect (`/api/agent/...`), env |
| `docs/GITHUB_AND_VERCEL.md` | GitHub repo + Vercel + **tempotrove.com** |
| `docs/LAUNCH_CHECKLIST.md` | Pre-launch security & ops checklist |
| `docs/COSTS_AND_BILLING.md` | Who pays for DB, LLM, hosting |
| `docs/BYOK.md` | BYOK design notes; implementation uses `USER_LLM_KEYS_FERNET_KEY` + `/auth/llm-keys` |
| `docs/ROADMAP.md` | Next steps + future WhatsApp / messaging |

## Layout

| Part | Role |
|------|------|
| `src/web/app.py` | FastAPI: `/chat`, `/auth/*`, `/health`, `/health/ready` |
| `apps/web` | Next.js UI (Vercel). Browser calls **`/api/agent/*`**; **`app/api/agent/[[...path]]/route.ts`** server-proxies to FastAPI (`AGENT_API_URL`) so embedded browsers avoid cross-port fetches and get JSON if the API is down. |
| `src/agent/factory.py` | [`create_deep_agent`](https://reference.langchain.com/python/deepagents/) only — LangGraph graph + built-in planning / filesystem / summarization |
| `supabase/taste_memory.sql` | Postgres + pgvector + `spotify_users` + RPC |

**You are not missing a piece** if you assumed: Vercel frontend + separate API + Spotify OAuth on the API host — that is the intended split. OAuth flow matches the usual pattern (like `spotify-imessage`): authorize on Spotify → redirect to **API** `/auth/callback` → session cookie → UI talks to API (here via same-origin proxy).

**Spotify tools on `/chat`:** Only the **session-linked** Spotify user (cookie + token in Supabase) is used. The API does **not** fall back to a developer OAuth session for anonymous web requests; the CLI sets an explicit opt-in for the local `spotipy` auth-manager flow.

**LangChain-style UI:** `POST /chat` returns JSON `tool_trace` for tool-call cards (see [docs/FRONTEND_LANGCHAIN.md](docs/FRONTEND_LANGCHAIN.md)). Human-in-the-loop / `useStream` needs LangGraph interrupt + resume wiring — documented there.

## Git branches

- **`main`** — production-ready; CI must pass. Deploy previews/production from here (or tag releases).
- **`develop`** — integration branch for day-to-day work; merge to `main` when you cut a release. CI runs on both (see `.github/workflows/ci.yml`).

Feature branches: open PRs **into `develop`** (then `develop` → `main`), or PRs straight to `main` for small fixes—pick one team habit and stick to it.

## Quick setup

```bash
cd spotify-llm
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
# Edit .env — see table below
```

**Frontend env** (`apps/web`):

```bash
cd apps/web && cp .env.local.example .env.local && npm install
```

### Required / important `.env` (root)

| Variable | Local dev | Production (public site) |
|----------|-----------|----------------------------|
| `OPENAI_API_KEY` | ✓ | ✓ |
| `TAVILY_API_KEY` | ✓ | ✓ |
| `SESSION_SECRET` | ✓ strong random | ✓ strong random |
| `SPOTIFY_CLIENT_ID` / `SECRET` | ✓ | ✓ |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8013/auth/callback` (direct to API) | **Recommended (UI uses `/api/agent` proxy):** `https://<your-web-host>/api/agent/auth/callback` — must match Spotify Dashboard exactly. **Direct-browser-to-API mode:** `https://<api-host>/auth/callback` (see `docs/DEPLOYMENT.md`) |
| `FRONTEND_URL` | `http://127.0.0.1:3003` | `https://<your-web-host>` (no trailing slash) |
| `SESSION_COOKIE_SECURE` | unset / `0` | `1` (HTTPS only) |
| `CORS_ALLOW_ORIGINS` | `http://127.0.0.1:3003,http://localhost:3003` | Your UI origin if the browser calls the API directly; optional when all traffic is same-origin via Next proxy |
| `AGENT_API_URL` (`apps/web`, **server-only** on Vercel) | `http://127.0.0.1:8013` | `https://<your-api-host>` |
| `NEXT_PUBLIC_AGENT_API_BASE_URL` | `http://127.0.0.1:8013` | Same API URL (UI label + direct mode); keep behind proxy in production |
| `SUPABASE_*` | ✓ for tokens + memory | ✓ |
| `CHECKPOINT_DATABASE_URL` | optional | **recommended** (threads + HITL survive restarts / multi-worker) |

FastAPI loads repo-root `.env` automatically (including `CORS_ALLOW_ORIGINS`).

## Run locally

### One command (cleanup + both servers)

From repo root (after `pip install -e .` and `apps/web` has `npm install`):

```bash
chmod +x scripts/start-local.sh scripts/stop-local.sh   # once
./scripts/start-local.sh
# or: make start-local
```

This runs **`scripts/kill-dev-ports.sh`**, then starts **FastAPI on 8013** and **Next on 3003** in the **background**. Logs: **`.dev/api.log`** and **`.dev/web.log`**. Stop: **`./scripts/stop-local.sh`** or **`make stop-local`**.

Open **`http://127.0.0.1:3003`**.

### Two terminals (foreground, good for debugging)

```bash
make kill-ports   # optional: free 8013 / 3003
```

**Terminal A — API**

```bash
source .venv/bin/activate
PYTHONPATH=. uvicorn src.web.app:app --reload --host 0.0.0.0 --port 8013
```

**Terminal B — Web**

```bash
cd apps/web && npm run dev
```

Open **`http://127.0.0.1:3003`**.

**Verify API without the UI:**

```bash
./scripts/smoke.sh
# or
curl -s http://127.0.0.1:8013/health
```

```bash
# quick timing checks
curl -s -o /dev/null -w "health: %{time_total}s\n" http://127.0.0.1:8013/health
curl -s -o /dev/null -w "chat: %{time_total}s\n" \
  -H "Content-Type: application/json" \
  -d '{"from":"perf","body":"hello"}' \
  http://127.0.0.1:8013/chat
```

Set `CHAT_DEBUG_LOGS=1` in `.env` to emit per-chat timings to the API logs.
If model calls fail with proxy-related errors, set `DISABLE_OUTBOUND_PROXY=1` and restart the API.
Client requests include `x-request-id`; the API echoes and logs it for correlation.

### If you see `ERR_CONNECTION_REFUSED` on `:3003`

The **Next.js dev server is not running**. `uvicorn` alone does not serve port 3003 — start `npm run dev` in `apps/web`. The web UI also calls `GET /health` on the API and shows a red banner if the API is down.

### If dev “stalls” or servers exit immediately

Check **`.dev/api.log`** and **`.dev/web.log`** after `./scripts/start-local.sh`.

| Symptom | Cause | Fix |
|--------|--------|-----|
| `ModuleNotFoundError: No module named 'fastapi.openapi.models'` | Corrupt / partial **FastAPI** install in `.venv` | `make fix-api` or `./scripts/fix-dev-env.sh` |
| API takes **minutes** to answer `/health` the first time | Heavy Python import graph; **httpx** used to load eagerly via auth helpers | Fixed in current code (lazy `httpx` in `src/auth/spotify_auth.py`); `start-local` waits up to ~3 min |
| `TypeError: loadEnvConfig is not a function` (Next) | **Node &lt; 20.19** and/or broken **`node_modules`** | Use **Node ≥ 20.19** (see `apps/web/.nvmrc`), then `make fix-dev-env` or `cd apps/web && rm -rf node_modules && npm install` |
| `start-local.sh` exits before starting | Node version check failed | Upgrade Node, reinstall web deps, rerun |

### Spotify redirect URI (local)

Register **exactly**:

`http://127.0.0.1:8013/auth/callback`

Spotify blocks `http://localhost/...` for new apps. Use loopback IP. See [redirect URI docs](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri).

**Why `127.0.0.1` for the web URL too?** Browsers treat `localhost` and `127.0.0.1` as different sites; session cookies for the API (on `127.0.0.1`) must match how you open the UI. In dev, `apps/web/next.config.ts` redirects `Host: localhost:<port>` → `http://127.0.0.1:<port>/…` (production builds skip those rules).

## API endpoints (summary)

- `GET /health` — process up (no agent load)
- `GET /health/ready` — whether the DeepAgents worker has finished prewarm (`PREWARM_AGENT`)
- `POST /chat` — JSON `{"from":"…","body":"…"}` → `{ "reply", "tool_trace"[] }` (always JSON; `error` on failure)
- `GET /auth/spotify` → `{ auth_url }` — start OAuth
- `GET /auth/callback` — Spotify return URL
- `GET /auth/status` — logged-in user

## Optional: taste memory (“RAG”)

v1-lite works without this: the agent can answer from live Spotify reads alone. For extra personalization, apply `supabase/taste_memory.sql` so `spotify_ingest_taste_memory` / `spotify_retrieve_taste_memory` can embed short taste snippets (see `docs/V1_LITE_SPEC.md`).

### What is Vercel? Are we using it?

**Vercel** is a hosting company best known for deploying **Next.js** apps globally (CDN, HTTPS, preview URLs, custom domains). **We are not automatically on Vercel** — nothing deploys until you connect this repo (or `apps/web`) to a Vercel account and click deploy. The codebase is **structured** for that pattern: **Next UI** on Vercel (or any Node host) + **FastAPI** on a second host (Fly.io, Railway, Render, a VPS, etc.), with `AGENT_API_URL` / `NEXT_PUBLIC_AGENT_API_BASE_URL` pointing at your API.

`apps/web/vercel.json` raises **`maxDuration`** for the `/api/agent/*` proxy (long chat/SSE); plan limits still apply — see `docs/DEPLOYMENT.md`.

## Production goal: “anyone on any device”

Step-by-step checklist, env tables, Vercel limits, and **Spotify redirect URI** (including the **`/api/agent/auth/callback`** proxy pattern) are in **`docs/DEPLOYMENT.md`**.

Summary:

1. **HTTPS everywhere** for real users and Spotify.
2. **Spotify Dashboard** — redirect URI must match **`SPOTIFY_REDIRECT_URI`** (usually the **web app** URL + `/api/agent/auth/callback` when using the default Next proxy).
3. **Env** — `FRONTEND_URL`, **`AGENT_API_URL`** on Vercel, API secrets on the FastAPI host; **`SESSION_COOKIE_SECURE=1`** in production.
4. **Sessions** — signed cookies; **multiple API replicas** without sticky sessions need a shared store (Redis) — see Future TODO.
5. **Checkpoints** — `CHECKPOINT_DATABASE_URL` (Postgres) for durable chat + HITL.
6. **Custom domains** — DNS/SSL for both UI and API as needed.

## CLI (no web UI)

```bash
python -m src.main
python -m src.main "up and coming indie 2026"
```

## Performance notes

- **First chat can be slow** the first time the process loads LangChain / DeepAgents (large Python import graph). The API **prewarms the agent in the background by default** (`PREWARM_AGENT=1`); watch logs for `Agent prewarm finished`. Set `PREWARM_AGENT=0` if you want the lightest API startup and accept a slow first message.
- **Next.js dev** (`npm run dev`) binds **`127.0.0.1:3003`** to match API cookies.
- **`npm ci` / installs** are bounded by network and disk; use `npm install` after the first clone if `npm ci` feels heavy.

## LangSmith (optional, recommended)

LangChain/DeepAgents tracing is environment-driven:

- `LANGSMITH_TRACING=true`
- `LANGSMITH_API_KEY=...`
- `LANGSMITH_PROJECT=spotify-llm-dev`

Restart the API after setting these; new turns will appear in LangSmith with tool-level spans.


## Makefile

```text
make help         # list targets
make start-local  # API + Next in background (.dev/*.log)
make fix-api      # repair FastAPI/Starlette in .venv
make fix-dev-env  # fix-api + clean web node_modules
make stop-local   # stop start-local PIDs + free ports
make kill-ports   # ./scripts/kill-dev-ports.sh
make install      # pip install -e .
make install-web  # npm ci in apps/web
make check-imports
make smoke        # needs API running; override with API=https://...
```

## Future TODO

- Shared session store (Redis) + `SESSION_COOKIE_SECURE` defaults for multi-instance API.
- Observability: traces, tool error metrics.
- Ranking: blend memory + recency + diversity.
- Optional: auto-ingest taste memory once after first Spotify login.
