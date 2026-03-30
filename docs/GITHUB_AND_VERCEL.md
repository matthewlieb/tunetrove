# GitHub + Vercel (quick path)

## 1. Create a GitHub repository

**Recommended repo name:** `tempotrove` (matches **tempotrove.com** and product name **TempoTrove**). Alternatives: `spotify-deepagent`, `spotify-llm`.

**Important:** This app should be its **own** Git repository. If `spotify-llm` currently lives **inside** a bigger folder that is *already* a git repo (e.g. a `Projects` directory), `git` will use that **parent** `.git` until you isolate this project.

**Safest path:**

1. Copy or move the **`spotify-llm`** folder to a new location (e.g. `~/code/tempotrove`) **outside** any other git repo.
2. There, run:

```bash
cd ~/code/tempotrove   # or your path
git init
git add .
git commit -m "Initial commit: TempoTrove / spotify-llm v1-lite"
git branch -M main
git remote add origin https://github.com/<you>/tempotrove.git
git push -u origin main
git checkout -b develop
git push -u origin develop
```

3. On GitHub: **New repository** → create empty `tempotrove` → use the URL above as `origin`.  
   **Renaming an existing repo:** Settings → General → Repository name → `tempotrove`, then locally:  
   `git remote set-url origin https://github.com/<you>/tempotrove.git`

If the folder must stay nested, use **git subtree** or consult [GitHub docs on splitting history](https://docs.github.com/en/get-started/using-git/about-git-subtree-merges) — avoid `git add` from the wrong directory, which can stage sibling projects.

**Never commit:** `.env`, `apps/web/.env.local`, or any file with API keys. They are listed in `.gitignore`. If you ever committed `.env.local`, run `git rm --cached apps/web/.env.local` and rotate keys.

## 2. Connect Vercel to GitHub

1. Vercel → **Add New…** → **Project** → **Install** the GitHub app if prompted.
2. **Import** your repository.
3. **Root Directory:** `apps/web` (critical when the repo root is `spotify-llm` and Next lives under `apps/web`).
4. **Environment variables** (Production): set at least **`AGENT_API_URL`** = your public FastAPI `https://...` origin (no trailing slash). See **`docs/DEPLOYMENT.md`** for the full list.
5. Deploy. Use branch **`main`** for production; optional **Preview** deployments on **`develop`** or PRs (Vercel project settings).

## 3. Spotify Developer Dashboard (production)

Add a redirect URI that matches **`SPOTIFY_REDIRECT_URI`** on the API:

- **With default Next proxy:**  
  `https://<your-vercel-host>/api/agent/auth/callback`  
  (or `https://tempotrove.com/api/agent/auth/callback` once the custom domain is live.)

Your local URIs (`127.0.0.1:8000`, etc.) can stay for other experiments, but **this app’s** local dev is **`http://127.0.0.1:8013/auth/callback`** when the browser hits the API directly — align ports and paths with your actual run config.

## 4. Custom domain (tempotrove.com + Cloudflare + Vercel)

Do this **after** the project deploys once on Vercel (you can use the default `*.vercel.app` URL first).

### 4a. Add the domain in Vercel

1. Vercel → your project → **Settings** → **Domains**.
2. Add **`tempotrove.com`** and **`www.tempotrove.com`** (add both so you can redirect one to the other).
3. Vercel shows **exact** DNS records to create. Keep that tab open.

Typical pattern (confirm against Vercel’s UI — values can change):

| Record | Name | Target / value | Notes |
|--------|------|----------------|--------|
| **A** or **ALIAS** | `@` (apex) | Vercel’s IPs or “flattened” CNAME | Vercel documents apex for Cloudflare |
| **CNAME** | `www` | `cname.vercel-dns.com` (or value Vercel shows) | Often easiest path |

[Vercel: working with DNS](https://vercel.com/docs/domains/working-with-dns) is authoritative if anything below disagrees.

### 4b. DNS in Cloudflare

1. Cloudflare → **tempotrove.com** → **DNS** → **Records**.
2. Create the records **exactly** as Vercel lists (name/host, type, value).
3. **Proxy status:** for the records pointing at Vercel, start with **DNS only** (grey cloud) until SSL validates; you can try **Proxied** (orange) later — if you see redirect loops or SSL errors, switch to DNS-only for those hostnames.
4. Wait for propagation (often minutes; TTL applies).

### 4c. Pick the canonical site URL

Choose **one** public URL (recommended: **`https://tempotrove.com`** with `www` redirecting to apex, or the reverse — just be consistent).

Then align **everywhere**:

| Place | Set to |
|-------|--------|
| **FastAPI `FRONTEND_URL`** | `https://tempotrove.com` (no trailing slash) |
| **FastAPI `SPOTIFY_REDIRECT_URI`** | `https://tempotrove.com/api/agent/auth/callback` (with Next proxy) |
| **Spotify Developer Dashboard** | Same redirect URI, **character-for-character** |
| **Vercel** | Primary domain = your canonical host; redirect the other hostname to it (Vercel domain settings). |
| **`CORS_ALLOW_ORIGINS`** | Include `https://tempotrove.com` if the browser ever calls the API directly; optional when all traffic is same-origin via `/api/agent`. |

### 4d. API env reminder

- **`SESSION_COOKIE_SECURE=1`** in production.
- Redeploy API and Vercel (or clear config cache) after changing `FRONTEND_URL` / redirect URI.

---

## 5. What you need besides Vercel + Supabase

| Piece | Role | Typical provider |
|-------|------|------------------|
| **Vercel** | Hosts **Next.js** (`apps/web`) | Vercel |
| **Supabase** | Postgres, Spotify tokens, BYOK rows, optional vectors | Supabase |
| **FastAPI host** | Runs **`src/web/app.py`** (agent, `/chat`, OAuth, sessions) | **Required — not on Vercel.** [Fly.io](https://fly.io), [Railway](https://railway.app), [Render](https://render.com), VPS, etc. |
| **Spotify Developer app** | OAuth client id/secret | [Spotify Dashboard](https://developer.spotify.com/dashboard) (no hosting bill) |
| **LLM** | Chat | **OpenAI** and/or **Anthropic** API keys (or user BYOK + your Fernet key) |
| **Tavily** | Web search tool | [Tavily](https://tavily.com) API key |
| **Domain DNS** | `tempotrove.com` → Vercel | Cloudflare (you already have the domain) |

Optional: **LangSmith** tracing, **Cloudflare** WAF/rate limits in front of the site, separate monitoring. You do **not** need another database product if Supabase covers app data + `CHECKPOINT_DATABASE_URL` (can be the same Supabase Postgres connection string).
