"""FastAPI app for spotify-llm v1-lite (auth + chat + health)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv

# Load repo-root .env before reading CORS / secrets (uvicorn does not do this by default).
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from src.agent import stream_chat_chunks, thread_uses_langgraph_checkpoint
from src.auth import get_oauth, get_user_profile, make_state, save_user_token, verify_oauth_state
from src.tools.spotify_context import set_spotify_user_context

# In-memory store: thread_id -> list of messages (when not using checkpointer)
_thread_messages: dict[str, list] = {}
_agent_run_chat = None
_agent_resume_chat = None
_default_agent_warmed = False
_agent_lock = Lock()

_LOG = logging.getLogger("uvicorn.error")
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_LLM_KEYS_BODY_MISSING = object()


def _prewarm_agent_enabled() -> bool:
    """Load DeepAgents in a background thread after startup so the first /chat is fast."""
    raw = (os.environ.get("PREWARM_AGENT") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _disable_outbound_proxy_enabled() -> bool:
    raw = (os.environ.get("DISABLE_OUTBOUND_PROXY") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _proxy_env_snapshot() -> dict[str, str]:
    out: dict[str, str] = {}
    for k in (*_PROXY_KEYS, "NO_PROXY", "no_proxy"):
        v = (os.environ.get(k) or "").strip()
        if v:
            out[k] = v
    return out


def _apply_proxy_env_policy() -> None:
    if _disable_outbound_proxy_enabled():
        for k in _PROXY_KEYS:
            os.environ.pop(k, None)
        _LOG.warning("DISABLE_OUTBOUND_PROXY=1 active; cleared HTTP(S)_PROXY/ALL_PROXY for API process")
    snap = _proxy_env_snapshot()
    if snap:
        keys = ", ".join(sorted(snap.keys()))
        _LOG.info("proxy env detected: %s", keys)


def _ensure_agent_loaded() -> None:
    """Import chat helpers and precompile the default (server-key) agent once per process."""
    global _agent_run_chat, _agent_resume_chat, _default_agent_warmed
    from src.agent.factory import get_default_agent, resume_chat as _resume, run_chat

    with _agent_lock:
        if _agent_run_chat is None or _agent_resume_chat is None:
            _agent_run_chat = run_chat
            _agent_resume_chat = _resume
        if not _default_agent_warmed:
            get_default_agent()
            _default_agent_warmed = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    _apply_proxy_env_policy()
    if not (os.environ.get("SESSION_SECRET") or "").strip():
        _LOG.warning(
            "SESSION_SECRET is unset: a new signing key is generated on every API restart. "
            "In-flight Spotify OAuth and existing login cookies will break after restart. "
            "Set SESSION_SECRET in repo-root .env (see .env.example)."
        )
    if _prewarm_agent_enabled():

        async def _warm() -> None:
            try:
                await asyncio.to_thread(_ensure_agent_loaded)
                _LOG.info("Agent prewarm finished (DeepAgents ready)")
            except Exception:
                _LOG.exception("Agent prewarm failed; first /chat will load the agent")

        asyncio.create_task(_warm())
    yield


app = FastAPI(
    title="spotify-llm",
    description="Music discovery agent webhook",
    lifespan=lifespan,
)


def _json_chat_error(request: Request, message: str, *, status_code: int = 500) -> JSONResponse:
    """Stable JSON shape for /chat and proxied clients (never rely on Starlette HTML error pages)."""
    rid = getattr(request.state, "request_id", "") if hasattr(request, "state") else ""
    return JSONResponse(
        status_code=min(max(status_code, 100), 599),
        content={
            "reply": message,
            "error": True,
            "tool_trace": [],
            "request_id": rid,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    _LOG.warning("validation error: %s", exc)
    return _json_chat_error(request, "Invalid request body", status_code=422)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, str):
        msg = detail
    elif isinstance(detail, list):
        msg = "Invalid request"
    else:
        msg = str(detail)
    return _json_chat_error(request, msg, status_code=exc.status_code)


def _expose_internal_errors() -> bool:
    raw = (os.environ.get("EXPOSE_INTERNAL_ERRORS") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    _LOG.exception("unhandled exception path=%s", request.url.path)
    if _expose_internal_errors():
        return _json_chat_error(request, f"Server error: {exc!s}", status_code=500)
    return _json_chat_error(request, "Server error. Please try again.", status_code=500)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = (request.headers.get("x-request-id") or "").strip() or str(uuid.uuid4())
    request.state.request_id = rid
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    response.headers["x-request-id"] = rid
    if _chat_debug_enabled():
        _LOG.info(
            "http request_id=%s method=%s path=%s status=%s elapsed_ms=%.1f",
            rid,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
    return response

_session_https = (os.environ.get("SESSION_COOKIE_SECURE") or "").strip().lower() in (
    "1",
    "true",
    "yes",
)
_ss = (os.environ.get("SESSION_SAME_SITE") or "lax").strip().lower()
if _ss not in ("lax", "strict", "none"):
    _ss = "lax"
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", secrets.token_hex(32)),
    same_site=_ss,
    https_only=_session_https,
)

# Allow browser apps (e.g. Vercel Next.js) to call this API.
# Configure with:
#   CORS_ALLOW_ORIGINS=http://localhost:3003,https://your-vercel-app.vercel.app
_cors = (os.environ.get("CORS_ALLOW_ORIGINS") or "").strip()
if _cors:
    origins = [o.strip() for o in _cors.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _run_agent_chat_sync(
    user_message: str, thread_id: str, spotify_user_id: str | None, previous_messages=None
):
    from src.tools.spotify_context import set_spotify_user_context

    set_spotify_user_context(spotify_user_id)
    try:
        _ensure_agent_loaded()
        from src.agent.factory import get_agent_for_spotify_user

        agent = get_agent_for_spotify_user(spotify_user_id)
        return _agent_run_chat(agent, user_message, thread_id=thread_id, previous_messages=previous_messages)
    finally:
        set_spotify_user_context(None)


def _run_agent_resume_sync(decisions: list, thread_id: str, spotify_user_id: str | None):
    from src.tools.spotify_context import set_spotify_user_context

    set_spotify_user_context(spotify_user_id)
    try:
        _ensure_agent_loaded()
        from src.agent.factory import get_agent_for_spotify_user

        agent = get_agent_for_spotify_user(spotify_user_id)
        return _agent_resume_chat(agent, decisions, thread_id=thread_id)
    finally:
        set_spotify_user_context(None)


def _session_user_id(request: Request) -> str | None:
    user = request.session.get("spotify_user")
    if isinstance(user, dict):
        uid = user.get("id")
        return uid.strip() if isinstance(uid, str) and uid.strip() else None
    return None


def _use_persistent_checkpointer() -> bool:
    """True when thread history is in LangGraph checkpoints (not in-memory _thread_messages)."""
    return thread_uses_langgraph_checkpoint()


def _agent_timeout_seconds() -> float:
    raw = (os.environ.get("AGENT_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        # POST /chat (JSON): allow multi-tool Deep Agent turns. Keep below typical proxy max when overridden.
        return 300.0
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 300.0


_CONV_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")


def _sanitize_conversation_id(raw) -> str | None:
    """Optional client thread suffix (isolates LangGraph / in-memory history)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or not _CONV_ID_RE.fullmatch(s):
        return None
    return s


