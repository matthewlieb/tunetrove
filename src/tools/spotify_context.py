"""Lightweight Spotify user context for request/thread scoping.

Kept separate from `spotify_tools` so FastAPI can import this without pulling
LangChain / tool definitions (faster cold start, smaller import graph).
"""

from __future__ import annotations

from contextvars import ContextVar

_CURRENT_USER_ID: ContextVar[str | None] = ContextVar("spotify_current_user_id", default=None)
# When True, allow SpotifyClient() developer OAuth fallback (CLI / local scripts only). Web chat keeps False.
_ALLOW_ANONYMOUS_SPOTIFY: ContextVar[bool] = ContextVar("spotify_allow_anonymous", default=False)


def set_spotify_user_context(user_id: str | None) -> None:
    _CURRENT_USER_ID.set(user_id.strip() if user_id else None)


def get_spotify_user_context() -> str | None:
    return _CURRENT_USER_ID.get()


def set_spotify_anonymous_allowed(allowed: bool) -> None:
    _ALLOW_ANONYMOUS_SPOTIFY.set(allowed)


def get_spotify_anonymous_allowed() -> bool:
    return _ALLOW_ANONYMOUS_SPOTIFY.get()
