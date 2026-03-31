"""Spotify tools: search, playlist actions, and taste-profile memory."""

import logging
import os
from typing import Annotated, Optional

from langchain_core.tools import BaseTool, InjectedToolArg, tool
from src.auth.spotify_auth import get_oauth, get_user_token, save_user_token
from src.tools.spotify_context import (
    get_spotify_anonymous_allowed,
    get_spotify_user_context,
    set_spotify_user_context,
)
from src.tools.taste_memory import MemoryDoc, ingest_memory_docs

_LOG = logging.getLogger(__name__)

# Injected by get_spotify_tools() on each invoke — not visible to the LLM. ContextVar alone
# is unreliable when the runtime executes tools on worker threads (no inherited context).
SpotifyBoundUserId = Annotated[Optional[str], InjectedToolArg]

# Shown when Spotify returns 403 for a user who completed OAuth but is not allowlisted
# (development mode). See https://developer.spotify.com/documentation/web-api/concepts/quota-modes
SPOTIFY_DEV_MODE_403_MESSAGE = (
    "Spotify API returned 403 Forbidden for this account. In Development mode, each user "
    "must be added under Developer Dashboard → your app → Settings → Users Management "
    "(name + Spotify email, up to 5 users). Login can succeed without allowlisting, but "
    "API calls fail until the user is added. "
    "https://developer.spotify.com/documentation/web-api/concepts/quota-modes"
)


class SpotifyAppAccessError(Exception):
    """Spotify rejected API access for this user (e.g. development mode allowlist)."""


class SpotifyClient:
    def __init__(self, access_token: str | None = None, user_id: str | None = None):
        import spotipy
        self._sp = None
        self._auth_user_id = user_id

        # Priority 1: Use the explicit access token passed from the session/factory
        if access_token:
            self._sp = spotipy.Spotify(auth=access_token)
            try:
                # Validate token immediately
                profile = self._sp.current_user()
                if user_id and profile.get("id") != user_id:
                    # Never execute writes with a token from another account.
                    self._refresh_and_reinit(user_id)
            except spotipy.exceptions.SpotifyException as e:
                if e.http_status == 403:
                    raise SpotifyAppAccessError(SPOTIFY_DEV_MODE_403_MESSAGE) from e
                if e.http_status == 401:
                    # Token expired; attempt refresh if user_id is known
                    if user_id:
                        self._refresh_and_reinit(user_id)
                    else:
                        raise ValueError("Spotify session expired. Please re-authenticate.")
                raise

        # Priority 2: Fallback to database lookup if only user_id is provided
        elif user_id:
            self._refresh_and_reinit(user_id)

        # Final Fallback: CLI / Anonymous mode (Only if allowed)
        if self._sp is None and get_spotify_anonymous_allowed():
            client_id = os.environ.get("SPOTIFY_CLIENT_ID")
            client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
            if not client_id or not client_secret:
                raise ValueError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET required for fallback.")
            self._sp = spotipy.Spotify(auth_manager=get_oauth())

        if self._sp is None:
            raise ValueError("No valid Spotify session found for this user. Please connect Spotify.")

    def _refresh_and_reinit(self, user_id: str):
        """Load a valid token for the user, refreshing only when needed."""
        import spotipy
        token_info = get_user_token(user_id)
        if not token_info:
            raise ValueError(f"No stored Spotify token for user {user_id}.")

        cached_access = token_info.get("access_token")
        if isinstance(cached_access, str) and cached_access.strip():
            try:
                temp_sp = spotipy.Spotify(auth=cached_access.strip())
                user_data = temp_sp.current_user()
                if user_data.get("id") == user_id:
                    self._sp = temp_sp
                    return
            except spotipy.exceptions.SpotifyException as e:
                if e.http_status == 403:
                    raise SpotifyAppAccessError(SPOTIFY_DEV_MODE_403_MESSAGE) from e
                pass

        refresh_token = token_info.get("refresh_token")
        if not refresh_token:
            raise ValueError(f"No stored Spotify refresh token for user {user_id}.")
        oauth = get_oauth()
        new_info = oauth.refresh_access_token(refresh_token)

        # Initialize with refreshed token and verify the owner matches.
        temp_sp = spotipy.Spotify(auth=new_info["access_token"])
        try:
            user_data = temp_sp.current_user()
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 403:
                raise SpotifyAppAccessError(SPOTIFY_DEV_MODE_403_MESSAGE) from e
            raise
        if user_data.get("id") != user_id:
            raise ValueError("Spotify token owner mismatch. Please reconnect Spotify.")

        # Persist updated token back to database.
        save_user_token(user_data, new_info)
        self._sp = temp_sp

    # --- Wrapper API Methods ---
    def search_tracks(self, q: str, limit: int = 10):
        return self._sp.search(q=q, type="track", limit=limit).get("tracks", {}).get("items", [])

    def search_artists(self, q: str, limit: int = 5):
        return self._sp.search(q=q, type="artist", limit=limit).get("artists", {}).get("items", [])

    def artist_top_tracks(self, artist_id: str, market: str = "US"):
        return self._sp.artist_top_tracks(artist_id=artist_id, country=market).get("tracks", [])

    def add_to_playlist(self, playlist_id: str, track_uris: list[str]):
        self._sp.playlist_add_items(playlist_id, track_uris)

    def save_tracks(self, track_uris: list[str]):
        self._sp.current_user_saved_tracks_add(track_uris)

    def list_playlists(self, limit: int = 20):
        return self._sp.current_user_playlists(limit=limit).get("items", [])

    def create_playlist(self, name: str, description: str = "", public: bool = True):
        user_id = self._sp.me()["id"]
        pl = self._sp.user_playlist_create(user_id, name, public=public, description=description)
        return pl.get("id", ""), pl.get("uri", "")

    def current_user_id(self) -> str:
        import spotipy

        try:
            return self._sp.me().get("id", "unknown")
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 403:
                raise SpotifyAppAccessError(SPOTIFY_DEV_MODE_403_MESSAGE) from e
            raise