def _parse_chat_turn(
    data: dict | None, request: Request
) -> tuple[str, str, str | None, str] | None:
    """Validate JSON body; return (body_str, thread_id, session_uid, base_tid) or None."""
    if not isinstance(data, dict):
        return None
    from_id = data.get("from", "anonymous")
    body = data.get("body", "")
    body_str = body.strip() if isinstance(body, str) else str(body).strip()
    if not body_str:
        return None
    session_uid = _session_user_id(request)
    base_tid = session_uid or str(from_id).strip()
    conv = _sanitize_conversation_id(data.get("conversation_id"))
    thread_id = f"{base_tid}::{conv}" if conv else base_tid
    return body_str, thread_id, session_uid, base_tid


def _parse_chat_resume(
    data: dict | None, request: Request
) -> tuple[str, str | None, str, list] | None:
    """Validate HITL resume body; return (thread_id, session_uid, base_tid, decisions)."""
    if not isinstance(data, dict):
        return None
    decisions = data.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return None
    from_id = data.get("from", "anonymous")
    session_uid = _session_user_id(request)
    base_tid = session_uid or str(from_id).strip()
    conv = _sanitize_conversation_id(data.get("conversation_id"))
    thread_id = f"{base_tid}::{conv}" if conv else base_tid
    return thread_id, session_uid, base_tid, decisions


_SPOTIFY_ID_CLAIM_RE = re.compile(r"^[a-zA-Z0-9]{10,64}$")


