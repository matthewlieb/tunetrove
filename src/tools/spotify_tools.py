"""Spotify tools: search, playlist actions, and taste-profile memory."""

import os
from typing import Optional

from langchain_core.tools import tool

from src.auth.spotify_auth import get_oauth, get_user_token, save_user_token
from src.tools.spotify_context import get_spotify_anonymous_allowed, get_spotify_user_context
from src.tools.taste_memory import MemoryDoc, ingest_memory_docs, retrieve_memory_docs

_SPOTIFY_CLIENT: Optional["SpotifyClient"] = None


class SpotifyClient:
    def __init__(self, user_id: str | None = None):
        import spotipy

        self._sp = None
        self._auth_user_id = user_id

        # First choice: per-user token persisted from web OAuth.
        if user_id:
            token_info = get_user_token(user_id)
            if token_info:
                access_token = token_info.get("access_token")
                if access_token:
                    self._sp = spotipy.Spotify(auth=access_token)
                    try:
                        self._sp.current_user()
                    except spotipy.exceptions.SpotifyException as e:
                        if e.http_status == 401 and token_info.get("refresh_token"):
                            oauth = get_oauth()
                            new_info = oauth.refresh_access_token(token_info["refresh_token"])
                            user = spotipy.Spotify(auth=new_info["access_token"]).current_user()
                            save_user_token(user, new_info)
                            self._sp = spotipy.Spotify(auth=new_info["access_token"])
                        else:
                            raise

        if user_id and self._sp is None:
            # Never fall back to a different OAuth identity when a specific user was requested (web session).
            raise ValueError(
                "No stored Spotify token for this account. Connect Spotify in the app, then try again."
            )

        # Fallback: CLI / scripts after set_spotify_anonymous_allowed(True) only.
        if self._sp is None:
            client_id = os.environ.get("SPOTIFY_CLIENT_ID")
            client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
            if not client_id or not client_secret:
                raise ValueError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are required for Spotify tools")
            oauth = get_oauth()
            self._sp = spotipy.Spotify(auth_manager=oauth)

    def search_tracks(self, q: str, limit: int = 10):
        out = self._sp.search(q=q, type="track", limit=limit)
        return out.get("tracks", {}).get("items", [])

    def search_artists(self, q: str, limit: int = 5):
        out = self._sp.search(q=q, type="artist", limit=limit)
        return out.get("artists", {}).get("items", [])

    def artist_top_tracks(self, artist_id: str, market: str = "US"):
        out = self._sp.artist_top_tracks(artist_id=artist_id, country=market)
        return out.get("tracks", [])

    def add_to_playlist(self, playlist_id: str, track_uris: list[str]):
        self._sp.playlist_add_items(playlist_id, track_uris)

    def save_tracks(self, track_uris: list[str]):
        self._sp.current_user_saved_tracks_add(track_uris)

    def list_playlists(self, limit: int = 20):
        out = self._sp.current_user_playlists(limit=limit)
        return out.get("items", [])

    def create_playlist(self, name: str, description: str = "", public: bool = True):
        user_id = self._sp.me()["id"]
        pl = self._sp.user_playlist_create(user_id, name, public=public, description=description)
        return pl.get("id", ""), pl.get("uri", "")

    def current_user_id(self) -> str:
        return self._sp.me().get("id", "unknown")

    def recently_played(self, limit: int = 20):
        out = self._sp.current_user_recently_played(limit=limit)
        return out.get("items", [])

    def top_tracks(self, time_range: str = "medium_term", limit: int = 20):
        out = self._sp.current_user_top_tracks(time_range=time_range, limit=limit)
        return out.get("items", [])

    def top_artists(self, time_range: str = "medium_term", limit: int = 20):
        out = self._sp.current_user_top_artists(time_range=time_range, limit=limit)
        return out.get("items", [])

    def followed_artists(self, limit: int = 20):
        out = self._sp.current_user_followed_artists(limit=limit)
        return out.get("artists", {}).get("items", [])

    def saved_tracks(self, limit: int = 50, offset: int = 0):
        out = self._sp.current_user_saved_tracks(limit=limit, offset=offset)
        return out.get("items", [])

    def audio_features(self, track_ids: list[str]):
        return self._sp.audio_features(track_ids)


def _get_client() -> Optional[SpotifyClient]:
    global _SPOTIFY_CLIENT
    current_user = get_spotify_user_context()
    if current_user:
        try:
            return SpotifyClient(user_id=current_user)
        except Exception:
            return None
    if not get_spotify_anonymous_allowed():
        return None
    if _SPOTIFY_CLIENT is not None:
        return _SPOTIFY_CLIENT
    if not os.environ.get("SPOTIFY_CLIENT_ID") or not os.environ.get("SPOTIFY_CLIENT_SECRET"):
        return None
    try:
        _SPOTIFY_CLIENT = SpotifyClient()
        return _SPOTIFY_CLIENT
    except Exception:
        return None


def _track_summary(t):
    name = t.get("name", "?")
    artists = ", ".join(a.get("name", "") for a in t.get("artists", []))
    uri = t.get("uri", "")
    return {"name": name, "artists": artists, "uri": uri}


