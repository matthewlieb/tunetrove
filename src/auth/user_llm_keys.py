"""Encrypted per-user OpenAI / Anthropic keys (BYOK) stored in Supabase."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from src.auth.spotify_auth import _headers, _supabase_key, _supabase_url

Provider = Literal["openai", "anthropic"]


def byok_configured() -> bool:
    """True when the API can encrypt/decrypt user keys (set ``USER_LLM_KEYS_FERNET_KEY``)."""
    return bool((os.environ.get("USER_LLM_KEYS_FERNET_KEY") or "").strip())


def _fernet():
    from cryptography.fernet import Fernet

    raw = (os.environ.get("USER_LLM_KEYS_FERNET_KEY") or "").strip()
    if not raw:
        return None
    return Fernet(raw.encode("ascii"))


def encrypt_secret(plain: str) -> str:
    f = _fernet()
    if f is None:
        raise ValueError("USER_LLM_KEYS_FERNET_KEY is not set")
    return f.encrypt(plain.strip().encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    f = _fernet()
    if f is None:
        raise ValueError("USER_LLM_KEYS_FERNET_KEY is not set")
    return f.decrypt(token.encode("ascii")).decode("utf-8")


@dataclass
class UserLlmSecrets:
    openai_key: str | None
    anthropic_key: str | None
    provider: Provider | None


def fetch_encrypted_row(user_id: str) -> dict[str, Any] | None:
    import httpx

    if not _supabase_url().strip() or not _supabase_key():
        return None
    url = _supabase_url().rstrip("/")
    headers = _headers()
    resp = httpx.get(
        f"{url}/rest/v1/spotify_users",
        headers=headers,
        params={
            "select": "llm_openai_key_encrypted,llm_anthropic_key_encrypted,llm_provider",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    rows = resp.json() or []
    return rows[0] if rows else None


def load_decrypted_secrets(user_id: str) -> UserLlmSecrets | None:
    if not byok_configured():
        return None
    row = fetch_encrypted_row(user_id)
    if not row:
        return None
    o_enc = row.get("llm_openai_key_encrypted")
    a_enc = row.get("llm_anthropic_key_encrypted")
    prov = row.get("llm_provider")
    if not o_enc and not a_enc:
        return None
    try:
        o = decrypt_secret(o_enc) if isinstance(o_enc, str) and o_enc.strip() else None
        a = decrypt_secret(a_enc) if isinstance(a_enc, str) and a_enc.strip() else None
    except Exception:
        return None
    p: Provider | None = None
    if prov in ("openai", "anthropic"):
        p = prov  # type: ignore[assignment]
    return UserLlmSecrets(openai_key=o, anthropic_key=a, provider=p)


def patch_llm_columns(
    user_id: str,
    *,
    openai_encrypted: str | None = None,
    anthropic_encrypted: str | None = None,
    provider: str | None = None,
    clear_openai: bool = False,
    clear_anthropic: bool = False,
    touch_provider: bool = False,
) -> None:
    import httpx

    url = _supabase_url().rstrip("/")
    headers = _headers()
    body: dict[str, Any] = {}
    if clear_openai:
        body["llm_openai_key_encrypted"] = None
    elif openai_encrypted is not None:
        body["llm_openai_key_encrypted"] = openai_encrypted
    if clear_anthropic:
        body["llm_anthropic_key_encrypted"] = None
    elif anthropic_encrypted is not None:
        body["llm_anthropic_key_encrypted"] = anthropic_encrypted
    if touch_provider:
        body["llm_provider"] = provider
    if not body:
        return
    resp = httpx.patch(
        f"{url}/rest/v1/spotify_users",
        headers={**headers, "Prefer": "return=minimal"},
        params={"user_id": f"eq.{user_id}"},
        json=body,
        timeout=30.0,
    )
    resp.raise_for_status()


def public_status(user_id: str) -> dict[str, Any]:
    """Safe to return to the client (no key material)."""
    if not byok_configured():
        return {
            "byok_server_enabled": False,
            "has_openai": False,
            "has_anthropic": False,
            "provider": None,
        }
    try:
        row = fetch_encrypted_row(user_id)
    except Exception:
        # Supabase misconfig, missing columns, or transient HTTP errors — still return 200 so the UI can load.
        return {
            "byok_server_enabled": True,
            "has_openai": False,
            "has_anthropic": False,
            "provider": None,
            "key_status_degraded": True,
        }
    if not row:
        return {
            "byok_server_enabled": True,
            "has_openai": False,
            "has_anthropic": False,
            "provider": None,
        }
    prov = row.get("llm_provider")
    return {
        "byok_server_enabled": True,
        "has_openai": bool((row.get("llm_openai_key_encrypted") or "").strip()),
        "has_anthropic": bool((row.get("llm_anthropic_key_encrypted") or "").strip()),
        "provider": prov if prov in ("openai", "anthropic") else None,
    }