def _spotify_user_id_claim_error(data: dict | None, session_uid: str | None) -> JSONResponse | None:
    """If the client sends ``spotify_user_id``, it must match the signed session (defense in depth)."""
    if not isinstance(data, dict):
        return None
    raw = data.get("spotify_user_id")
    if raw is None or raw is False:
        return None
    claim = str(raw).strip()
    if not claim:
        return None
    if not _SPOTIFY_ID_CLAIM_RE.fullmatch(claim):
        return JSONResponse(
            status_code=400,
            content={"reply": "Invalid `spotify_user_id` in request body.", "error": True, "tool_trace": []},
        )
    if session_uid and claim != session_uid:
        _LOG.warning(
            "spotify_user_id body claim does not match session cookie (session=%s claim=%s)",
            session_uid,
            claim,
        )
        return JSONResponse(
            status_code=403,
            content={
                "reply": "Spotify login session does not match `spotify_user_id`. Refresh the page and reconnect Spotify.",
                "error": True,
                "tool_trace": [],
            },
        )
    return None


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


def _chat_debug_enabled() -> bool:
    raw = (os.environ.get("CHAT_DEBUG_LOGS") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# Blocks in multimodal / streaming AIMessage.content — never surface these as chat text.
_SKIP_CONTENT_BLOCK_TYPES = frozenset(
    {
        "function_call",
        "tool_call",
        "tool_use",
        "function",
        "server_tool_use",
        "invalid_tool_calls",
        "reasoning",
    }
)


def _last_ai_content(messages):
    """Last user-visible assistant string, skipping tool-only AI messages."""
    from langchain_core.messages import AIMessage

    for m in reversed(messages):
        if not isinstance(m, AIMessage) or not m.content:
            continue
        txt = _content_to_text(m.content)
        if txt and txt.strip():
            return txt
    return None


def _reply_from_stream_chunks(chunks: list[str]) -> str:
    """Join streamed text deltas (fallback when checkpoint state lacks final AIMessage text)."""
    return "".join(chunks).strip()


def _empty_run_user_message(tool_trace: list[dict]) -> str:
    """When the graph stops without assistant text but tools ran — clearer than bare (no reply)."""
    if not tool_trace:
        return "(no reply)"
    kinds = {t.get("kind") for t in tool_trace if isinstance(t, dict)}
    if "tool_result" in kinds:
        return (
            "The assistant ran tools but did not produce a final written answer. "
            "Check **Activity · tools** for outputs, then try **Send** again or shorten your question."
        )
    if "tool_call" in kinds:
        return (
            "Tools were invoked but the run stopped before a final reply. "
            "If you do not see an **Approve / Reject** banner, try **Send** again once. "
            "Some mobile or in-app browsers drop long streams—use Chrome or Safari if it keeps happening."
        )
    return "(no reply)"


def _reply_sounds_like_hitl_wait(reply: str) -> bool:
    """True when the stream stub message should be replaced by the HITL banner copy."""
    r = (reply or "").strip()
    if not r or r == "(no reply)":
        return True
    if "run stopped before" in r:
        return True
    if "Tools were invoked but" in r:
        return True
    if "did not produce a final written answer" in r:
        return True
    return False


_HITL_STREAM_FALLBACK_REPLY = (
    "The assistant is waiting for your approval before running a Spotify or file action. "
    "Use **Approve** or **Reject** below (or POST /chat/resume)."
)


def _resolve_stream_reply(
    final_msgs: list,
    streamed_text_chunks: list[str],
    tool_trace: list[dict],
) -> str:
    from_state = (_last_ai_content(final_msgs) or "").strip()
    from_stream = _reply_from_stream_chunks(streamed_text_chunks).strip()
    # Prefer checkpoint text unless it is missing or the literal "(no reply)" while we did stream tokens.
    if from_stream:
        if not from_state or from_state == "(no reply)":
            return from_stream
        return from_state
    if from_state:
        return from_state
    return _empty_run_user_message(tool_trace)


def _sanitize_json_value(v):
    """Make a value JSON-serializable for tool_trace (best-effort)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): _sanitize_json_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_sanitize_json_value(x) for x in v]
    return str(v)


_TRACE_ARG_STR_MAX = 900
_TRACE_ARG_LIST_MAX = 24


def _truncate_trace_args(obj, *, depth: int = 0):
    """Keep tool_trace payloads small for the browser (e.g. long Spotify URI lists)."""
    if depth > 8:
        return "…"
    if isinstance(obj, str):
        if len(obj) > _TRACE_ARG_STR_MAX:
            return obj[:_TRACE_ARG_STR_MAX] + "…[truncated]"
        return obj
    if isinstance(obj, dict):
        return {str(k): _truncate_trace_args(v, depth=depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        if len(obj) > _TRACE_ARG_LIST_MAX:
            head = [_truncate_trace_args(x, depth=depth + 1) for x in obj[:_TRACE_ARG_LIST_MAX]]
            return head + [f"…[{len(obj) - _TRACE_ARG_LIST_MAX} more items truncated]"]
        return [_truncate_trace_args(x, depth=depth + 1) for x in obj]
    return obj


def _hitl_dict_from_interrupts(interrupts) -> dict | None:
    if not interrupts:
        return None
    first = interrupts[0]
    raw = first.value if hasattr(first, "value") else first
    if isinstance(raw, dict):
        return {
            "action_requests": raw.get("action_requests"),
            "review_configs": raw.get("review_configs"),
        }
    return {"value": str(raw)}


def _tool_trace_from_messages(messages, *, max_entries: int = 40) -> list[dict]:
    """Extract AIMessage tool_calls + ToolMessage results for LangChain-style UI cards.

    Mirrors the *shape* described in LangChain frontend tool-calling docs; this API
    is not LangGraph Server / useStream — see docs/FRONTEND_LANGCHAIN.md.
    """
    if not messages:
        return []
    try:
        from langchain_core.messages import AIMessage, ToolMessage
    except Exception:
        return []

    trace: list[dict] = []
    for m in messages:
        if isinstance(m, AIMessage):
            tcalls = getattr(m, "tool_calls", None) or []
            for tc in tcalls:
                if isinstance(tc, dict):
                    name = tc.get("name") or "?"
                    args = tc.get("args") or {}
                    tid = tc.get("id") or ""
                else:
                    name = getattr(tc, "name", None) or "?"
                    args = getattr(tc, "args", None) or {}
                    tid = getattr(tc, "id", None) or ""
                trace.append(
                    {
                        "kind": "tool_call",
                        "name": str(name),
                        "args": _truncate_trace_args(_sanitize_json_value(args)),
                        "id": str(tid),
                    }
                )
        elif isinstance(m, ToolMessage):
            content = m.content
            if not isinstance(content, str):
                content = str(content)
            trace.append(
                {
                    "kind": "tool_result",
                    "name": str(getattr(m, "name", "") or ""),
                    "tool_call_id": str(getattr(m, "tool_call_id", "") or ""),
                    "content": (content[:2000] + "…[truncated]") if len(content) > 2000 else content,
                }
            )
    return trace[-max_entries:]


def _content_to_text(content) -> str:
    """Normalize model content payloads into plain text.

    Deep Agents / provider streams often mix text blocks with ``function_call`` /
    tool blocks in ``content``. We must not fall back to ``str(list)`` — that
    leaked raw JSON into SSE tokens and the final ``reply``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype in _SKIP_CONTENT_BLOCK_TYPES:
                    continue
                text = block.get("text")
                if not (isinstance(text, str) and text.strip()):
                    alt = block.get("content")
                    if isinstance(alt, str) and alt.strip():
                        text = alt
                    else:
                        text = None
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif isinstance(block, str) and block.strip():
                parts.append(block.strip())
        return "\n\n".join(parts)
    return str(content)


@app.get("/")
def read_root():
    return {
        "ok": True,
        "usage": "POST /chat (JSON), POST /chat/stream (SSE), POST /chat/resume (HITL decisions)",
        "auth": "GET /auth/spotify, GET /auth/callback, GET /auth/status, POST /auth/logout, "
        "GET /auth/llm-keys, POST /auth/llm-keys",
    }


@app.get("/health")
def health():
    """Fast import path — use this to verify the API process is up (no agent load)."""
    return {"ok": True, "service": "spotify-llm-api"}


@app.get("/health/ready")
def health_ready():
    """True once DeepAgents has finished loading (or skipped if PREWARM_AGENT=0)."""
    return {
        "agent_ready": _default_agent_warmed,
        "prewarm_enabled": _prewarm_agent_enabled(),
    }


@app.get("/auth/spotify")
def spotify_login(request: Request):
    """Initiate Spotify OAuth login and return auth URL."""
    try:
        oauth = get_oauth()
        state = make_state()
        auth_url = oauth.get_authorize_url(state=state)
        return {"auth_url": auth_url}
    except Exception as e:
        _LOG.exception("Spotify OAuth setup failed")
        return JSONResponse(
            status_code=503,
            content={
                "auth_url": None,
                "error": str(e),
                "authenticated": False,
            },
        )


@app.get("/auth/callback")
def spotify_callback(request: Request, code: str | None = None, state: str | None = None):
    """Handle Spotify OAuth callback and persist user token."""
    frontend = os.environ.get("FRONTEND_URL", "http://127.0.0.1:3003")
    try:
        state_ok = verify_oauth_state(state)
        if not code or not state_ok:
            _LOG.warning(
                "Spotify OAuth callback rejected: code_present=%s state_verified=%s",
                bool(code),
                state_ok,
            )
            return RedirectResponse(f"{frontend}?spotify_auth=error")
        oauth = get_oauth()
        token_info = oauth.get_access_token(code)
        if not token_info or "access_token" not in token_info:
            return RedirectResponse(f"{frontend}?spotify_auth=error")
        import spotipy

        sp = spotipy.Spotify(auth=token_info["access_token"])
        user = sp.current_user()
        save_user_token(user, token_info)
        request.session["spotify_user"] = {
            "id": user.get("id", ""),
            "display_name": user.get("display_name", ""),
            "email": user.get("email", ""),
        }
        return RedirectResponse(f"{frontend}?spotify_auth=success")
    except Exception:
        return RedirectResponse(f"{frontend}?spotify_auth=error")


@app.get("/auth/status")
def auth_status(request: Request):
    uid = _session_user_id(request)
    if not uid:
        return {"authenticated": False, "user": None}
    profile = get_user_profile(uid)
    if not profile:
        # Keep signed-in state resilient when profile storage is temporarily unavailable.
        sess_user = request.session.get("spotify_user")
        if isinstance(sess_user, dict):
            profile = {
                "id": uid,
                "display_name": sess_user.get("display_name", ""),
                "email": sess_user.get("email", ""),
            }
        else:
            return {"authenticated": False, "user": None}
    from src.auth.user_llm_keys import public_status

    # Include BYOK flags in the same response so the UI does not depend on a second request
    # (avoids flaky /auth/llm-keys calls when cookies/proxy timing differs).
    return {"authenticated": True, "user": profile, "llm_keys": public_status(uid)}


@app.post("/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return {"success": True}


@app.get("/auth/llm-keys")
def auth_llm_keys_get(request: Request):
    """BYOK status for the signed-in Spotify user (no key material)."""
    uid = _session_user_id(request)
    if not uid:
        raise HTTPException(401, "Not authenticated")
    from src.auth.user_llm_keys import public_status

    return public_status(uid)


@app.post("/auth/llm-keys")
async def auth_llm_keys_post(request: Request):
    """Save or clear encrypted OpenAI / Anthropic keys (requires USER_LLM_KEYS_FERNET_KEY on the server)."""
    uid = _session_user_id(request)
    if not uid:
        raise HTTPException(401, "Not authenticated")
    from src.agent.factory import invalidate_user_agent_cache
    from src.auth.user_llm_keys import (
        byok_configured,
        encrypt_secret,
        fetch_encrypted_row,
        patch_llm_columns,
        public_status,
    )

    if not byok_configured():
        raise HTTPException(
            503,
            "BYOK is not configured: set USER_LLM_KEYS_FERNET_KEY on the API (see .env.example).",
        )
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Expected JSON body")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON object expected")

    o_raw = data.get("openai_key", _LLM_KEYS_BODY_MISSING)
    a_raw = data.get("anthropic_key", _LLM_KEYS_BODY_MISSING)
    if o_raw is _LLM_KEYS_BODY_MISSING and a_raw is _LLM_KEYS_BODY_MISSING and "provider" not in data:
        return {"ok": True, **public_status(uid)}

    row = fetch_encrypted_row(uid) or {}
    has_o = bool((row.get("llm_openai_key_encrypted") or "").strip())
    has_a = bool((row.get("llm_anthropic_key_encrypted") or "").strip())

    if o_raw is _LLM_KEYS_BODY_MISSING and a_raw is _LLM_KEYS_BODY_MISSING:
        if not (has_o and has_a):
            raise HTTPException(
                400,
                "Save at least one API key before changing provider alone, or include keys in this request.",
            )
        pv = data.get("provider")
        if not isinstance(pv, str) or not pv.strip():
            raise HTTPException(400, 'provider must be "openai" or "anthropic"')
        p = pv.strip().lower()
        if p not in ("openai", "anthropic"):
            raise HTTPException(400, 'provider must be "openai" or "anthropic"')
        patch_llm_columns(uid, touch_provider=True, provider=p)
        invalidate_user_agent_cache(uid)
        return {"ok": True, **public_status(uid)}

    clear_openai = False
    clear_anthropic = False
    set_o_enc: str | None = None
    set_a_enc: str | None = None

    if o_raw is not _LLM_KEYS_BODY_MISSING:
        if o_raw is None or (isinstance(o_raw, str) and not str(o_raw).strip()):
            clear_openai = True
            has_o = False
        else:
            if not isinstance(o_raw, str):
                raise HTTPException(400, "openai_key must be a string")
            set_o_enc = encrypt_secret(o_raw)
            has_o = True

    if a_raw is not _LLM_KEYS_BODY_MISSING:
        if a_raw is None or (isinstance(a_raw, str) and not str(a_raw).strip()):
            clear_anthropic = True
            has_a = False
        else:
            if not isinstance(a_raw, str):
                raise HTTPException(400, "anthropic_key must be a string")
            set_a_enc = encrypt_secret(a_raw)
            has_a = True

    prov_in = data.get("provider")
    existing_prov = row.get("llm_provider")
    if has_o and has_a:
        p = (prov_in if isinstance(prov_in, str) else "").strip().lower()
        if p not in ("openai", "anthropic"):
            if existing_prov in ("openai", "anthropic"):
                p = str(existing_prov)
            else:
                raise HTTPException(
                    400,
                    'When both keys are stored, set provider to "openai" or "anthropic" (which model to call).',
                )
        final_provider: str | None = p
    elif has_o:
        final_provider = "openai"
    elif has_a:
        final_provider = "anthropic"
    else:
        final_provider = None

    patch_llm_columns(
        uid,
        openai_encrypted=set_o_enc,
        anthropic_encrypted=set_a_enc,
        clear_openai=clear_openai,
        clear_anthropic=clear_anthropic,
        provider=final_provider,
        touch_provider=True,
    )
    invalidate_user_agent_cache(uid)
    return {"ok": True, **public_status(uid)}


@app.post("/chat")
async def chat_json(request: Request):
    """JSON-only chat. Always returns JSON (even on failure) so proxies/clients don't break on `res.json()`.

    Response shape:
      { "reply": str, "error"?: bool, "tool_trace": [...] }
    """
    tool_trace: list[dict] = []
    started = time.perf_counter()
    request_id = getattr(request.state, "request_id", "")
    try:
        try:
            data = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "reply": "Expected a JSON object with `from` and `body`.",
                    "error": True,
                    "tool_trace": [],
                },
            )
        parsed = _parse_chat_turn(data, request)
        if parsed is None:
            if not isinstance(data, dict):
                return JSONResponse(
                    status_code=400,
                    content={
                        "reply": "Chat body must be a JSON object.",
                        "error": True,
                        "tool_trace": [],
                    },
                )
            return JSONResponse(
                status_code=400,
                content={"reply": "Message body is empty.", "error": True, "tool_trace": []},
            )

        body_str, thread_id, session_uid, base_tid = parsed
        claim_err = _spotify_user_id_claim_error(data, session_uid)
        if claim_err is not None:
            return claim_err
        use_checkpoint = _use_persistent_checkpointer()
        previous = None if use_checkpoint else _thread_messages.get(thread_id)
        if _chat_debug_enabled():
            _LOG.info(
                "chat start request_id=%s thread_id=%s body_chars=%s checkpoint=%s",
                request_id,
                thread_id,
                len(body_str),
                use_checkpoint,
            )

        try:
            turn = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_agent_chat_sync,
                    body_str,
                    thread_id,
                    session_uid,
                    previous,
                ),
                timeout=_agent_timeout_seconds(),
            )
            messages = list(turn.get("messages") or [])
            hitl = turn.get("hitl")
            if not use_checkpoint:
                _thread_messages[thread_id] = messages
            tool_trace = _tool_trace_from_messages(messages)
            reply = _last_ai_content(messages) or "(no reply)"
            if hitl and _reply_sounds_like_hitl_wait(reply):
                reply = _HITL_STREAM_FALLBACK_REPLY
            if _chat_debug_enabled():
                _LOG.info(
                    "chat ok request_id=%s thread_id=%s elapsed_ms=%.1f tool_events=%s reply_chars=%s hitl=%s",
                    request_id,
                    thread_id,
                    (time.perf_counter() - started) * 1000.0,
                    len(tool_trace),
                    len(reply),
                    bool(hitl),
                )
            out = {"reply": reply, "tool_trace": tool_trace}
            if hitl:
                out["hitl_pending"] = True
                out["hitl"] = _sanitize_json_value(hitl)
            return out
        except asyncio.TimeoutError:
            if _chat_debug_enabled():
                _LOG.warning(
                    "chat timeout request_id=%s thread_id=%s elapsed_ms=%.1f",
                    request_id,
                    thread_id,
                    (time.perf_counter() - started) * 1000.0,
                )
            return JSONResponse(
                status_code=504,
                content={
                    "reply": "The model call timed out. Please try again.",
                    "error": True,
                    "tool_trace": tool_trace,
                },
            )
        except Exception as e:
            _LOG.exception("chat agent run failed")
            if _chat_debug_enabled():
                _LOG.error(
                    "chat error request_id=%s thread_id=%s elapsed_ms=%.1f",
                    request_id,
                    thread_id,
                    (time.perf_counter() - started) * 1000.0,
                )
            return JSONResponse(
                status_code=500,
                content={
                    "reply": f"Agent error: {e}",
                    "error": True,
                    "tool_trace": tool_trace,
                },
            )
    except Exception as e:
        # Session middleware, context setup, or other unexpected failures
        _LOG.exception("chat endpoint failed")
        return JSONResponse(
            status_code=500,
            content={
                "reply": f"Server error: {e}",
                "error": True,
                "tool_trace": tool_trace,
            },
        )