def _artist_summary(a):
    return {
        "id": a.get("id", ""),
        "name": a.get("name", "?"),
        "genres": ", ".join(a.get("genres", [])),
    }


def _track_id_from_uri(uri: str) -> str:
    if uri.startswith("spotify:track:"):
        return uri.split(":")[-1]
    if "track/" in uri:
        return uri.rsplit("track/", 1)[-1].split("?")[0]
    return uri


@tool
def spotify_search_tracks(query: str, limit: int = 10) -> str:
    """Search Spotify for tracks by song name, artist, or genre. Returns track names, artists, and URIs for adding to playlist or saving."""
    client = _get_client()
    if not client:
        return "Spotify is not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET (and run auth flow)."
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
def spotify_search_artists(query: str, limit: int = 5) -> str:
    """Search Spotify artists by name. Returns artist IDs for precise follow-up tools like spotify_get_artist_top_tracks."""
    client = _get_client()
    if not client:
        return "Spotify is not configured (or no authenticated Spotify session for this user)."
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
def spotify_get_artist_top_tracks(artist_id: str, market: str = "US", limit: int = 5) -> str:
    """Get top tracks for a specific artist ID. Use after spotify_search_artists to avoid wrong-artist matches."""
    client = _get_client()
    if not client:
        return "Spotify is not configured (or no authenticated Spotify session for this user)."
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
def spotify_list_playlists(limit: int = 20) -> str:
    """List the user's Spotify playlists. Returns playlist name and ID for each. Use this when the user says 'my playlist' or 'add to playlist' without naming one, so you can show them their playlists or pick one."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        items = client.list_playlists(limit=limit)
        if not items:
            return "No playlists found."
        lines = []
        for p in items:
            name = p.get("name", "?")
            pid = p.get("id", "")
            lines.append(f"{name} | ID: {pid}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to list playlists: {e}"


@tool
def spotify_create_playlist(name: str, description: str = "") -> str:
    """Create a new Spotify playlist for the user. Use when they say 'new playlist', 'create a playlist', or 'put these in a playlist called X'. Returns the new playlist ID so you can then call spotify_add_to_playlist with it. name: playlist name. description: optional description."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        pid, uri = client.create_playlist(name=name, description=description)
        return f"Created playlist '{name}'. ID: {pid} (use this ID with spotify_add_to_playlist to add tracks)."
    except Exception as e:
        return f"Failed to create playlist: {e}"


@tool
def spotify_add_to_playlist(playlist_id: str, track_uris: str) -> str:
    """Add one or more tracks to a Spotify playlist. playlist_id: the playlist's Spotify ID (or URI spotify:playlist:...). Get the ID from spotify_list_playlists (match by name) or from spotify_create_playlist. track_uris: comma-separated list of spotify:track:... URIs."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        uris = [u.strip() for u in track_uris.split(",") if u.strip()]
        if not uris:
            return "No track URIs provided."
        playlist_id = playlist_id.replace("spotify:playlist:", "").strip()
        client.add_to_playlist(playlist_id, uris)
        return f"Added {len(uris)} track(s) to playlist."
    except Exception as e:
        return f"Failed to add to playlist: {e}"


@tool
def spotify_save_tracks(track_uris: str) -> str:
    """Save one or more tracks to the user's Spotify library (Liked Songs). track_uris is a comma-separated list of spotify:track:... URIs."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        uris = [u.strip() for u in track_uris.split(",") if u.strip()]
        if not uris:
            return "No track URIs provided."
        client.save_tracks(uris)
        return f"Saved {len(uris)} track(s) to your library."
    except Exception as e:
        return f"Failed to save tracks: {e}"


