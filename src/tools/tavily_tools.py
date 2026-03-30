"""Web search for music discovery via Tavily."""

import os
from langchain_tavily import TavilySearch


MUSIC_WEB_SEARCH_DESCRIPTION = (
    "Search the web for music-related info: artists, genres, up-and-coming acts, "
    "reviews, or mood-based recommendations. Use for discovering new music, similar artists, "
    "or context about bands and releases. Input is a search query. "
    "When you answer the user, only cite URLs returned in this tool's results—do not invent links."
)


DEFAULT_INCLUDE_DOMAINS = [
    "spotify.com",
    "pitchfork.com",
    "bandcamp.com",
    "soundcloud.com",
    "npr.org",
    "rollingstone.com",
    "billboard.com",
    "reddit.com",
    "rateyourmusic.com",
    "allmusic.com",
    "last.fm",
    "musicboard.app",
    "residentadvisor.net",
    "stereogum.com",
    "nme.com",
]


def _include_domains() -> list[str]:
    raw = (os.environ.get("MUSIC_SEARCH_INCLUDE_DOMAINS") or "").strip()
    if not raw:
        return DEFAULT_INCLUDE_DOMAINS
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or DEFAULT_INCLUDE_DOMAINS


def music_web_search_tool():
    """Return a Tavily search tool configured for music discovery. Requires TAVILY_API_KEY."""
    if not os.environ.get("TAVILY_API_KEY"):
        raise ValueError("TAVILY_API_KEY is not set")
    return TavilySearch(
        name="music_web_search",
        description=MUSIC_WEB_SEARCH_DESCRIPTION,
        max_results=10,
        search_depth="advanced",
        include_domains=_include_domains(),
    )