@app.post("/chat/resume")
async def chat_resume(request: Request):
    """Resume the Deep Agent after a human-in-the-loop interrupt (same thread_id as /chat).

    Body: `{ "from", "conversation_id"?, "decisions": [ {"type":"approve"|"reject"|"edit", ...}, ... ] }`
    See https://docs.langchain.com/oss/python/deepagents/human-in-the-loop
    """
    tool_trace: list[dict] = []
    started = time.perf_counter()
    request_id = getattr(request.state, "request_id", "")
    try:
        try:
            data = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "reply": "Expected JSON with `from`, optional `conversation_id`, and `decisions` (non-empty list).",
                    "error": True,
                    "tool_trace": [],
                },
            )
        parsed = _parse_chat_resume(data, request)
        if parsed is None:
            return JSONResponse(
                status_code=400,
                content={
                    "reply": "Invalid resume body: need non-empty `decisions` list.",
                    "error": True,
                    "tool_trace": [],
                },
            )
        thread_id, session_uid, base_tid, decisions = parsed
        claim_err = _spotify_user_id_claim_error(data, session_uid)
        if claim_err is not None:
            return claim_err
        use_checkpoint = _use_persistent_checkpointer()
        if not use_checkpoint:
            return JSONResponse(
                status_code=400,
                content={
                    "reply": "HITL resume requires LangGraph checkpointing (enable DEEPAGENTS_HITL or set CHECKPOINT_DATABASE_URL).",
                    "error": True,
                    "tool_trace": [],
                },
            )
        if _chat_debug_enabled():
            _LOG.info(
                "chat resume request_id=%s thread_id=%s decisions=%s",
                request_id,
                thread_id,
                len(decisions),
            )
        try:
            turn = await asyncio.wait_for(
                asyncio.to_thread(_run_agent_resume_sync, decisions, thread_id, session_uid),
                timeout=_agent_timeout_seconds(),
            )
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=504,
                content={
                    "reply": "Resume timed out. Please try again.",
                    "error": True,
                    "tool_trace": tool_trace,
                },
            )
        except Exception as e:
            _LOG.exception("chat resume agent run failed")
            return JSONResponse(
                status_code=500,
                content={
                    "reply": f"Agent error: {e}",
                    "error": True,
                    "tool_trace": tool_trace,
                },
            )
        messages = list(turn.get("messages") or [])
        hitl = turn.get("hitl")
        tool_trace = _tool_trace_from_messages(messages)
        reply = _last_ai_content(messages) or "(no reply)"
        if hitl and _reply_sounds_like_hitl_wait(reply):
            reply = "Further approval is required before the next action can run."
        if _chat_debug_enabled():
            _LOG.info(
                "chat resume ok request_id=%s thread_id=%s elapsed_ms=%.1f hitl=%s",
                request_id,
                thread_id,
                (time.perf_counter() - started) * 1000.0,
                bool(hitl),
            )
        out = {"reply": reply, "tool_trace": tool_trace}
        if hitl:
            out["hitl_pending"] = True
            out["hitl"] = _sanitize_json_value(hitl)
        return out
    except Exception as e:
        _LOG.exception("chat resume endpoint failed")
        return JSONResponse(
            status_code=500,
            content={
                "reply": f"Server error: {e}",
                "error": True,
                "tool_trace": tool_trace,
            },
        )


