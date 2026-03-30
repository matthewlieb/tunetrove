"""Spotify OAuth + per-user token persistence utilities (Supabase-backed)."""

from __future__ import annotations

import json
import os
import secrets
from threading import Lock

# Lazy-import httpx inside call sites so `import src.web.app` stays fast.
# A top-level `import httpx` can pull a very large import graph (CLI helpers, etc.)
# and delay uvicorn bind by minutes on some machines.


_EPHEMERAL_USER_TOKENS: dict[str, dict] = {}
_EPHEMERAL_USER_PROFILES: dict[str, dict] = {}
_ephemeral_lock = Lock()


def _cache_user_state(user: dict, token_info: dict | None = None) -> None:
    user_id = (user.get("id") or "").strip()
    if not user_id:
        return
    profile = {
        "id": user_id,
        "display_name": user.get("display_name", ""),
        "email": user.get("email", ""),
    }
    with _ephemeral_lock:
        _EPHEMERAL_USER_PROFILES[user_id] = profile
        if token_info:
            _EPHEMERAL_USER_TOKENS[user_id] = dict(token_info)


def _cached_user_token(user_id: str) -> dict | None:
    with _ephemeral_lock:
        tok = _EPHEMERAL_USER_TOKENS.get(user_id)
        return dict(tok) if isinstance(tok, dict) else None


def _cached_user_profile(user_id: str) -> dict | None:
    with _ephemeral_lock:
        prof = _EPHEMERAL_USER_PROFILES.get(user_id)
        return dict(prof) if isinstance(prof, dict) else None


def _supabase_url() -> str:
    return (
        os.environ.get("SUPABASE_URL")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        or ""
    ).strip()


def _supabase_key() -> str:
    return (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()


def _headers() -> dict[str, str]:
    url = _supabase_url()
    key = _supabase_key()
    if not url or not key:
        raise ValueError(
            "Supabase not configured for auth storage. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _scope() -> str:
    return (
        "playlist-read-private playlist-read-collaborative "
        "playlist-modify-public playlist-modify-private "
        "user-library-read user-library-modify "
        "user-read-recently-played user-top-read user-follow-read user-read-email user-read-private"
    )


def get_oauth():
    from spotipy.cache_handler import MemoryCacheHandler
    from spotipy.oauth2 import SpotifyOAuth

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8013/auth/callback")
    if not client_id or not client_secret:
        raise ValueError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are required")
    show_dialog = (os.environ.get("SPOTIFY_SHOW_DIALOG") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=_scope(),
        show_dialog=show_dialog,
        # Do not use a shared file cache on the API server; otherwise one
        # user's cached token can be reused for another user's callback.
        cache_handler=MemoryCacheHandler(),
        open_browser=False,
    )


def make_state() -> str:
    return secrets.token_urlsafe(32)


def save_user_token(user: dict, token_info: dict) -> None:
    import httpx

    user_id = user.get("id")
    if not user_id:
        raise ValueError("Spotify user id missing")
    # Keep a process-local fallback so login and tool calls can continue even
    # if Supabase is temporarily unavailable.
    _cache_user_state(user, token_info)

    if not _supabase_url().strip() or not _supabase_key():
        return
    url = _supabase_url().rstrip("/")
    headers = _headers()
    payload = {
        "user_id": user_id,
        "display_name": user.get("display_name", ""),
        "email": user.get("email", ""),
        "token_json": token_info,
    }
    try:
        resp = httpx.post(
            f"{url}/rest/v1/spotify_users?on_conflict=user_id",
            headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
    except Exception:
        # Keep login/session alive even if persistent storage is temporarily down.
        return


def get_user_token(user_id: str) -> dict | None:
    import httpx

    cached = _cached_user_token(user_id)
    if not _supabase_url().strip() or not _supabase_key():
        return cached
    url = _supabase_url().rstrip("/")
    try:
        headers = _headers()
        resp = httpx.get(
            f"{url}/rest/v1/spotify_users",
            headers=headers,
            params={"select": "token_json", "user_id": f"eq.{user_id}", "limit": "1"},
            timeout=30.0,
        )
        resp.raise_for_status()
        rows = resp.json() or []
        if not rows:
            return cached
        val = rows[0].get("token_json")
        if isinstance(val, str):
            out = json.loads(val)
        else:
            out = val
        if isinstance(out, dict):
            with _ephemeral_lock:
                _EPHEMERAL_USER_TOKENS[user_id] = dict(out)
            return out
        return cached
    except Exception:
        return cached


def get_user_profile(user_id: str) -> dict | None:
    import httpx

    cached = _cached_user_profile(user_id)
    if not _supabase_url().strip() or not _supabase_key():
        return cached
    url = _supabase_url().rstrip("/")
    try:
        headers = _headers()
        resp = httpx.get(
            f"{url}/rest/v1/spotify_users",
            headers=headers,
            params={
                "select": "user_id,display_name,email",
                "user_id": f"eq.{user_id}",
                "limit": "1",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        rows = resp.json() or []
        if not rows:
            return cached
        row = rows[0]
        out = {
            "id": row.get("user_id", ""),
            "display_name": row.get("display_name", ""),
            "email": row.get("email", ""),
        }
        with _ephemeral_lock:
            _EPHEMERAL_USER_PROFILES[user_id] = dict(out)
        return out
    except Exception:
        return cached


def delete_user(user_id: str) -> None:
    import httpx

    with _ephemeral_lock:
        _EPHEMERAL_USER_TOKENS.pop(user_id, None)
        _EPHEMERAL_USER_PROFILES.pop(user_id, None)

    if not _supabase_url().strip() or not _supabase_key():
        return
    url = _supabase_url().rstrip("/")
    headers = _headers()
    resp = httpx.delete(
        f"{url}/rest/v1/spotify_users",
        headers={**headers, "Prefer": "return=minimal"},
        params={"user_id": f"eq.{user_id}"},
        timeout=30.0,
    )
    resp.raise_for_status()

