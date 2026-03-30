"""DeepAgents-only agent factory for spotify-llm.

Uses the Deep Agents SDK (`create_deep_agent`): LangGraph-native harness with built-in planning,
virtual filesystem, subagents, and summarization — see:

- https://github.com/langchain-ai/deepagents
- https://docs.langchain.com/oss/python/deepagents/overview

We pass custom tools (Tavily + Spotify), an app-specific system prompt, and an optional LangGraph
Postgres checkpointer. Additional middleware is not required for summarization: `create_deep_agent`
already inserts `create_summarization_middleware` with the resolved model and backend.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Sequence

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from src.tools.tavily_tools import music_web_search_tool
from src.tools.spotify_tools import get_spotify_tools

_USER_AGENT_CACHE: OrderedDict[str, Any] = OrderedDict()
_USER_AGENT_CACHE_MAX = 48
_user_agent_cache_lock = Lock()
_default_agent_singleton: Any = None
_default_agent_singleton_lock = Lock()


def _all_tools(spotify_token: str = None) -> Sequence[BaseTool]:
    base: list[BaseTool] = [music_web_search_tool()]
    # Pass the token here so the tools use the user's account
    base.extend(get_spotify_tools(access_token=spotify_token)) 
    return base


def _model_id_string() -> str:
    if os.environ.get("DEEPAGENTS_MODEL"):
        return os.environ["DEEPAGENTS_MODEL"].strip()
    provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic:claude-3-5-haiku-20241022"
    return "openai:gpt-4o-mini"


def _llm_request_timeout_seconds() -> int:
    raw = (os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS") or "120").strip()
    try:
        return max(30, int(raw))
    except ValueError:
        return 120


def _llm_max_retries() -> int:
    raw = (os.environ.get("LLM_MAX_RETRIES") or "4").strip()
    try:
        return max(0, min(15, int(raw)))
    except ValueError:
        return 4


def _deepagents_model() -> Any:
    """String id or bound chat model with per-request timeout (see Deep Agents customization)."""
    ident = _model_id_string()
    if (os.environ.get("DEEPAGENTS_MODEL_INIT") or "1").strip().lower() in ("0", "false", "no"):
        return ident
    try:
        from langchain.chat_models import init_chat_model

        return init_chat_model(
            model=ident,
            timeout=_llm_request_timeout_seconds(),
            max_retries=_llm_max_retries(),
        )
    except Exception:
        return ident


def _openai_model_id_for_byok() -> str:
    s = _model_id_string()
    if s.lower().startswith("openai:"):
        return s
    return "openai:gpt-4o-mini"


def _anthropic_model_id_for_byok() -> str:
    s = _model_id_string()
    if s.lower().startswith("anthropic:"):
        return s
    custom = (os.environ.get("ANTHROPIC_MODEL") or "").strip()
    if custom:
        return custom if custom.startswith("anthropic:") else f"anthropic:{custom}"
    return "anthropic:claude-3-5-haiku-20241022"


def _chat_model_for_user_secrets(secrets: Any) -> Any:
    """``secrets`` is :class:`src.auth.user_llm_keys.UserLlmSecrets` (avoid circular import)."""
    from langchain.chat_models import init_chat_model

    o = getattr(secrets, "openai_key", None)
    a = getattr(secrets, "anthropic_key", None)
    prov = getattr(secrets, "provider", None)
    timeout = _llm_request_timeout_seconds()
    retries = _llm_max_retries()
    if o and a:
        if prov == "anthropic":
            return init_chat_model(
                model=_anthropic_model_id_for_byok(),
                api_key=a,
                timeout=timeout,
                max_retries=retries,
            )
        return init_chat_model(
            model=_openai_model_id_for_byok(),
            api_key=o,
            timeout=timeout,
            max_retries=retries,
        )
    if o:
        return init_chat_model(
            model=_openai_model_id_for_byok(),
            api_key=o,
            timeout=timeout,
            max_retries=retries,
        )
    if a:
        return init_chat_model(
            model=_anthropic_model_id_for_byok(),
            api_key=a,
            timeout=timeout,
            max_retries=retries,
        )
    return _deepagents_model()


SYSTEM_PROMPT = """You are a friendly music discovery assistant (v1-lite).