@app.post("/chat/stream")
async def chat_stream(request: Request):
    """SSE: Deep Agents `stream(..., version='v2')` token deltas + final `tool_trace`.

    See https://docs.langchain.com/oss/python/deepagents/streaming
    """
    tool_trace: list[dict] = []
    request_id = getattr(request.state, "request_id", "")
    try:
        try:
            data = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "reply": "Expected a JSON object with `from` and `body`.",
                    "error": True,
                    "tool_trace": [],
                },
            )
        parsed = _parse_chat_turn(data, request)
        if parsed is None:
            if not isinstance(data, dict):
                return JSONResponse(
                    status_code=400,
                    content={
                        "reply": "Chat body must be a JSON object.",
                        "error": True,
                        "tool_trace": [],
                    },
                )
            return JSONResponse(
                status_code=400,
                content={"reply": "Message body is empty.", "error": True, "tool_trace": []},
            )

        body_str, thread_id, session_uid, base_tid = parsed
        claim_err = _spotify_user_id_claim_error(data, session_uid)
        if claim_err is not None:
            return claim_err
        use_checkpoint = _use_persistent_checkpointer()
        previous = None if use_checkpoint else _thread_messages.get(thread_id)

        def event_gen():
            from langchain_core.messages import AIMessageChunk

            set_spotify_user_context(session_uid)
            try:
                _ensure_agent_loaded()
                from src.agent.factory import get_agent_for_spotify_user

                agent = get_agent_for_spotify_user(session_uid)
                final_msgs: list = []
                streamed_chunks: list[str] = []
                try:
                    for part in stream_chat_chunks(agent, body_str, thread_id, previous):
                        if not isinstance(part, dict) or part.get("type") != "messages":
                            continue
                        tup = part.get("data")
                        if not isinstance(tup, tuple) or not tup:
                            continue
                        token = tup[0]
                        if isinstance(token, AIMessageChunk):
                            if token.content:
                                txt = _content_to_text(token.content)
                                if txt:
                                    streamed_chunks.append(txt)
                                    yield _sse("token", {"text": txt})
                            for tc in getattr(token, "tool_call_chunks", None) or []:
                                nm = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                                if nm:
                                    yield _sse("tool", {"name": str(nm)})
                    hitl_raw: dict | None = None
                    try:
                        snap = agent.get_state({"configurable": {"thread_id": thread_id}})
                        if snap and getattr(snap, "values", None):
                            vals = snap.values
                            if isinstance(vals, dict):
                                final_msgs = list(vals.get("messages") or [])
                        if snap and getattr(snap, "interrupts", None):
                            hitl_raw = _hitl_dict_from_interrupts(snap.interrupts)
                    except Exception:
                        _LOG.exception("chat stream get_state failed")
                        final_msgs = []
                    if not use_checkpoint and final_msgs:
                        _thread_messages[thread_id] = final_msgs
                    trace = _tool_trace_from_messages(final_msgs)
                    reply = _resolve_stream_reply(final_msgs, streamed_chunks, trace)
                    if hitl_raw and _reply_sounds_like_hitl_wait(reply):
                        reply = _HITL_STREAM_FALLBACK_REPLY
                    done_payload: dict = {"reply": reply, "tool_trace": trace, "request_id": request_id}
                    if hitl_raw:
                        done_payload["hitl_pending"] = True
                        done_payload["hitl"] = _sanitize_json_value(hitl_raw)
                    yield _sse("done", done_payload)
                except Exception as e:
                    _LOG.exception("chat stream run failed")
                    yield _sse("error", {"message": str(e), "request_id": request_id})
            finally:
                set_spotify_user_context(None)

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Request-Id": request_id,
            },
        )
    except Exception as e:
        _LOG.exception("chat stream endpoint failed")
        return JSONResponse(
            status_code=500,
            content={"reply": f"Server error: {e}", "error": True, "tool_trace": tool_trace},
        )
