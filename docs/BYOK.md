# Bring-your-own API keys (BYOK) — design sketch

**Goal:** Let each signed-in user attach their own **OpenAI** or **Anthropic** key so **you** are not charged for every model call.

**Status:** Not implemented in v1-lite. This page is for when you’re ready to prioritize it.

## Constraints

- Keys are **highly sensitive**. Never log them, never return them in JSON, never commit them.
- **Server-stored keys** must be **encrypted at rest** (e.g. KMS, Supabase Vault, or app-level encryption with a master secret in env).
- **Browser-only keys** avoid server storage but are easier to steal via XSS and awkward for long SSE requests unless every call is proxied with the key in a header (still server sees key per request).

## Practical approaches

### A. Encrypted per-user secrets (recommended for SaaS)

1. Add columns e.g. `openai_key_encrypted`, `anthropic_key_encrypted` (or one JSON blob) on your user/profile table in **Supabase**, scoped by `spotify_user.id`.
2. Encrypt with a **server-only** master key (`USER_LLM_KEYS_SECRET` or KMS).
3. UI: “Settings → paste API key → Save” → `POST /api/user/llm-keys` on your API (session required).
4. In `create_deep_agent` / chat path, resolve **that user’s** decrypted key and bind `init_chat_model` for the request.
5. Rate-limit and cap usage per user to reduce abuse.

### B. OAuth-style “connect OpenAI” (if providers offer it)

Rare today for raw API keys; most flows still paste keys or use org billing.

### C. Pass-through billing (Stripe + your platform key)

You charge users a subscription and still use **your** key; simpler ops, different economics.

## Spotify dashboard

BYOK does **not** replace Spotify: users still connect **their** Spotify; you still store **their** refresh tokens (Supabase) under your Spotify app’s Client ID.

## Related

- **`docs/COSTS_AND_BILLING.md`** — who pays today.
- **`docs/ROADMAP.md`** — where BYOK fits in priorities.
