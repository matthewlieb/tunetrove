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

## 4. Custom domain (tempotrove.com)

1. **Registrar (e.g. Cloudflare):** you own **tempotrove.com** — add DNS per Vercel’s instructions (usually CNAME `www` → `cname.vercel-dns.com`, and apex ALIAS/flattening or A records as Vercel documents).
2. Vercel project → **Domains** → add `tempotrove.com` / `www`.
3. Update **`FRONTEND_URL`**, **`SPOTIFY_REDIRECT_URI`**, and Spotify dashboard to the **same** public HTTPS URL pattern.
4. Ensure **`CORS_ALLOW_ORIGINS`** includes your production UI origin if you use direct API calls from the browser.
5. **`SESSION_COOKIE_SECURE=1`** on the API in production.
