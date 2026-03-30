# TempoTrove — pre-launch checklist

Use this before pointing **tempotrove.com** at production traffic.

## Security & secrets

- [ ] **`SESSION_SECRET`** — long random string, stable across deploys (rotating logs everyone out).
- [ ] **`SESSION_COOKIE_SECURE=1`** and HTTPS on both UI and API.
- [ ] **`EXPOSE_INTERNAL_ERRORS`** unset in production (generic JSON errors to clients).
- [ ] No `.env` / keys in git; rotate anything that ever leaked (screenshots, logs, CI logs).
- [ ] **BYOK:** `USER_LLM_KEYS_FERNET_KEY` only on API; never in Vercel public env.
- [ ] **Supabase:** RLS/policies reviewed for `spotify_users` (tokens, encrypted LLM columns).

## Spotify & domains

- [ ] Spotify app **Redirect URIs** match **`SPOTIFY_REDIRECT_URI`** exactly (including `https://tempotrove.com/api/agent/auth/callback` if using the Next proxy).
- [ ] **`FRONTEND_URL`** = `https://tempotrove.com` (no trailing slash), aligned with Vercel + Cloudflare DNS.
- [ ] **`CORS_ALLOW_ORIGINS`** includes `https://tempotrove.com` if the browser ever calls the API directly.

## Infra

- [ ] **API** reachable at public URL; **`AGENT_API_URL`** on Vercel matches it.
- [ ] **`CHECKPOINT_DATABASE_URL`** set if you need durable threads + HITL across restarts.
- [ ] **Rate limiting** — Cloudflare WAF / rules or proxy limits on `/chat` and `/auth/*` before open marketing.
- [ ] **Multi-instance API:** shared session store if you scale past one worker (see README).

## Quality gates (automated)

- [ ] **GitHub Actions** green on `main` and `develop` (`pip-audit`, `npm audit`, Next build, lint).
- [ ] Optional: **LangSmith** tracing for first-week debugging.
- [ ] Optional: **Snyk** / Dependabot alerts enabled on the repo.

## LLM / product

- [ ] Smoke-test: connect Spotify, chat, playlist read/write, BYOK save (if enabled).
- [ ] Prompt-injection awareness: tools only expose what users should access; HITL on writes stays on for production if configured.
