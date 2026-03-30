"""Tool exports.

Keep this module lightweight: avoid importing heavyweight tool modules at import
time, because `src.web.app` imports `src.tools.spotify_context` and Python loads
`src.tools.__init__` first. Lazy resolution here prevents slow API startup.
"""

from __future__ import annotations

__all__ = [
    "music_web_search_tool",
    "spotify_search_tracks",
    "spotify_list_playlists",
    "spotify_create_playlist",
    "spotify_add_to_playlist",
    "spotify_save_tracks",
    "get_spotify_tools",
]


def __getattr__(name: str):
    if name == "music_web_search_tool":
        from .tavily_tools import music_web_search_tool

        return music_web_search_tool

    if name in {
        "spotify_search_tracks",
        "spotify_list_playlists",
        "spotify_create_playlist",
        "spotify_add_to_playlist",
        "spotify_save_tracks",
        "get_spotify_tools",
    }:
        from . import spotify_tools as _spotify_tools

        return getattr(_spotify_tools, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
