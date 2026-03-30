"""Supabase-backed vector memory for Spotify taste profiles."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

import httpx


def _supabase_url() -> str:
    return (
        os.environ.get("SUPABASE_URL")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        or ""
    ).strip()


def _supabase_key() -> str:
    return (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()


def _headers() -> dict[str, str]:
    url = _supabase_url()
    key = _supabase_key()
    if not url or not key:
        raise ValueError(
            "Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _embed_texts(texts: list[str]) -> list[list[float]]:
    # Keep this implementation explicit and simple: we currently rely on OpenAI
    # embeddings for taste-memory retrieval quality.
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is required for taste-memory embeddings")
    from langchain_openai import OpenAIEmbeddings

    embedder = OpenAIEmbeddings(model=os.environ.get("TASTE_EMBEDDING_MODEL", "text-embedding-3-small"))
    return embedder.embed_documents(texts)


@dataclass
class MemoryDoc:
    source: str
    text: str
    metadata: dict


def ingest_memory_docs(user_id: str, docs: Iterable[MemoryDoc]) -> int:
    docs = list(docs)
    if not docs:
        return 0
    vectors = _embed_texts([d.text for d in docs])
    url = _supabase_url().rstrip("/")
    headers = _headers()
    rows = []
    for doc, vec in zip(docs, vectors):
        rows.append(
            {
                "user_id": user_id,
                "source": doc.source,
                "text": doc.text,
                "metadata": (doc.metadata or {}),
                # pgvector literal format
                "embedding": "[" + ",".join(str(x) for x in vec) + "]",
            }
        )
    resp = httpx.post(
        f"{url}/rest/v1/taste_memory",
        headers={**headers, "Prefer": "return=minimal"},
        json=rows,
        timeout=30.0,
    )
    resp.raise_for_status()
    return len(docs)


def retrieve_memory_docs(user_id: str, query: str, k: int = 5) -> list[dict]:
    if k <= 0:
        return []
    query_vec = _embed_texts([query])[0]
    url = _supabase_url().rstrip("/")
    headers = _headers()
    res = httpx.post(
        f"{url}/rest/v1/rpc/match_taste_memory",
        headers=headers,
        json={
            "query_embedding": "[" + ",".join(str(x) for x in query_vec) + "]",
            "match_user_id": user_id,
            "match_count": max(1, min(k, 50)),
        },
        timeout=30.0,
    )
    res.raise_for_status()
    data = res.json() or []
    out = []
    for row in data:
        out.append(
            {
                "score": row.get("score", 0.0),
                "source": row.get("source", ""),
                "text": row.get("text", ""),
                "metadata": row.get("metadata") or {},
                "created_at": row.get("created_at"),
            }
        )
    return out