You run inside the Deep Agents harness: you may have built-in planning, a virtual filesystem, subagents, and related tools. For typical requests here, **prefer answering directly** with `music_web_search` and the Spotify tools below—avoid heavy `write_todos` / subagent workflows unless the user asks for a large multi-step research project. Do **not** rely on `execute` (shell); this deployment does not expose a command sandbox.

You can:
- Search the web for music trends, scenes, and recommendations (deep research when the user wants discovery).
- Read Spotify state for the signed-in user: library profile, playlists, saved tracks, top items, recently played.
- Modify Spotify when the user clearly asks: create playlists, add tracks, save tracks.

Latency: prefer **fewer, decisive tool calls** over long chains. For “artists like X” / discovery, usually **one or two** `music_web_search` queries plus light Spotify checks is enough—do not stack redundant `spotify_build_library_profile` + top + recent reads unless the question is explicitly about *their* library.

**Playlist batching (speed):** For each playlist edit the user asked for, use **one** `spotify_add_to_playlist` call with **comma-separated** `track_uris` for every track you intend to add in that step (up to Spotify’s per-request limit)—**not** many separate add calls in parallel or sequence. Same for `spotify_save_tracks`: batch URIs in one call when possible.

Behavior rules:
1. Ground answers in Spotify tools for anything about *this user's* library; use music_web_search for the wider world (new artists, press, scenes).
1b. **Thread continuity (playlists & follow-ups):** When the user clearly refers to **prior turns** (e.g. **add those**, **blend**, **put them in a playlist**, **like before**, **similar to X from earlier**), you **must** reuse the **exact names** from **your earlier assistant messages** in this thread and verify on Spotify. Do **not** substitute a different catalog match that merely **sounds similar**. If a string is ambiguous, use `spotify_search_artists` with explicit spelling or ask one short clarifying question before writes.
1c. **Single-turn focus (no unrelated detours):** Do **not** start **unrelated** extra work from old turns (no “while I’m at it” research or surprise playlist edits). **Follow-ups that explicitly continue playlist or recommendation work are related** — treat them as one flow with the thread history.
1d. **Library / taste facts** (favorite artist, top artist, most played, “who do I listen to most”, my genres): use **only** Spotify **read** tools such as `spotify_get_top_items`, `spotify_get_recently_played`, `spotify_get_saved_tracks`, or `spotify_build_library_profile`. **Do not** call `music_web_search`. **Do not** create playlists or add tracks unless the user clearly asks to create/modify a playlist **in that same message** (or clearly continues that request per 1b).
2. Optional personalization: if `spotify_retrieve_taste_memory` / `spotify_ingest_taste_memory` are available and Supabase is configured, use them for richer taste snippets; if not, rely on spotify_build_library_profile, top/recent/saved tools only—do not block the user on memory setup. If long-term `/memories/` is enabled, you may append a short bullet list of **names from the current discovery thread** to `/memories/session_discovery.txt` after substantial discovery turns so follow-up playlist requests stay aligned (no secrets).
3. When recommendations depend on preference, use spotify_build_library_profile, spotify_get_top_items, spotify_get_recently_played as needed.
4. For playlist actions, confirm ambiguous playlist names before modifying.
5. Keep responses concise and personalized.
6. Treat time as dynamic. Use the provided [today=YYYY-MM-DD ...] context and never assume a static year.
7. For "up-and-coming/new artists" requests, perform deeper web research:
   - Use music_web_search with a broad timeframe (avoid month-only unless the user asks),
   - Cross-check across multiple distinct sources,
   - Prefer recent/current-year signals before older lists.
   - Reserve "up-and-coming" / "emerging" / "ones to watch" for artists the sources actually frame that way. Do **not** label multi-platinum, long-established, or already-mainstream acts as up-and-coming unless the article does so explicitly for *that* year.
   - **Citations:** Only link to URLs that appear in the **music_web_search tool result text** (e.g. `url` fields in results). Never invent, guess, or reuse one article URL for every name. If you lack a real URL from the tool output for a bullet, omit the markdown link or say you do not have a direct source.
   - When helpful, use `spotify_search_artists` to confirm an artist exists on Spotify before highlighting them in a discovery list.