def _get_client(
    access_token: str | None = None,
    user_id: str | None = None,
) -> tuple[Optional[SpotifyClient], Optional[str]]:
    """Return (client, None) or (None, user-facing error message)."""
    ctx_user_id = get_spotify_user_context()
    current_user_id = (
        user_id.strip()
        if isinstance(user_id, str) and user_id.strip()
        else (ctx_user_id.strip() if isinstance(ctx_user_id, str) and ctx_user_id.strip() else None)
    )
    try:
        return SpotifyClient(access_token=access_token, user_id=current_user_id), None
    except SpotifyAppAccessError as e:
        return None, str(e)
    except ValueError as e:
        msg = str(e)
        if "No stored Spotify token" in msg or "No stored Spotify refresh token" in msg:
            return (
                None,
                "Spotify authorization was found, but no usable token is stored for this account. "
                "Please click Connect Spotify again to refresh your token.",
            )
        if "No valid Spotify session found" in msg:
            return None, "No valid Spotify session found. Please connect Spotify again."
        if "Spotify session expired" in msg or "re-authenticate" in msg.lower():
            return None, f"{msg} Try Connect Spotify again."
        return None, msg
    except Exception as e:
        _LOG.warning("Spotify _get_client failed: %s", e, exc_info=True)
        return (
            None,
            f"Could not open a Spotify API client ({e!s}). Try reconnecting Spotify or retry shortly.",
        )


def _spotify_tool_error_detail(e: BaseException) -> str:
    import spotipy

    if isinstance(e, spotipy.exceptions.SpotifyException) and e.http_status == 403:
        return SPOTIFY_DEV_MODE_403_MESSAGE
    return str(e)


def _session_user_id() -> str | None:
    uid = get_spotify_user_context()
    if isinstance(uid, str) and uid.strip():
        return uid.strip()
    return None


def _require_session_user_match(
    client: SpotifyClient,
    spotify_bound_user_id: str | None = None,
) -> str | None:
    """Return an error string when session user and token owner differ."""
    expected_user_id = (
        spotify_bound_user_id.strip()
        if isinstance(spotify_bound_user_id, str) and spotify_bound_user_id.strip()
        else _session_user_id()
    )
    if not expected_user_id:
        return "Spotify session missing. Please connect Spotify for this user first."
    try:
        actual_user_id = client.current_user_id()
    except SpotifyAppAccessError as e:
        return str(e)
    except Exception:
        return "Could not verify Spotify account for this session. Please reconnect Spotify."
    if actual_user_id != expected_user_id:
        return (
            "Spotify account mismatch detected. "
            "Please reconnect Spotify, then try again."
        )
    return None

