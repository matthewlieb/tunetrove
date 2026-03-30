# LangChain-style frontend (tool calling & HITL)

This project’s chat API is a **custom** FastAPI `POST /chat` (and optional **SSE** `POST /chat/stream` for token streaming) — not the LangGraph Platform HTTP API that [`useStream`](https://docs.langchain.com/oss/python/langchain/frontend/tool-calling) expects (`apiUrl` + `assistantId`).

**Keep FastAPI for this app:** it owns Spotify OAuth, session cookies, and a stable contract for the Next proxy. Adopting the LangGraph dev server / Platform would be an **optional second path** (same graph, different transport) if you want doc-identical `useStream` and HITL resume without reimplementing those endpoints.

We still follow the **same UX patterns** from the official docs:

## Tool calling UI

- **Docs:** [Tool calling (LangChain)](https://docs.langchain.com/oss/python/langchain/frontend/tool-calling)
- **This repo:** After each turn, the API returns `tool_trace`: an array of `tool_call` / `tool_result` entries derived from `AIMessage.tool_calls` and `ToolMessage` in the final message list. The Next.js app renders them as compact cards under the assistant reply (`apps/web/components/ToolTrace.tsx`).

Shape (subset; see `src/web/app.py` → `_tool_trace_from_messages`):

```json
{
  "reply": "…",
  "tool_trace": [
    { "kind": "tool_call", "name": "spotify_search", "args": { … }, "id": "…" },
    { "kind": "tool_result", "name": "spotify_search", "tool_call_id": "…", "content": "…" }
  ]
}
```

Errors always return JSON too: `{ "reply": "…", "error": true, "tool_trace": [] }` with appropriate HTTP status, so the UI never breaks on `res.json()`.

## Human-in-the-loop (HITL)

- **Docs:** [Human-in-the-Loop](https://docs.langchain.com/oss/python/langchain/frontend/human-in-the-loop)
- **This repo:** **Not wired yet.** Full HITL as in the docs needs graph **interrupts**, a **resume** command on the backend, and a client that calls `stream.submit(null, { command: { resume: response } })` (or equivalent against LangGraph Server).

**Options to get doc-identical HITL later:**

1. Expose the agent via **LangGraph dev server / Platform** and use `@langchain/react` `useStream` from the Next app, **or**
2. Implement interrupts in the DeepAgents/LangGraph graph and add endpoints such as `POST /chat/resume` that mirror your interrupt payload.

Until then, treat destructive Spotify actions as guarded by prompts/tools only (no approval UI).

## References

- [Tool calling](https://docs.langchain.com/oss/python/langchain/frontend/tool-calling)
- [Human-in-the-Loop](https://docs.langchain.com/oss/python/langchain/frontend/human-in-the-loop)