8. For playlist building from artist names, avoid weak track-name matching:
   - Resolve artist IDs with spotify_search_artists,
   - Fetch spotify_get_artist_top_tracks per artist,
   - Then add those URIs to playlists.
9. If Spotify write actions fail or look suspicious, report failure clearly and suggest reconnecting Spotify.

Important: the chat message includes [session_user_id=...]. Use that value as user_id for tools that require it.

If **Skills** are enabled, follow the bundled `v1-lite-music` skill’s decision tree for tool choice and turn boundaries.
"""


_REPO_ROOT = Path(__file__).resolve().parents[2]
_checkpointer = None
_lt_memory_store = None
_bundled_skill_files_cache: dict[str, Any] | None = None


def _skills_enabled() -> bool:
    v = (os.environ.get("DEEPAGENTS_SKILLS") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _lt_memory_enabled() -> bool:
    v = (os.environ.get("DEEPAGENTS_LT_MEMORY") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _bundled_skill_files() -> dict[str, Any]:
    """StateBackend skills: seed virtual files per https://docs.langchain.com/oss/python/deepagents/skills"""
    global _bundled_skill_files_cache
    if _bundled_skill_files_cache is not None:
        return _bundled_skill_files_cache
    if not _skills_enabled():
        _bundled_skill_files_cache = {}
        return _bundled_skill_files_cache
    p = _REPO_ROOT / "skills" / "v1-lite-music" / "SKILL.md"
    if not p.is_file():
        _bundled_skill_files_cache = {}
        return _bundled_skill_files_cache
    try:
        from deepagents.backends.utils import create_file_data
    except Exception:
        _bundled_skill_files_cache = {}
        return _bundled_skill_files_cache
    content = p.read_text(encoding="utf-8")
    _bundled_skill_files_cache = {"/skills/v1-lite-music/SKILL.md": create_file_data(content)}
    return _bundled_skill_files_cache


def _get_lt_memory_store():
    """In-process store for /memories/ — dev only; use PostgresStore in production."""
    global _lt_memory_store
    if _lt_memory_store is None:
        from langgraph.store.memory import InMemoryStore

        _lt_memory_store = InMemoryStore()
    return _lt_memory_store


def _agent_invoke_input(messages: list) -> dict[str, Any]:
    payload: dict[str, Any] = {"messages": messages}
    sf = _bundled_skill_files()
    if sf:
        payload["files"] = sf
    return payload


def hitl_enabled() -> bool:
    """Human-in-the-loop for sensitive tools (see https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)."""
    s = (os.environ.get("SPOTIFY_HITL") or "").strip().lower()
    if s in ("0", "false", "no", "off"):
        return False
    if s in ("1", "true", "yes", "on"):
        return True
    d = (os.environ.get("DEEPAGENTS_HITL") or "1").strip().lower()
    return d not in ("0", "false", "no", "off")


def thread_uses_langgraph_checkpoint() -> bool:
    """True when conversation state lives in LangGraph checkpoints (Postgres, MemorySaver, etc.)."""
    conn = (
        os.environ.get("CHECKPOINT_DATABASE_URL")
        or os.environ.get("SUPABASE_DB_URL")
        or os.environ.get("CHECKPOINT_DB")
        or ""
    ).strip()
    return bool(conn) or hitl_enabled() or _memory_checkpoint_requested()


def _memory_checkpoint_requested() -> bool:
    return (os.environ.get("LANGGRAPH_CHECKPOINTER") or "").strip().lower() == "memory"


def _get_checkpointer():
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer
    conn_str = (
        os.environ.get("CHECKPOINT_DATABASE_URL")
        or os.environ.get("SUPABASE_DB_URL")
        or os.environ.get("CHECKPOINT_DB")
        or ""
    ).strip()
    if conn_str:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            cm = PostgresSaver.from_conn_string(conn_str)
            _checkpointer = cm.__enter__() if hasattr(cm, "__enter__") else cm  # noqa: PLW0603
            if hasattr(_checkpointer, "setup"):
                _checkpointer.setup()
            return _checkpointer
        except Exception:
            pass
    if hitl_enabled() or _memory_checkpoint_requested():
        from langgraph.checkpoint.memory import MemorySaver

        _checkpointer = MemorySaver()  # noqa: PLW0603
        return _checkpointer
    return None


