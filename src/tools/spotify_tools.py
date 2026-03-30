"""Spotify tools: search, playlist actions, and taste-profile memory."""

import os
from typing import Optional, Sequence
from langchain_core.tools import tool
from src.auth.spotify_auth import get_oauth, get_user_token, save_user_token
from src.tools.spotify_context import get_spotify_anonymous_allowed, resolve_spotify_user_id_for_tools
from src.tools.taste_memory import MemoryDoc, ingest_memory_docs, retrieve_memory_docs

_SPOTIFY_CLIENT: Optional["SpotifyClient"] = None

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
                raise ValueError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET required for fallback.")
            self._sp = spotipy.Spotify(auth_manager=get_oauth())

        if self._sp is None:
            raise ValueError("No valid Spotify session found for this user. Please connect Spotify.")

    def _refresh_and_reinit(self, user_id: str):
        """Refreshes the Spotify access token using the stored refresh token."""
        import spotipy
        token_info = get_user_token(user_id)
        if token_info and token_info.get("refresh_token"):
            oauth = get_oauth()
            new_info = oauth.refresh_access_token(token_info["refresh_token"])
            
            # Initialize with new token
            temp_sp = spotipy.Spotify(auth=new_info["access_token"])
            user_data = temp_sp.current_user()
            
            # Persist updated token back to database
            save_user_token(user_data, new_info)
            self._sp = temp_sp
        else:
            raise ValueError(f"No stored Spotify token or refresh token for user {user_id}.")

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
        return self._sp.me().get("id", "unknown")

def _get_client(access_token: str | None = None) -> Optional[SpotifyClient]:
    """Internal helper to get a client instance using the session token or context."""
    current_user_id = resolve_spotify_user_id_for_tools()
    try:
        return SpotifyClient(access_token=access_token, user_id=current_user_id)
    except Exception:
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
def spotify_search_tracks(query: str, limit: int = 10, access_token: str = None) -> str:
    """Search Spotify for tracks by song name, artist, or genre. Returns track names, artists, and URIs."""
    client = _get_client(access_token)
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
        return f"Spotify search failed: {e}"

@tool
def spotify_search_artists(query: str, limit: int = 5, access_token: str = None) -> str:
    """Search Spotify artists by name. Returns artist IDs and Genres."""
    client = _get_client(access_token)
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
        return f"Spotify artist search failed: {e}"

@tool
def spotify_get_artist_top_tracks(artist_id: str, market: str = "US", limit: int = 5, access_token: str = None) -> str:
    """Get top tracks for a specific artist ID. Use after searching to find the right artist."""
    client = _get_client(access_token)
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
        return f"Failed to fetch artist top tracks: {e}"

@tool
def spotify_list_playlists(limit: int = 20, access_token: str = None) -> str:
    """List the user's Spotify playlists. Returns name and ID. Use this when picking a playlist to add tracks to."""
    client = _get_client(access_token)
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
        return f"Failed to list playlists: {e}"

@tool
def spotify_create_playlist(name: str, description: str = "", access_token: str = None) -> str:
    """Create a new Spotify playlist for the user. Returns the new playlist ID for use with spotify_add_to_playlist."""
    client = _get_client(access_token)
    if not client:
        return "Spotify session missing."
    try:
        pid, uri = client.create_playlist(name=name, description=description)
        return f"Successfully created playlist '{name}'. ID: {pid}"
    except Exception as e:
        return f"Failed to create playlist: {e}"

@tool
def spotify_add_to_playlist(playlist_id: str, track_uris: str, access_token: str = None) -> str:
    """Add tracks to a playlist. track_uris is a comma-separated list of spotify:track:... URIs."""
    client = _get_client(access_token)
    if not client:
        return "Spotify session missing."
    try:
        uris = [u.strip() for u in track_uris.split(",") if u.strip()]
        if not uris:
            return "No track URIs provided."
        # Clean the ID in case a full URI was passed
        clean_id = playlist_id.replace("spotify:playlist:", "").strip()
        client.add_to_playlist(clean_id, uris)
        return f"Successfully added {len(uris)} track(s) to the playlist."
    except Exception as e:
        return f"Failed to add to playlist: {e}"

@tool
def spotify_save_tracks(track_uris: str, access_token: str = None) -> str:
    """Save tracks to the user's Spotify library (Liked Songs). track_uris is a comma-separated list of URIs."""
    client = _get_client(access_token)
    if not client:
        return "Spotify session missing."
    try:
        uris = [u.strip() for u in track_uris.split(",") if u.strip()]
        if not uris:
            return "No track URIs provided."
        client.save_tracks(uris)
        return f"Saved {len(uris)} track(s) to your 'Liked Songs' library."
    except Exception as e:
        return f"Failed to save tracks to library: {e}"

@tool
def spotify_get_recently_played(limit: int = 20, access_token: str = None) -> str:
    """Get the user's recently played tracks. Useful for current taste signals."""
    client = _get_client(access_token)
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
        return f"Failed to fetch recently played: {e}"

@tool
def spotify_build_library_profile(limit: int = 20, access_token: str = None) -> str:
    """Build a concise snapshot of user taste from top tracks/artists and recent listening."""
    client = _get_client(access_token)
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
        return f"Failed to build profile: {e}"

@tool
def spotify_ingest_taste_memory(user_id: str = "default", access_token: str = None) -> str:
    """Ingest Spotify listening data into local vector memory for long-term taste retrieval."""
    client = _get_client(access_token)
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
        return f"Failed to ingest taste memory: {e}"

def get_spotify_tools(access_token: str = None) -> list:
    """
    Returns the list of Spotify tools. 
    In factory.py, these tools will be initialized with the user's specific token.
    """
    return [
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