@tool
def spotify_get_recently_played(limit: int = 20) -> str:
    """Get the user's recently played tracks. Useful for current taste signals."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        items = client.recently_played(limit=limit)
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
def spotify_get_top_items(kind: str = "tracks", time_range: str = "medium_term", limit: int = 20) -> str:
    """Get top tracks or artists from Spotify profile. kind: tracks|artists. time_range: short_term|medium_term|long_term."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        if kind == "artists":
            items = client.top_artists(time_range=time_range, limit=limit)
            if not items:
                return "No top artists found."
            return "\n".join([f"{a.get('name','?')} | Genres: {', '.join(a.get('genres', []))}" for a in items])
        items = client.top_tracks(time_range=time_range, limit=limit)
        if not items:
            return "No top tracks found."
        lines = []
        for tr in items:
            s = _track_summary(tr)
            lines.append(f"{s['name']} — {s['artists']} | URI: {s['uri']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch top items: {e}"


@tool
def spotify_get_followed_artists(limit: int = 20) -> str:
    """Get artists followed by the user on Spotify."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        items = client.followed_artists(limit=limit)
        if not items:
            return "No followed artists found."
        return "\n".join([f"{a.get('name','?')} | Genres: {', '.join(a.get('genres', []))}" for a in items])
    except Exception as e:
        return f"Failed to fetch followed artists: {e}"


@tool
def spotify_get_audio_features(track_uris: str) -> str:
    """Get Spotify audio features for track URIs or IDs. track_uris is comma-separated."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        ids = [_track_id_from_uri(x.strip()) for x in track_uris.split(",") if x.strip()]
        if not ids:
            return "No track URIs provided."
        features = client.audio_features(ids) or []
        lines = []
        for tid, f in zip(ids, features):
            if not f:
                continue
            lines.append(
                f"{tid} | danceability={f.get('danceability')} energy={f.get('energy')} "
                f"valence={f.get('valence')} tempo={f.get('tempo')} acousticness={f.get('acousticness')}"
            )
        return "\n".join(lines) if lines else "No audio features found."
    except Exception as e:
        return f"Failed to fetch audio features: {e}"


@tool
def spotify_build_library_profile(limit: int = 20) -> str:
    """Build a concise snapshot of user taste from top tracks/artists and recent listening."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        top_tracks = client.top_tracks(limit=limit)
        top_artists = client.top_artists(limit=limit)
        recent = client.recently_played(limit=limit)
        lines = [f"Spotify user: {client.current_user_id()}"]
        lines.append("Top artists:")
        for a in top_artists[:10]:
            lines.append(f"- {a.get('name','?')} ({', '.join(a.get('genres', []))})")
        lines.append("Top tracks:")
        for t in top_tracks[:10]:
            s = _track_summary(t)
            lines.append(f"- {s['name']} — {s['artists']}")
        lines.append("Recently played:")
        for row in recent[:10]:
            s = _track_summary(row.get("track", {}))
            lines.append(f"- {s['name']} — {s['artists']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to build profile: {e}"


@tool
def spotify_ingest_taste_memory(user_id: str = "default", saved_limit: int = 50, recent_limit: int = 30, top_limit: int = 30) -> str:
    """Ingest Spotify listening data into local vector memory for long-term taste retrieval."""
    client = _get_client()
    if not client:
        return "Spotify is not configured."
    try:
        docs: list[MemoryDoc] = []
        user_spotify_id = client.current_user_id()
        target_user = user_id.strip() or user_spotify_id

        for row in client.saved_tracks(limit=max(1, min(saved_limit, 50)), offset=0):
            tr = row.get("track", {})
            s = _track_summary(tr)
            docs.append(
                MemoryDoc(
                    source="saved_tracks",
                    text=f"Saved track: {s['name']} by {s['artists']}",
                    metadata={"uri": s["uri"]},
                )
            )
        for row in client.recently_played(limit=max(1, min(recent_limit, 50))):
            tr = row.get("track", {})
            s = _track_summary(tr)
            docs.append(
                MemoryDoc(
                    source="recently_played",
                    text=f"Recently played: {s['name']} by {s['artists']}",
                    metadata={"uri": s["uri"]},
                )
            )
        for tr in client.top_tracks(limit=max(1, min(top_limit, 50))):
            s = _track_summary(tr)
            docs.append(
                MemoryDoc(
                    source="top_tracks",
                    text=f"Top track: {s['name']} by {s['artists']}",
                    metadata={"uri": s["uri"]},
                )
            )
        for a in client.top_artists(limit=max(1, min(top_limit, 50))):
            docs.append(
                MemoryDoc(
                    source="top_artists",
                    text=f"Top artist: {a.get('name', '?')} | genres: {', '.join(a.get('genres', []))}",
                    metadata={"name": a.get("name", "?")},
                )
            )
        if not docs:
            return "No Spotify data found to ingest."
        added = ingest_memory_docs(target_user, docs)
        return f"Ingested {added} taste-memory documents for user '{target_user}' (spotify_id={user_spotify_id})."
    except Exception as e:
        return f"Failed to ingest taste memory: {e}"


@tool
def spotify_retrieve_taste_memory(user_id: str = "default", query: str = "music taste", k: int = 6) -> str:
    """Retrieve relevant historical taste memory for a user from local vector memory."""
    try:
        target_user = user_id.strip() or "default"
        rows = retrieve_memory_docs(target_user, query=query, k=max(1, min(k, 20)))
        if not rows:
            return (
                f"No taste-memory found for user '{target_user}'. "
                "Run spotify_ingest_taste_memory first."
            )
        lines = []
        for r in rows:
            lines.append(f"[{r['source']}] score={r['score']:.3f} {r['text']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to retrieve taste memory: {e}"


def get_spotify_tools():
    """Return Spotify tools only if credentials are configured."""
    if not os.environ.get("SPOTIFY_CLIENT_ID") or not os.environ.get("SPOTIFY_CLIENT_SECRET"):
        return []
    return [
        spotify_search_tracks,
        spotify_search_artists,
        spotify_get_artist_top_tracks,
        spotify_list_playlists,
        spotify_create_playlist,
        spotify_add_to_playlist,
        spotify_save_tracks,
        spotify_get_recently_played,
        spotify_get_top_items,
        spotify_get_followed_artists,
        spotify_get_audio_features,
        spotify_build_library_profile,
        spotify_ingest_taste_memory,
        spotify_retrieve_taste_memory,
    ]