def _interrupt_on_map() -> dict[str, Any]:
    """``interrupt_on`` for Deep Agents HITL (see https://docs.langchain.com/oss/python/deepagents/human-in-the-loop).

    Defaults favor fewer prompts: DeepAgents virtual FS tools are off unless ``DEEPAGENTS_HITL_FS=1``.
    Spotify writes use ``SPOTIFY_HITL_SCOPE``: ``minimal`` (default) interrupts only ``spotify_create_playlist``
    and ``spotify_ingest_taste_memory``; ``full`` also interrupts ``spotify_add_to_playlist`` and
    ``spotify_save_tracks``.
    """
    fs_hitl = (os.environ.get("DEEPAGENTS_HITL_FS") or "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    scope = (os.environ.get("SPOTIFY_HITL_SCOPE") or "minimal").strip().lower()
    full_spotify = scope in ("full", "all")

    spotify: dict[str, Any] = {
        "spotify_create_playlist": True,
        "spotify_add_to_playlist": bool(full_spotify),
        "spotify_save_tracks": bool(full_spotify),
        "spotify_ingest_taste_memory": {"allowed_decisions": ["approve", "reject"]},
    }
    out: dict[str, Any] = dict(spotify)
    if fs_hitl:
        out["write_file"] = {"allowed_decisions": ["approve", "reject"]}
        out["edit_file"] = {"allowed_decisions": ["approve", "reject"]}
    else:
        out["write_file"] = False
        out["edit_file"] = False
    return out


def _compile_deep_agent_with_model(model: Any, spotify_token: str = None) -> Any:
    """Single place that calls ``create_deep_agent`` (model is string or ``BaseChatModel`` per Deep Agents docs)."""

    try:
        from deepagents import create_deep_agent
        from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "deepagents is not installed. From repo root run: pip install -e . (see pyproject.toml)."
        ) from e

    tools = list(_all_tools(spotify_token=spotify_token))    
    prompt = os.environ.get("AGENT_SYSTEM_PROMPT", SYSTEM_PROMPT)

    cp = _get_checkpointer()
    kwargs: dict[str, Any] = {
        "model": model,
        "tools": tools,
        "system_prompt": prompt,
        "checkpointer": cp,
    }
    if hitl_enabled() and cp is not None:
        kwargs["interrupt_on"] = _interrupt_on_map()
    if _bundled_skill_files():
        kwargs["skills"] = ["/skills/"]

    if _lt_memory_enabled():
        # https://docs.langchain.com/oss/python/deepagents/long-term-memory
        def make_backend(runtime: Any):
            return CompositeBackend(
                default=StateBackend(runtime),
                routes={"/memories/": StoreBackend(runtime)},
            )

        kwargs["store"] = _get_lt_memory_store()
        kwargs["backend"] = make_backend

    return create_deep_agent(**kwargs)


def get_default_agent() -> Any:
    """Shared Deep Agent using server env keys (single compile per process)."""
    global _default_agent_singleton
    with _default_agent_singleton_lock:
        if _default_agent_singleton is None:
            _default_agent_singleton = _compile_deep_agent_with_model(_deepagents_model())
        return _default_agent_singleton


def create_agent():
    """Alias for :func:`get_default_agent` (CLI and tests)."""
    return get_default_agent()


def invalidate_user_agent_cache(user_id: str) -> None:
    """Call after the user updates or clears BYOK keys."""
    with _user_agent_cache_lock:
        _USER_AGENT_CACHE.pop(user_id, None)


def get_agent_for_spotify_user(spotify_user_id: str | None) -> Any:
    # 1. Quick exit if no user
    if not spotify_user_id or not spotify_user_id.strip():
        return get_default_agent()

    uid = spotify_user_id.strip()

    # 2. Check cache first (improves performance)
    with _user_agent_cache_lock:
        if uid in _USER_AGENT_CACHE:
            _USER_AGENT_CACHE.move_to_end(uid)
            return _USER_AGENT_CACHE[uid]

    # 3. Resolve user-specific requirements (secrets & token)
    # Note: Replace 'your_db' with your actual Supabase/DB helper
    user_token = your_db.get_token(uid) 
    
    from src.auth.user_llm_keys import load_decrypted_secrets
    secrets = load_decrypted_secrets(uid)

    # 4. Compile the dedicated agent
    # Use user-specific model if they have BYOK, otherwise use default model
    model = _chat_model_for_user_secrets(secrets) if secrets else _deepagents_model()
    
    # CRITICAL FIX: Pass the resolved user_token here
    graph = _compile_deep_agent_with_model(model, spotify_token=user_token)

    # 5. Save to cache and manage size
    with _user_agent_cache_lock:
        _USER_AGENT_CACHE[uid] = graph
        if len(_USER_AGENT_CACHE) > _USER_AGENT_CACHE_MAX:
            _USER_AGENT_CACHE.popitem(last=False)
            
    return graph


