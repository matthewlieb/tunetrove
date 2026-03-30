"""Spotify OAuth + per-user token persistence utilities (Supabase-backed)."""

from __future__ import annotations

import json
import os
import secrets

# Lazy-import httpx inside call sites so `import src.web.app` stays fast.
# A top-level `import httpx` can pull a very large import graph (CLI helpers, etc.)
# and delay uvicorn bind by minutes on some machines.


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
    from spotipy.oauth2 import SpotifyOAuth

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8013/auth/callback")
    if not client_id or not client_secret:
        raise ValueError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are required")
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=_scope(),
        open_browser=False,
    )


def make_state() -> str:
    return secrets.token_urlsafe(32)


def save_user_token(user: dict, token_info: dict) -> None:
    import httpx

    user_id = user.get("id")
    if not user_id:
        raise ValueError("Spotify user id missing")
    url = _supabase_url().rstrip("/")
    headers = _headers()
    payload = {
        "user_id": user_id,
        "display_name": user.get("display_name", ""),
        "email": user.get("email", ""),
        "token_json": token_info,
    }
    resp = httpx.post(
        f"{url}/rest/v1/spotify_users?on_conflict=user_id",
        headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()


def get_user_token(user_id: str) -> dict | None:
    import httpx

    if not _supabase_url().strip() or not _supabase_key():
        return None
    url = _supabase_url().rstrip("/")
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
        return None
    val = rows[0].get("token_json")
    if isinstance(val, str):
        return json.loads(val)
    return val


def get_user_profile(user_id: str) -> dict | None:
    import httpx

    if not _supabase_url().strip() or not _supabase_key():
        return None
    url = _supabase_url().rstrip("/")
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
        return None
    row = rows[0]
    return {
        "id": row.get("user_id", ""),
        "display_name": row.get("display_name", ""),
        "email": row.get("email", ""),
    }


def delete_user(user_id: str) -> None:
    import httpx

    url = _supabase_url().rstrip("/")
    headers = _headers()
    resp = httpx.delete(
        f"{url}/rest/v1/spotify_users",
        headers={**headers, "Prefer": "return=minimal"},
        params={"user_id": f"eq.{user_id}"},
        timeout=30.0,
    )
    resp.raise_for_status()

