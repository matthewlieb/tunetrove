# spotify-llm v1-lite spec

## Product goal

Ship a lean, reliable music assistant where any user can sign in with Spotify, ask questions about their own library, create/modify playlists through chat, and get web-researched suggestions.

## In scope (must ship)

- Spotify OAuth login/logout for end users.
- User-scoped chat with persistent identity (`spotify_user.id`).
- Spotify read operations for personal context:
  - profile/status
  - playlists
  - saved tracks, top items, recently played
- Spotify write operations via agent:
  - create playlist
  - add tracks to playlist
  - save tracks when explicitly requested
- Deep research tool for music discovery, used as evidence in recommendations.
- Web UI + API usable from phone/laptop browsers over HTTPS in production.

## Out of scope (v1-lite non-goals)

- Twilio/WhatsApp webhook ingestion.
- Debug/diagnostic API endpoints as product features.
- Complex ranking pipelines beyond agent + tool composition.
- Multi-tenant org features, billing, social features.
- Full historical analytics dashboards.

## User stories

- As a user, I can connect my Spotify account and see that I am authenticated.
- As a user, I can ask "what should I listen to from my library?" and get grounded answers.
- As a user, I can ask to create a playlist and add tracks from recommendations.
- As a user, I can ask for "up-and-coming artists like X" and get researched suggestions.

## Functional requirements

### Auth + session

- `GET /auth/spotify` returns `auth_url`.
- `GET /auth/callback` validates state, exchanges code, stores token, sets session.
- `GET /auth/status` returns authenticated user (or unauthenticated state).
- `POST /auth/logout` clears session.
- Session cookie settings must support secure production deployment.

### Chat API

- `POST /chat` accepts JSON `{ from, body }`.
- Requires or infers user context from authenticated session.
- Always returns JSON shape:
  - success: `{ reply, tool_trace }`
  - failure: `{ reply, error: true, tool_trace }`
- Reasonable timeout behavior with informative errors.

### Research + Spotify actions

- Agent can call:
  - web music research tool(s)
  - Spotify read tool(s)
  - Spotify write tool(s)
- Agent must avoid silent writes on ambiguous user intent.
- Failed write operations must be reported clearly.

## Non-functional requirements (v1-lite)

- Reliability: healthy API route responds quickly (`/health`), cold-start behavior documented.
- Security: secrets only via env vars; no secrets in logs; OAuth state validation enabled.
- Observability: request IDs on API requests and structured error logging.
- Performance targets:
  - health endpoint p95 under 1s in normal conditions
  - auth start endpoint p95 under 5s
  - chat p95 under 20s for normal turns (excluding heavy model/provider incidents)

## Deployment baseline

- Frontend and API deployed separately over HTTPS.
- Spotify redirect URI matches **`SPOTIFY_REDIRECT_URI`** (with the default Next proxy, use `https://<web-host>/api/agent/auth/callback` — see **`docs/DEPLOYMENT.md`**).
- CORS and `FRONTEND_URL` configured when the browser calls the API directly; same-origin proxy reduces cookie friction.
- Supabase configured for token persistence; Postgres **`CHECKPOINT_DATABASE_URL`** recommended for production chat + HITL.

### Optional: taste embeddings

Run `supabase/taste_memory.sql` when you want vector-backed taste snippets (`spotify_ingest_taste_memory` / `spotify_retrieve_taste_memory`). v1-lite does not require it for library Q&A or playlist actions.

## API surface (v1-lite)

- `GET /`
- `GET /health`
- `GET /health/ready`
- `GET /auth/spotify`
- `GET /auth/callback`
- `GET /auth/status`
- `POST /auth/logout`
- `POST /chat`

## Acceptance checklist

- User can authenticate from multiple device types (mobile + desktop browser).
- Authenticated status is reflected in UI after callback.
- Chat returns library-aware recommendations for authenticated users.
- Playlist create/add flow succeeds end-to-end for authenticated users.
- Research-backed recommendations cite current/recent information from web search.
- Error states are user-readable and do not return HTML error pages to the web app.