def _build_turn_messages(user_message: str, thread_id: str, previous_messages) -> list:
    """Messages batch for invoke/stream (same semantics as run_chat)."""
    messages = list(previous_messages) if previous_messages else []
    now = datetime.now(timezone.utc)
    today_ctx = f"[today={now.date().isoformat()} utc_weekday={now.strftime('%A')} year={now.year}]"
    enriched = f"[session_user_id={thread_id}] {today_ctx} {user_message}"
    messages.append(HumanMessage(content=enriched))
    return messages


def _messages_from_invoke_output(out: Any) -> list:
    if hasattr(out, "value"):
        val = out.value
        if isinstance(val, dict) and "messages" in val:
            return list(val["messages"])
    if isinstance(out, dict) and "messages" in out:
        return list(out["messages"])
    return []


def _hitl_payload_from_invoke(out: Any) -> dict[str, Any] | None:
    ints = getattr(out, "interrupts", None) or ()
    if not ints:
        return None
    first = ints[0]
    raw = first.value if hasattr(first, "value") else first
    if isinstance(raw, dict):
        return {
            "action_requests": raw.get("action_requests"),
            "review_configs": raw.get("review_configs"),
        }
    return {"value": str(raw)}


def run_chat(agent: Any, user_message: str, thread_id: str = "default", previous_messages=None):
    """Invoke the Deep Agent graph.

    Returns ``{"messages": [...], "hitl": {...} | None}``. When ``hitl`` is set, execution is
    paused for human approval (same ``thread_id``; resume with ``resume_chat``).
    """
    messages = _build_turn_messages(user_message, thread_id, previous_messages)
    config = {"configurable": {"thread_id": thread_id}}
    inp = _agent_invoke_input(messages)
    try:
        out = agent.invoke(inp, config=config, version="v2")
    except TypeError:
        out = agent.invoke(inp, config=config)
    msgs = _messages_from_invoke_output(out)
    hitl = _hitl_payload_from_invoke(out)
    return {"messages": msgs, "hitl": hitl}


def resume_chat(
    agent: Any,
    decisions: list[dict[str, Any]],
    thread_id: str = "default",
):
    """Resume after HITL interrupt; ``decisions`` matches LangGraph / Deep Agents docs."""
    from langgraph.types import Command

    config = {"configurable": {"thread_id": thread_id}}
    try:
        out = agent.invoke(Command(resume={"decisions": decisions}), config=config, version="v2")
    except TypeError:
        out = agent.invoke(Command(resume={"decisions": decisions}), config=config)
    msgs = _messages_from_invoke_output(out)
    hitl = _hitl_payload_from_invoke(out)
    return {"messages": msgs, "hitl": hitl}


def stream_chat_chunks(
    agent: Any,
    user_message: str,
    thread_id: str = "default",
    previous_messages=None,
):
    """Yield LangGraph v2 stream parts from the Deep Agent (`stream_mode`, `version='v2'`).

    See https://docs.langchain.com/oss/python/deepagents/streaming
    """
    messages = _build_turn_messages(user_message, thread_id, previous_messages)
    config = {"configurable": {"thread_id": thread_id}}
    # v2 stream format (LangGraph >= 1.1); token deltas + tool_call_chunks live in "messages".
    inp = _agent_invoke_input(messages)
    try:
        yield from agent.stream(
            inp,
            config=config,
            stream_mode=["messages"],
            subgraphs=True,
            version="v2",
        )
    except TypeError:
        yield from agent.stream(
            inp,
            config=config,
            stream_mode=["messages"],
            subgraphs=True,
        )
