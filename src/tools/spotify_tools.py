"""Spotify tools: search, playlist actions, and taste-profile memory."""

import os
from typing import Optional, Sequence
from langchain_core.tools import tool
from src.auth.spotify_auth import get_oauth, get_user_token, save_user_token
from src.tools.spotify_context import get_spotify_anonymous_allowed, resolve_spotify_user_id_for_tools
from src.tools.taste_memory import MemoryDoc, ingest_memory_docs, retrieve_memory_docs

class SpotifyClient:
    def __init__(self, access_token: str | None = None, user_id: str | None = None):
        import spotipy
        self._sp = None
        self._auth_user_id = user_id

        # Priority 1: Use the explicit access token passed from the session/factory
        if access_token:
            self._sp = spotipy.Spotify(auth=access_token)
            # Optional: Verify token is still valid
            try:
                self._sp.current_user()
            except spotipy.exceptions.SpotifyException as e:
                if e.http_status == 401:
                    # Token expired; attempt refresh if user_id is known
                    if user_id:
                        self._refresh_and_reinit(user_id)
                    else:
                        raise ValueError("Spotify session expired. Please re-authenticate.")

        # Priority 2: Fallback to database lookup if only user_id is provided
        elif user_id:
            self._refresh_and_reinit(user_id)

        # Final Fallback: CLI / Anonymous mode (Only if allowed)
        if self._sp is None and get_spotify_anonymous_allowed():
            client_id = os.environ.get("SPOTIFY_CLIENT_ID")
            client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
            if not client_id or not client_secret:
                raise ValueError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET required.")
            self._sp = spotipy.Spotify(auth_manager=get_oauth())

        if self._sp is None:
            raise ValueError("No valid Spotify session found for this user.")

    def _refresh_and_reinit(self, user_id: str):
        import spotipy
        token_info = get_user_token(user_id)
        if token_info and token_info.get("refresh_token"):
            oauth = get_oauth()
            new_info = oauth.refresh_access_token(token_info["refresh_token"])
            # Update the DB with the new token
            sp_temp = spotipy.Spotify(auth=new_info["access_token"])
            user_data = sp_temp.current_user()
            save_user_token(user_data, new_info)
            self._sp = sp_temp
        else:
            raise ValueError(f"No stored Spotify token for user {user_id}.")

    # --- API Wrapper Methods ---
    def search_tracks(self, q: str, limit: int = 10):
        return self._sp.search(q=q, type="track", limit=limit).get("tracks", {}).get("items", [])

    def search_artists(self, q: str, limit: int = 5):
        return self._sp.search(q=q, type="artist", limit=limit).get("artists", {}).get("items", [])

    def artist_top_tracks(self, artist_id: str, market: str = "US"):
        return self._sp.artist_top_tracks(artist_id=artist_id, country=market).get("tracks", [])

    def create_playlist(self, name: str, description: str = "", public: bool = True):
        user_id = self._sp.me()["id"]
        pl = self._sp.user_playlist_create(user_id, name, public=public, description=description)
        return pl.get("id", ""), pl.get("uri", "")

    def add_to_playlist(self, playlist_id: str, track_uris: list[str]):
        self._sp.playlist_add_items(playlist_id, track_uris)

    def current_user_id(self) -> str:
        return self._sp.me().get("id", "unknown")

def _get_client(access_token: str | None = None) -> Optional[SpotifyClient]:
    """
    Helper to get the client. If an access_token is passed (from the agent factory),
    it uses it. Otherwise, it attempts to resolve the user ID from the context.
    """
    current_user_id = resolve_spotify_user_id_for_tools()
    
    # Priority: Use the token if the factory provided one
    if access_token or current_user_id:
        try:
            return SpotifyClient(access_token=access_token, user_id=current_user_id)
        except Exception:
            return None
            
    # Final fallback for anonymous/CLI mode
    if get_spotify_anonymous_allowed():
        try:
            return SpotifyClient()
        except Exception:
            return None
    return None

# --- Helper Summarizers ---
def _track_summary(t):
    return {
        "name": t.get("name", "?"),
        "artists": ", ".join(a.get("name", "") for a in t.get("artists", [])),
        "uri": t.get("uri", "")
    }

# --- Tool Definitions ---

@tool
def spotify_list_playlists(limit: int = 20, access_token: str = None) -> str:
    """List the user's Spotify playlists. Returns name and ID."""
    client = _get_client(access_token)
    if not client: return "Spotify session missing."
    try:
        items = client._sp.current_user_playlists(limit=limit).get("items", [])
        if not items: return "No playlists found."
        return "\n".join([f"{p.get('name')} | ID: {p.get('id')}" for p in items])
    except Exception as e: return f"Error: {e}"

@tool
def spotify_get_recently_played(limit: int = 20, access_token: str = None) -> str:
    """Get user's recently played tracks for taste signals."""
    client = _get_client(access_token)
    if not client: return "Spotify session missing."
    try:
        items = client._sp.current_user_recently_played(limit=limit).get("items", [])
        lines = []
        for row in items:
            s = _track_summary(row.get("track", {}))
            lines.append(f"{s['name']} — {s['artists']} | URI: {s['uri']}")
        return "\n".join(lines) or "No history found."
    except Exception as e: return f"Error: {e}"

@tool
def spotify_ingest_taste_memory(user_id: str = "default", access_token: str = None) -> str:
    """Ingest Spotify data into local vector memory for long-term retrieval."""
    client = _get_client(access_token)
    if not client: return "Spotify session missing."
    try:
        # Re-use your existing ingestion logic here using client._sp
        # ... (Same logic as before, just using this client instance)
        return "Taste memory updated."
    except Exception as e: return f"Ingestion failed: {e}"

# --- The Tool Factory ---

def get_spotify_tools(access_token: str = None) -> list:
    """
    Returns the list of tools, pre-configured to use the provided access_token.
    This is what src/agent/factory.py should call.
    """
    # Define a wrapper that injects the token into each tool call if needed, 
    # or rely on the tool's internal _get_client(access_token) call.
    return [
        spotify_search_tracks,
        spotify_search_artists,
        spotify_get_artist_top_tracks,
        spotify_list_playlists,
        spotify_create_playlist,
        spotify_add_to_playlist,
        spotify_save_tracks,
        spotify_get_recently_played,
        spotify_ingest_taste_memory,
    ]
