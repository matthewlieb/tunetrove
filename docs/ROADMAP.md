# Roadmap (spotify-llm / TempoTrove)

Near-term items align with **`docs/DEPLOYMENT.md`** and **`docs/V1_LITE_SPEC.md`**.

## Done or in progress (check repo)

- Deep Agents + Spotify read/write + Tavily search + Next UI + API proxy.
- Human-in-the-loop for sensitive Spotify / filesystem tools.
- Production deployment docs (Vercel + separate API, Spotify redirect via `/api/agent/...`).

## Next steps (product)

1. **Production hardening** — `CHECKPOINT_DATABASE_URL`, `SESSION_SECRET`, `SESSION_COOKIE_SECURE=1`, monitor errors, optional LangSmith.
2. **Spotify “Extended Quota” / production mode** — Development-mode Spotify apps cap testers; for broad public login follow [Spotify developer policy](https://developer.spotify.com/policy) and dashboard guidance when you scale.
3. **BYOK (user LLM keys)** — See **`docs/BYOK.md`**.
4. **Branding** — `NEXT_PUBLIC_APP_NAME` (default **TempoTrove**), **tempotrove.com**, favicon.

## Future: WhatsApp / messaging

**Goal:** Inbound/outbound chat on WhatsApp (or similar) wired to the **same** agent + Spotify user mapping.

**Likely building blocks:**

- **Twilio WhatsApp API** (or Meta Cloud API) webhook → FastAPI route.
- Map **WhatsApp sender id** → **Spotify user** (linking flow: “reply with a code from the web app” or deep link once).
- Reuse **`POST /chat`** (or internal `run_chat`) with that user’s `thread_id` / session rules.
- **Out of scope for v1-lite** per `V1_LITE_SPEC.md`; treat as a **phase 2** channel.

**Docs to add when you start:** `docs/WHATSAPP.md` (webhook contract, env vars, Twilio sandbox steps).

## Engineering backlog (from README)

- Shared **Redis** (or similar) session store for **multi-instance** API without sticky sessions.
- Observability: metrics on tool errors and latency.
- Optional auto-ingest taste memory after first Spotify login.