# --- Helper Summarizers ---
def _track_summary(t):
    """Formats track data for clear tool output."""
    name = t.get("name", "?")
    artists = ", ".join(a.get("name", "") for a in t.get("artists", []))
    uri = t.get("uri", "")
    return {"name": name, "artists": artists, "uri": uri}

def _artist_summary(a):
    """Formats artist data for clear tool output."""
    return {
        "id": a.get("id", ""),
        "name": a.get("name", "?"),
        "genres": ", ".join(a.get("genres", [])),
    }

def _track_id_from_uri(uri: str) -> str:
    """Extracts a Spotify ID from various URI formats."""
    if uri.startswith("spotify:track:"):
        return uri.split(":")[-1]
    if "track/" in uri:
        return uri.rsplit("track/", 1)[-1].split("?")[0]
    return uri

# --- Tool Definitions ---

@tool
def spotify_search_tracks(
    query: str,
    limit: int = 10,
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """Search Spotify for tracks by song name, artist, or genre. Returns track names, artists, and URIs."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing. Please authenticate."
    try:
        items = client.search_tracks(query, limit=limit)
        if not items:
            return "No tracks found."
        lines = []
        for t in items:
            s = _track_summary(t)
            lines.append(f"{s['name']} — {s['artists']} | URI: {s['uri']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Spotify search failed: {_spotify_tool_error_detail(e)}"

@tool
def spotify_search_artists(
    query: str,
    limit: int = 5,
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """Search Spotify artists by name. Returns artist IDs and Genres."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing."
    try:
        items = client.search_artists(query, limit=limit)
        if not items:
            return "No artists found."
        lines = []
        for a in items:
            s = _artist_summary(a)
            lines.append(f"{s['name']} | ID: {s['id']} | Genres: {s['genres']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Spotify artist search failed: {_spotify_tool_error_detail(e)}"

@tool
def spotify_get_artist_top_tracks(
    artist_id: str,
    market: str = "US",
    limit: int = 5,
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """Get top tracks for a specific artist ID. Use after searching to find the right artist."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing."
    try:
        items = client.artist_top_tracks(artist_id=artist_id, market=market)[: max(1, min(limit, 10))]
        if not items:
            return "No top tracks found for that artist."
        lines = []
        for t in items:
            s = _track_summary(t)
            lines.append(f"{s['name']} — {s['artists']} | URI: {s['uri']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch artist top tracks: {_spotify_tool_error_detail(e)}"

@tool
def spotify_list_playlists(
    limit: int = 20,
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """List the user's Spotify playlists. Returns name and ID. Use this when picking a playlist to add tracks to."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing. Please authenticate."
    try:
        items = client.list_playlists(limit=limit)
        if not items:
            return "No playlists found in your account."
        lines = []
        for p in items:
            name = p.get("name", "?")
            pid = p.get("id", "")
            lines.append(f"{name} | ID: {pid}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to list playlists: {_spotify_tool_error_detail(e)}"

@tool
def spotify_create_playlist(
    name: str,
    description: str = "",
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """Create a new Spotify playlist for the user. Returns the new playlist ID for use with spotify_add_to_playlist."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing."
    mismatch = _require_session_user_match(client, spotify_bound_user_id)
    if mismatch:
        return mismatch
    try:
        pid, uri = client.create_playlist(name=name, description=description)
        return f"Successfully created playlist '{name}'. ID: {pid}"
    except Exception as e:
        return f"Failed to create playlist: {_spotify_tool_error_detail(e)}"

@tool
def spotify_add_to_playlist(
    playlist_id: str,
    track_uris: str,
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """Add tracks to a playlist. track_uris is a comma-separated list of spotify:track:... URIs."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing."
    mismatch = _require_session_user_match(client, spotify_bound_user_id)
    if mismatch:
        return mismatch
    try:
        uris = [u.strip() for u in track_uris.split(",") if u.strip()]
        if not uris:
            return "No track URIs provided."
        # Clean the ID in case a full URI was passed
        clean_id = playlist_id.replace("spotify:playlist:", "").strip()
        client.add_to_playlist(clean_id, uris)
        return f"Successfully added {len(uris)} track(s) to the playlist."
    except Exception as e:
        return f"Failed to add to playlist: {_spotify_tool_error_detail(e)}"

@tool
def spotify_save_tracks(
    track_uris: str,
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """Save tracks to the user's Spotify library (Liked Songs). track_uris is a comma-separated list of URIs."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing."
    mismatch = _require_session_user_match(client, spotify_bound_user_id)
    if mismatch:
        return mismatch
    try:
        uris = [u.strip() for u in track_uris.split(",") if u.strip()]
        if not uris:
            return "No track URIs provided."
        client.save_tracks(uris)
        return f"Saved {len(uris)} track(s) to your 'Liked Songs' library."
    except Exception as e:
        return f"Failed to save tracks to library: {_spotify_tool_error_detail(e)}"

@tool
def spotify_get_recently_played(
    limit: int = 20,
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """Get the user's recently played tracks. Useful for current taste signals."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing."
    try:
        items = client._sp.current_user_recently_played(limit=limit).get("items", [])
        if not items:
            return "No recently played tracks found."
        lines = []
        for row in items:
            tr = row.get("track", {})
            s = _track_summary(tr)
            lines.append(f"{s['name']} — {s['artists']} | URI: {s['uri']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch recently played: {_spotify_tool_error_detail(e)}"

@tool
def spotify_build_library_profile(
    limit: int = 20,
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """Build a concise snapshot of user taste from top tracks/artists and recent listening."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing."
    try:
        top_tracks = client._sp.current_user_top_tracks(limit=limit).get("items", [])
        top_artists = client._sp.current_user_top_artists(limit=limit).get("items", [])
        recent = client._sp.current_user_recently_played(limit=limit).get("items", [])
        
        lines = [f"Spotify user: {client.current_user_id()}", "\nTop artists:"]
        lines.extend([f"- {a.get('name')} ({', '.join(a.get('genres', []))})" for a in top_artists[:10]])
        lines.append("\nTop tracks:")
        lines.extend([f"- {t.get('name')} — {', '.join(a.get('name') for a in t.get('artists', []))}" for t in top_tracks[:10]])
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to build profile: {_spotify_tool_error_detail(e)}"

@tool
def spotify_ingest_taste_memory(
    user_id: str = "default",
    access_token: str = None,
    spotify_bound_user_id: SpotifyBoundUserId = None,
) -> str:
    """Ingest Spotify listening data into local vector memory for long-term taste retrieval."""
    client, spotify_err = _get_client(access_token, spotify_bound_user_id)
    if spotify_err:
        return spotify_err
    if not client:
        return "Spotify session missing."
    try:
        docs: list[MemoryDoc] = []
        user_spotify_id = client.current_user_id()
        target_user = user_id.strip() if user_id != "default" else user_spotify_id

        # Fetch and format top tracks for memory ingestion
        for tr in client._sp.current_user_top_tracks(limit=20).get("items", []):
            s = _track_summary(tr)
            docs.append(MemoryDoc(
                source="top_tracks",
                text=f"Top track: {s['name']} by {s['artists']}",
                metadata={"uri": s["uri"]}
            ))
        
        if not docs:
            return "No Spotify data found to ingest."
        
        added = ingest_memory_docs(target_user, docs)
        return f"Ingested {added} taste-memory documents for user '{target_user}'."
    except Exception as e:
        return f"Failed to ingest taste memory: {_spotify_tool_error_detail(e)}"

def get_spotify_tools(access_token: str | None = None, user_id: str | None = None) -> list[BaseTool]:
    """
    Returns the list of Spotify tools. 
    In factory.py, these tools will be initialized with the user's specific token.
    """
    base: list[BaseTool] = [
        spotify_search_tracks,
        spotify_search_artists,
        spotify_get_artist_top_tracks,
        spotify_list_playlists,
        spotify_create_playlist,
        spotify_add_to_playlist,
        spotify_save_tracks,
        spotify_get_recently_played,
        spotify_build_library_profile,
        spotify_ingest_taste_memory,
    ]
    bound_token = access_token.strip() if isinstance(access_token, str) and access_token.strip() else None
    bound_user_id = user_id.strip() if isinstance(user_id, str) and user_id.strip() else None
    if not bound_token and not bound_user_id:
        return base

    def _invoke_bound(tool_obj: BaseTool, payload: dict) -> str:
        payload = dict(payload)
        prev_user = get_spotify_user_context()
        if bound_user_id:
            set_spotify_user_context(bound_user_id)
            payload["spotify_bound_user_id"] = bound_user_id
        try:
            if bound_token:
                payload["access_token"] = bound_token
            out = tool_obj.invoke(payload)
            return out if isinstance(out, str) else str(out)
        finally:
            if bound_user_id:
                set_spotify_user_context(prev_user)

    @tool("spotify_search_tracks")
    def spotify_search_tracks_bound(query: str, limit: int = 10) -> str:
        """Search Spotify for tracks by song name, artist, or genre. Returns track names, artists, and URIs."""
        return _invoke_bound(spotify_search_tracks, {"query": query, "limit": limit})

    @tool("spotify_search_artists")
    def spotify_search_artists_bound(query: str, limit: int = 5) -> str:
        """Search Spotify artists by name. Returns artist IDs and Genres."""
        return _invoke_bound(spotify_search_artists, {"query": query, "limit": limit})

    @tool("spotify_get_artist_top_tracks")
    def spotify_get_artist_top_tracks_bound(artist_id: str, market: str = "US", limit: int = 5) -> str:
        """Get top tracks for a specific artist ID. Use after searching to find the right artist."""
        return _invoke_bound(
            spotify_get_artist_top_tracks,
            {"artist_id": artist_id, "market": market, "limit": limit},
        )

    @tool("spotify_list_playlists")
    def spotify_list_playlists_bound(limit: int = 20) -> str:
        """List the user's Spotify playlists. Returns name and ID. Use this when picking a playlist to add tracks to."""
        return _invoke_bound(spotify_list_playlists, {"limit": limit})

    @tool("spotify_create_playlist")
    def spotify_create_playlist_bound(name: str, description: str = "") -> str:
        """Create a new Spotify playlist for the user. Returns the new playlist ID for use with spotify_add_to_playlist."""
        return _invoke_bound(spotify_create_playlist, {"name": name, "description": description})

    @tool("spotify_add_to_playlist")
    def spotify_add_to_playlist_bound(playlist_id: str, track_uris: str) -> str:
        """Add tracks to a playlist. track_uris is a comma-separated list of spotify:track:... URIs."""
        return _invoke_bound(
            spotify_add_to_playlist,
            {"playlist_id": playlist_id, "track_uris": track_uris},
        )

    @tool("spotify_save_tracks")
    def spotify_save_tracks_bound(track_uris: str) -> str:
        """Save tracks to the user's Spotify library (Liked Songs). track_uris is a comma-separated list of URIs."""
        return _invoke_bound(spotify_save_tracks, {"track_uris": track_uris})

    @tool("spotify_get_recently_played")
    def spotify_get_recently_played_bound(limit: int = 20) -> str:
        """Get the user's recently played tracks. Useful for current taste signals."""
        return _invoke_bound(spotify_get_recently_played, {"limit": limit})

    @tool("spotify_build_library_profile")
    def spotify_build_library_profile_bound(limit: int = 20) -> str:
        """Build a concise snapshot of user taste from top tracks/artists and recent listening."""
        return _invoke_bound(spotify_build_library_profile, {"limit": limit})

    @tool("spotify_ingest_taste_memory")
    def spotify_ingest_taste_memory_bound(user_id: str = "default") -> str:
        """Ingest Spotify listening data into local vector memory for long-term taste retrieval."""
        return _invoke_bound(spotify_ingest_taste_memory, {"user_id": user_id})

    return [
        spotify_search_tracks_bound,
        spotify_search_artists_bound,
        spotify_get_artist_top_tracks_bound,
        spotify_list_playlists_bound,
        spotify_create_playlist_bound,
        spotify_add_to_playlist_bound,
        spotify_save_tracks_bound,
        spotify_get_recently_played_bound,
        spotify_build_library_profile_bound,
        spotify_ingest_taste_memory_bound,
    ]

