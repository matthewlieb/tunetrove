# Deploying spotify-llm for public users (any device)

This app is **two services**: a **Next.js** UI (ideal host: [Vercel](https://vercel.com)) and a **FastAPI** agent API (Fly.io, Railway, Render, a VPS, etc.). Browsers need **HTTPS** in production. Spotify requires a **registered redirect URI** that matches your deployment **exactly**.

## 1. Choose how the browser talks to the API

### A. Recommended: same-origin proxy (default)

The UI calls **`/api/agent/*`** on the **same hostname** as the site (e.g. `https://app.example.com/api/agent/chat`). Next.js forwards to FastAPI using **`AGENT_API_URL`** (server-only).

**Why:** The session cookie is set on **your site’s origin**. Spotify redirects the user back to that same origin for `/api/agent/auth/callback`, so login works on **phones and desktops** without cross-site cookies.

### B. Direct API (advanced)

Set **`NEXT_PUBLIC_USE_AGENT_PROXY=0`** and point **`NEXT_PUBLIC_AGENT_API_BASE_URL`** at the API. Then the browser calls the API **directly**. You must:

- Set **`CORS_ALLOW_ORIGINS`** on the API to your exact UI origin(s).
- Register Spotify redirect **`https://<api-host>/auth/callback`** (not the `/api/agent/...` path).
- Use **`SESSION_SAME_SITE=none`** and **`SESSION_COOKIE_SECURE=1`** so third-party cookies work (fragile on Safari). Prefer **pattern A** unless you know you need B.

---

## 2. Spotify Developer Dashboard

1. Open [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) → your app.
2. Add a redirect URI (must match **`SPOTIFY_REDIRECT_URI`** in the API env, character for character):

| Setup | Example redirect URI |
|--------|----------------------|
| **Proxy (recommended)** | `https://your-app.vercel.app/api/agent/auth/callback` |
| **Custom domain** | e.g. `https://tempotrove.com/api/agent/auth/callback` (must match `FRONTEND_URL` host) |
| **Direct API mode** | `https://api.example.com/auth/callback` |

3. Save. Spotify allows multiple URIs; keep your local `http://127.0.0.1:8013/auth/callback` for dev if you still use it.

### Production: OAuth sends you to `http://127.0.0.1:3003` (or other localhost)

After Spotify login, **`GET /auth/callback`** redirects to **`FRONTEND_URL`** (see `src/web/app.py`). If **Railway** still has **`FRONTEND_URL=http://127.0.0.1:3003`** from local dev, you will land on localhost even when you started from **`https://tempotrove.com`**.

**Fix on Railway (then redeploy the API):**

| Variable | Example for custom domain |
|----------|---------------------------|
| **`FRONTEND_URL`** | `https://tempotrove.com` (no trailing slash) |
| **`SPOTIFY_REDIRECT_URI`** | `https://tempotrove.com/api/agent/auth/callback` (must match Spotify Dashboard **exactly**) |
| **`CORS_ALLOW_ORIGINS`** | Include `https://tempotrove.com` if you use direct browser→API calls |
| **`SESSION_COOKIE_SECURE`** | `1` on HTTPS |

Then **log out** in the app (or clear cookies for both domains) and **Connect Spotify** again from **`https://tempotrove.com`**.

### Production: chat says “Spotify is not configured” but the sidebar shows you’re logged in

Spotify tools need a **stored token** in Supabase (**`spotify_users`** via `get_user_token`). If OAuth finished on the wrong host, or **`SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`** are missing on Railway, or the table was never created (**`supabase/taste_memory.sql`**), the UI session can look “logged in” while tools get **no token**. Fix **`FRONTEND_URL`** / redirect trio above, verify Supabase, and reconnect Spotify.

### Local dev: “Connect Spotify” does nothing or OAuth fails

1. **API must be running** (e.g. uvicorn on **port 8013** per README). The UI calls `/api/agent/auth/spotify` → FastAPI **`GET /auth/spotify`**. No API = failed fetch (you may see a red banner or an error line in chat).

2. **Redirect URI must match exactly** (Spotify Dashboard ↔ `SPOTIFY_REDIRECT_URI` in the API `.env`):

| Wrong (common mistake) | Right for default local API |
|------------------------|------------------------------|
| `http://127.0.0.1:8000/callback` | **`http://127.0.0.1:8013/auth/callback`** |
| `http://127.0.0.1:8000/api/callback` | Path must be **`/auth/callback`**, not `/api/callback` |
| Port **8000** | Port **8013** (or whatever you run uvicorn on — must match) |

3. If you use the **Next proxy** for OAuth in dev, register instead **`http://127.0.0.1:3003/api/agent/auth/callback`** and set **`SPOTIFY_REDIRECT_URI`** to that same string on the API.

4. **Website** in the Spotify app can be `http://127.0.0.1:3003` for development; it does not replace the redirect URI list.

---

## 3. Environment variables

### FastAPI (API host) — repo root `.env` or host secrets

| Variable | Production notes |
|----------|-------------------|
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | From Spotify app |
| `SPOTIFY_REDIRECT_URI` | **Must** match dashboard (see §2). With proxy: `https://<site>/api/agent/auth/callback` |
| `FRONTEND_URL` | **Public UI origin** (no trailing slash), e.g. `https://your-app.vercel.app` — used after OAuth |
| `SESSION_SECRET` | Long random string; **stable** across deploys |
| `SESSION_COOKIE_SECURE` | `1` when users only use HTTPS |
| `SESSION_SAME_SITE` | `lax` for proxy pattern (default); `none` only if you use direct cross-origin API + Secure |
| `CORS_ALLOW_ORIGINS` | Required for **direct** API mode; for proxy-only you can omit or set to your UI origin |
| `OPENAI_API_KEY` / `TAVILY_API_KEY` | Required for the agent |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Required for storing Spotify tokens (see repo auth code) |
| `USER_LLM_KEYS_FERNET_KEY` | **Optional but required for BYOK**: Fernet key so users can save their own OpenAI/Anthropic keys in the UI. Without it, the sidebar shows *BYOK not enabled*; host-paid **`OPENAI_API_KEY`** still works. |
| `CHECKPOINT_DATABASE_URL` | **Strongly recommended** for production (Postgres; Supabase pooler works) so chat threads survive restarts and HITL works reliably |
| `AGENT_TIMEOUT_SECONDS` | Keep aligned with your host’s max request time |

### Next.js (Vercel / Node host) — `apps/web` env

| Variable | Production notes |
|----------|-------------------|
| `AGENT_API_URL` | **Server-only**: `https://your-api.example.com` (no trailing slash). Vercel → your FastAPI URL |
| `NEXT_PUBLIC_USE_AGENT_PROXY` | `1` (default) for pattern A |
| `NEXT_PUBLIC_AGENT_API_BASE_URL` | Shown in UI debug line; set to same API URL or leave default when using proxy |
| `AGENT_API_FETCH_TIMEOUT_MS` | Default 600000 ms for chat routes; must be ≤ your **Vercel function max duration** (see §5) |

Do **not** put API keys in `NEXT_PUBLIC_*` vars (they are exposed to the browser).

---

## 4. Vercel project settings

1. **Root Directory:** `apps/web` (if the repo root is `spotify-llm`).
2. **Framework preset:** Next.js (auto).
3. **Environment variables:** Add the `apps/web` table above for Production (and Preview if you test OAuth there — add matching Spotify redirect for preview URL or use a branch domain).
4. **Build:** `npm run build` from `apps/web`.

The agent proxy App Route (`apps/web/app/api/agent/[[...path]]/route.ts`) exports **`maxDuration`** so long chat/SSE requests are less likely to be cut off (plan limits still apply). Avoid `vercel.json` → `functions` patterns for this: if Vercel misclassifies the project, those patterns are validated against the legacy `api/` folder and the build fails.

---

## 5. Limits and scaling

- **Vercel serverless:** Free/Hobby **`maxDuration`** tops out at **300s** on current plans. Long agent turns or **SSE** may still need **Pro** or moving heavy work to **Railway**. If you hit limits, keep the API on Railway and only proxy through Vercel, or use shorter turns / non-streaming.
- **FastAPI:** Run **one process** or use **sticky sessions** if you add replicas without a **shared checkpointer**; prefer **`CHECKPOINT_DATABASE_URL`** so any worker can resume threads.
- **Redis session store** is not implemented yet; multiple **stateless** API instances + signed cookies can desync in-memory session unless you move sessions to Redis (see README Future TODO).

---

## 6. Smoke checks after deploy

1. Open the **production UI** over **HTTPS**.
2. **Connect Spotify** → you should return to the same site with `?spotify_auth=success` and see your name.
3. Send a short chat message; confirm **Activity · tools** updates and no HTML error pages.
4. Optional: `curl -sS https://<api>/health` should return JSON `{"ok":true,...}`.

---

## 7. Mobile

The UI uses a responsive flex layout and viewport metadata. Use a real device or browser devtools device mode; if touch targets feel tight, adjust padding in `apps/web/app/page.tsx` later.

---

## Quick reference: proxy vs direct

```
Proxy (default):
  User → https://site.com/api/agent/auth/callback → Next → FastAPI /auth/callback
  SPOTIFY_REDIRECT_URI=https://site.com/api/agent/auth/callback
  Cookie host = site.com ✓

Direct:
  User → https://api.example.com/auth/callback
  SPOTIFY_REDIRECT_URI=https://api.example.com/auth/callback
  CORS + SameSite=None + Secure on API
```
