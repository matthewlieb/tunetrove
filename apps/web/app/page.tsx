"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ChatMarkdown } from "../components/ChatMarkdown";
import { ToolTrace, normalizeToolTrace, type ToolTraceEntry } from "../components/ToolTrace";
import { agentApiLabel, agentApiUrl, fetchWithTimeout } from "../lib/agent-api";
import { readResponseBody } from "../lib/parse-api-response";

type ChatMsg = {
  role: "user" | "assistant" | "system";
  content: string;
  toolTrace?: ToolTraceEntry[];
};

type LlmKeyStatus = {
  byok_server_enabled?: boolean;
  has_openai?: boolean;
  has_anthropic?: boolean;
  provider?: string | null;
  /** True when Supabase row fetch failed; saving keys may still work. */
  key_status_degraded?: boolean;
};

/** When `/auth/status` omits `llm_keys` (older API) — never block the BYOK form. */
const LLM_STATUS_IF_EMBED_MISSING: LlmKeyStatus = {
  byok_server_enabled: true,
  has_openai: false,
  has_anthropic: false,
  provider: null,
};

function formatByokSaveError(res: Response, data: Record<string, unknown> | null): string {
  const d = data?.detail;
  let detailStr = "";
  if (typeof d === "string") detailStr = d;
  else if (Array.isArray(d) && d.length) {
    const first = d[0];
    if (first && typeof first === "object" && "msg" in first) {
      detailStr = String((first as { msg: unknown }).msg);
    } else {
      detailStr = JSON.stringify(d);
    }
  }
  const low = detailStr.toLowerCase();
  if (res.status === 404 || low === "not found" || low.includes("not found")) {
    return "Could not reach the save-keys route (404). Start the agent API (e.g. port 8013), check AGENT_API_URL / the /api/agent proxy, and redeploy if routes are missing.";
  }
  if (res.status === 401) return "Session expired — connect Spotify again, then save.";
  if (res.status === 503) return "BYOK is not enabled on this server (USER_LLM_KEYS_FERNET_KEY).";
  if (detailStr) return detailStr;
  return `Save failed (HTTP ${res.status}).`;
}

const T_HEALTH_MS = 15_000;
/** OAuth + cold spotipy import; must exceed proxy upstream GET timeout (90s). */
const T_AUTH_MS = 120_000;
/** Must exceed Next proxy budget for POST /chat and /chat/stream (see route.ts — 600s for chat). */
const T_CHAT_MS = 620_000;
const CHAT_SESSIONS_PREFIX = "spotify-llm-chat-sessions-v2::";
const CHAT_MESSAGES_PREFIX_SCOPED = "spotify-llm-messages-v4::";
const CHAT_ACTIVE_CONV_PREFIX = "spotify-llm-active-conv-v2::";
/** Legacy keys (pre–per-user scope) — migrated once into the active scope. */
const LEGACY_CHAT_SESSIONS_KEY = "spotify-llm-chat-sessions-v1";
const LEGACY_MESSAGES_PREFIX = "spotify-llm-messages-v3::";
const LEGACY_ACTIVE_CONV_KEY = "spotify-llm-active-conv";
const CONVERSATION_ID_KEY = "spotify-llm-conversation-id";
const FROM_ID_KEY = "spotify-llm-from-id";
const MAX_CHAT_SESSIONS = 40;
const APP_NAME = (process.env.NEXT_PUBLIC_APP_NAME || "TempoTrove").trim();

type ChatSessionMeta = { id: string; title: string; updatedAt: number };

function sessionsStorageKey(scope: string) {
  return `${CHAT_SESSIONS_PREFIX}${scope}`;
}

function chatMessagesStorageKey(scope: string, convId: string) {
  return `${CHAT_MESSAGES_PREFIX_SCOPED}${scope}::${convId}`;
}

function activeConvStorageKey(scope: string) {
  return `${CHAT_ACTIVE_CONV_PREFIX}${scope}`;
}

function normalizeSessionRows(p: unknown): ChatSessionMeta[] {
  if (!Array.isArray(p)) return [];
  return p
    .filter((x) => x && typeof (x as ChatSessionMeta).id === "string")
    .map((x) => {
      const o = x as ChatSessionMeta;
      return {
        id: o.id,
        title: typeof o.title === "string" ? o.title : "Chat",
        updatedAt: typeof o.updatedAt === "number" ? o.updatedAt : Date.now(),
      };
    });
}

/** Copy legacy global chat keys into `scope` if the scoped index is still empty. */
function migrateLegacyChatsIntoScope(scope: string): void {
  if (typeof window === "undefined") return;
  try {
    const sk = sessionsStorageKey(scope);
    if (window.localStorage.getItem(sk)) return;

    let raw = window.localStorage.getItem(LEGACY_CHAT_SESSIONS_KEY);
    if (!raw) {
      const legacyConv = window.localStorage.getItem(CONVERSATION_ID_KEY)?.trim();
      if (legacyConv) {
        const one: ChatSessionMeta = { id: legacyConv, title: "Chat", updatedAt: Date.now() };
        raw = JSON.stringify([one]);
      } else {
        return;
      }
    }
    const sessions = normalizeSessionRows(JSON.parse(raw) as unknown);
    if (!sessions.length) return;

    for (const s of sessions) {
      const oldMsg = window.localStorage.getItem(`${LEGACY_MESSAGES_PREFIX}${s.id}`);
      if (oldMsg) {
        window.localStorage.setItem(chatMessagesStorageKey(scope, s.id), oldMsg);
      }
    }
    window.localStorage.setItem(sk, JSON.stringify(sessions));
    const legacyActive = window.localStorage.getItem(LEGACY_ACTIVE_CONV_KEY)?.trim();
    if (legacyActive && sessions.some((x) => x.id === legacyActive)) {
      window.localStorage.setItem(activeConvStorageKey(scope), legacyActive);
    }
  } catch {
    /* ignore */
  }
}

function readChatSessions(scope: string): ChatSessionMeta[] {
  if (typeof window === "undefined") return [];
  try {
    migrateLegacyChatsIntoScope(scope);
    const raw = window.localStorage.getItem(sessionsStorageKey(scope));
    if (raw) {
      const sessions = normalizeSessionRows(JSON.parse(raw) as unknown);
      if (sessions.length) return sessions;
    }
  } catch {
    /* ignore */
  }
  const id = crypto.randomUUID();
  const one: ChatSessionMeta = { id, title: "New chat", updatedAt: Date.now() };
  try {
    window.localStorage.setItem(sessionsStorageKey(scope), JSON.stringify([one]));
    window.localStorage.setItem(activeConvStorageKey(scope), id);
  } catch {
    /* ignore */
  }
  return [one];
}

function writeChatSessions(scope: string, sessions: ChatSessionMeta[]) {
  try {
    window.localStorage.setItem(sessionsStorageKey(scope), JSON.stringify(sessions.slice(0, MAX_CHAT_SESSIONS)));
  } catch {
    /* ignore */
  }
}

function initialSidebarChatStateForScope(scope: string): { sessions: ChatSessionMeta[]; activeId: string } {
  if (typeof window === "undefined") {
    return { sessions: [{ id: "", title: "New chat", updatedAt: 0 }], activeId: "" };
  }
  const sessions = readChatSessions(scope);
  const active = window.localStorage.getItem(activeConvStorageKey(scope))?.trim();
  const activeId =
    (active && sessions.some((x) => x.id === active) ? active : null) || sessions[0]?.id || "";
  return { sessions, activeId };
}

const DEFAULT_SYSTEM_MESSAGE =
  "Ask for recommendations, then say things like “add those to my playlist” or “create a playlist called Discovery”.";

/** Set NEXT_PUBLIC_CHAT_STREAM=0 to use non-streaming POST /chat only. */
const USE_CHAT_STREAM = process.env.NEXT_PUBLIC_CHAT_STREAM !== "0";

function hitlActionCount(hitl: unknown): number {
  if (!hitl || typeof hitl !== "object") return 1;
  const ar = (hitl as { action_requests?: unknown }).action_requests;
  return Array.isArray(ar) && ar.length > 0 ? ar.length : 1;
}

function hitlSummary(hitl: unknown): string {
  if (!hitl || typeof hitl !== "object") return "Pending tool execution";
  const ar = (hitl as { action_requests?: { name?: string }[] }).action_requests;
  if (!Array.isArray(ar) || ar.length === 0) return "Pending tool execution";
  const name = ar[0]?.name ?? "tool";
  return ar.length > 1 ? `${name} (+${ar.length - 1} more)` : name;
}

function parseSseChunks(buffer: string): {
  events: { event: string; data: Record<string, unknown> }[];
  rest: string;
} {
  const events: { event: string; data: Record<string, unknown> }[] = [];
  const norm = buffer.replace(/\r\n/g, "\n");
  const parts = norm.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const block of parts) {
    if (!block.trim()) continue;
    let ev = "message";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) ev = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    const raw = dataLines.join("\n");
    if (!raw) continue;
    try {
      events.push({ event: ev, data: JSON.parse(raw) as Record<string, unknown> });
    } catch {
      /* malformed chunk */
    }
  }
  return { events, rest };
}

const TOOLS_PANEL_LS = "tempotrove-tools-panel";

export default function HomePage() {
  const [fromId] = useState(() => {
    if (typeof window === "undefined") return `web-${crypto.randomUUID()}`;
    const existing = window.localStorage.getItem(FROM_ID_KEY);
    if (existing && existing.trim()) return existing;
    const next = `web-${crypto.randomUUID()}`;
    window.localStorage.setItem(FROM_ID_KEY, next);
    return next;
  });
  const [chatSessions, setChatSessions] = useState<ChatSessionMeta[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [authBusy, setAuthBusy] = useState(false);
  const [apiReachable, setApiReachable] = useState<boolean | null>(null);
  const [spotifyUser, setSpotifyUser] = useState<{ id: string; display_name?: string } | null>(null);
  const [agentWarmup, setAgentWarmup] = useState<{ ready: boolean; prewarm: boolean } | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([
    {
      role: "system",
      content: DEFAULT_SYSTEM_MESSAGE,
    },
  ]);
  const [activityTrace, setActivityTrace] = useState<ToolTraceEntry[]>([]);
  /** Human-in-the-loop: same LangGraph thread until user approves or rejects (POST /chat/resume). */
  const [pendingHitl, setPendingHitl] = useState<Record<string, unknown> | null>(null);
  const [llmKeyStatus, setLlmKeyStatus] = useState<LlmKeyStatus | null>(null);
  /** After first `/auth/status` completes (chat sidebar keys off Spotify user id / anon). */
  const [authReady, setAuthReady] = useState(false);
  const [llmOpenaiInput, setLlmOpenaiInput] = useState("");
  const [llmAnthropicInput, setLlmAnthropicInput] = useState("");
  const [llmProviderChoice, setLlmProviderChoice] = useState<"openai" | "anthropic">("openai");
  const [llmKeysBusy, setLlmKeysBusy] = useState(false);
  const [llmKeysInlineError, setLlmKeysInlineError] = useState<string | null>(null);
  const [showLlmOpenai, setShowLlmOpenai] = useState(false);
  const [showLlmAnthropic, setShowLlmAnthropic] = useState(false);

  const [toolsOpen, setToolsOpenState] = useState(() => {
    if (typeof window === "undefined") return true;
    try {
      return localStorage.getItem(TOOLS_PANEL_LS) !== "0";
    } catch {
      return true;
    }
  });
  const setToolsOpen = useCallback((open: boolean) => {
    setToolsOpenState(open);
    try {
      localStorage.setItem(TOOLS_PANEL_LS, open ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, []);

  const list = useMemo(() => messages.filter((m) => m.role !== "system"), [messages]);
  const spotifyUserLabel = useMemo(() => {
    if (!spotifyUser) return "";
    const id = (spotifyUser.id || "").trim();
    const name = (spotifyUser.display_name || "").trim();
    if (name && id && name !== id) return `${name} (@${id})`;
    return name || id || "";
  }, [spotifyUser]);
  const sortedSessions = useMemo(
    () => [...chatSessions].sort((a, b) => b.updatedAt - a.updatedAt),
    [chatSessions],
  );
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const [compact, setCompact] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia("(max-width: 768px)");
    const sync = () => setCompact(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  const storageScope = spotifyUser?.id ?? "anon";

  useEffect(() => {
    if (!authReady) return;
    const { sessions, activeId } = initialSidebarChatStateForScope(storageScope);
    setChatSessions(sessions);
    setConversationId(activeId);
  }, [authReady, storageScope]);

  const touchSession = useCallback(
    (id: string, title?: string) => {
      const scope = spotifyUser?.id ?? "anon";
      const now = Date.now();
      setChatSessions((prev) => {
        const next = prev.map((s) =>
          s.id === id
            ? { ...s, updatedAt: now, ...(title !== undefined ? { title } : {}) }
            : s,
        );
        next.sort((a, b) => b.updatedAt - a.updatedAt);
        writeChatSessions(scope, next);
        return next;
      });
    },
    [spotifyUser?.id],
  );

  const newChat = useCallback(() => {
    const scope = spotifyUser?.id ?? "anon";
    const id = crypto.randomUUID();
    const row: ChatSessionMeta = { id, title: "New chat", updatedAt: Date.now() };
    setChatSessions((prev) => {
      const next = [row, ...prev].slice(0, MAX_CHAT_SESSIONS);
      writeChatSessions(scope, next);
      return next;
    });
    setConversationId(id);
    setMessages([{ role: "system", content: DEFAULT_SYSTEM_MESSAGE }]);
    setActivityTrace([]);
    setPendingHitl(null);
  }, [spotifyUser?.id]);

  const selectChat = useCallback(
    (id: string) => {
      if (id === conversationId || busy) return;
      setConversationId(id);
      setActivityTrace([]);
      setPendingHitl(null);
    },
    [conversationId, busy],
  );

  const deleteChat = useCallback(
    (id: string, e: React.MouseEvent) => {
      e.stopPropagation();
      if (busy) return;
      const scope = spotifyUser?.id ?? "anon";
      try {
        window.localStorage.removeItem(chatMessagesStorageKey(scope, id));
      } catch {
        /* ignore */
      }
      setChatSessions((prev) => {
        const filtered = prev.filter((s) => s.id !== id);
        const sorted =
          filtered.length > 0
            ? [...filtered].sort((a, b) => b.updatedAt - a.updatedAt)
            : [{ id: crypto.randomUUID(), title: "New chat", updatedAt: Date.now() }];
        writeChatSessions(scope, sorted);
        if (id === conversationId) {
          const pick = sorted[0]!.id;
          queueMicrotask(() => {
            setConversationId(pick);
            setPendingHitl(null);
          });
        }
        return sorted;
      });
    },
    [busy, conversationId, spotifyUser?.id],
  );

  async function mergeLlmFromDedicatedEndpointQuiet() {
    try {
      const res = await fetchWithTimeout(agentApiUrl("/auth/llm-keys"), { credentials: "include" }, T_AUTH_MS);
      if (!res.ok) return;
      const j = (await res.json()) as LlmKeyStatus;
      if (j && typeof j === "object") setLlmKeyStatus(j);
    } catch {
      /* keep status from /auth/status or LLM_STATUS_IF_EMBED_MISSING */
    }
  }

  async function refreshAuthStatus() {
    try {
      const res = await fetchWithTimeout(
        agentApiUrl("/auth/status"),
        { credentials: "include" },
        T_AUTH_MS,
      );
      const data = (await res.json()) as {
        authenticated?: boolean;
        user?: { id: string; display_name?: string } | null;
        llm_keys?: LlmKeyStatus;
      };
      if (data.authenticated && data.user) {
        setSpotifyUser(data.user);
        const keys =
          data.llm_keys && typeof data.llm_keys === "object" ? data.llm_keys : LLM_STATUS_IF_EMBED_MISSING;
        setLlmKeyStatus(keys);
        if (!data.llm_keys || typeof data.llm_keys !== "object") {
          void mergeLlmFromDedicatedEndpointQuiet();
        }
      } else {
        setSpotifyUser(null);
        setLlmKeyStatus(null);
      }
    } catch {
      setSpotifyUser(null);
      setLlmKeyStatus(null);
    } finally {
      setAuthReady(true);
    }
  }

  useEffect(() => {
    const p = llmKeyStatus?.provider;
    if (p === "openai" || p === "anthropic") setLlmProviderChoice(p);
  }, [llmKeyStatus?.provider]);

  useEffect(() => {
    let cancelled = false;
    const warmPoll: { id?: number } = {};

    const applyReadyPayload = (data: { agent_ready?: boolean; prewarm_enabled?: boolean }) => {
      const ready = Boolean(data.agent_ready);
      const prewarm = data.prewarm_enabled !== false;
      if (!cancelled) setAgentWarmup({ ready, prewarm });
      if (ready && warmPoll.id !== undefined) {
        window.clearInterval(warmPoll.id);
        warmPoll.id = undefined;
      }
    };

    warmPoll.id = window.setInterval(() => {
      void (async () => {
        try {
          const r = await fetchWithTimeout(agentApiUrl("/health/ready"), { credentials: "omit" }, T_HEALTH_MS);
          if (cancelled || !r.ok) return;
          const data = (await r.json()) as { agent_ready?: boolean; prewarm_enabled?: boolean };
          if (!cancelled && data.agent_ready) applyReadyPayload(data);
        } catch {
          /* ignore */
        }
      })();
    }, 4000);

    void (async () => {
      try {
        const r = await fetchWithTimeout(agentApiUrl("/health"), { credentials: "omit" }, T_HEALTH_MS);
        if (!cancelled) setApiReachable(r.ok);
      } catch {
        if (!cancelled) setApiReachable(false);
      }
    })();
    void (async () => {
      try {
        const r = await fetchWithTimeout(agentApiUrl("/health/ready"), { credentials: "omit" }, T_HEALTH_MS);
        if (cancelled || !r.ok) return;
        const data = (await r.json()) as { agent_ready?: boolean; prewarm_enabled?: boolean };
        applyReadyPayload(data);
      } catch {
        if (!cancelled) setAgentWarmup(null);
      }
    })();
    void refreshAuthStatus();
    const params = new URLSearchParams(window.location.search);
    const auth = params.get("spotify_auth");
    if (auth === "success" || auth === "error") {
      void refreshAuthStatus();
      window.history.replaceState({}, "", "/");
      if (auth === "error") {
        setMessages((m) => [
          ...m,
          {
            role: "assistant",
            content:
              "Spotify login did not complete (state or token exchange failed, or cookies were blocked). " +
              "Try again in **Chrome or Safari**, turn off strict tracking blocking for this site, " +
              "and ensure the API has **SESSION_SECRET** and (on HTTPS) **SESSION_COOKIE_SECURE=1** set. " +
              "If it keeps failing, check Railway logs for `/auth/callback`.",
          },
        ]);
      }
    }
    return () => {
      cancelled = true;
      if (warmPoll.id !== undefined) window.clearInterval(warmPoll.id);
    };
    // Mount-only bootstrap (health + auth); listing refreshAuthStatus would re-run on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!conversationId || !authReady) return;
    const scope = spotifyUser?.id ?? "anon";
    try {
      window.localStorage.setItem(activeConvStorageKey(scope), conversationId);
    } catch {
      /* ignore */
    }
  }, [conversationId, authReady, spotifyUser?.id]);

  useLayoutEffect(() => {
    if (!conversationId || !authReady) return;
    const scope = spotifyUser?.id ?? "anon";
    try {
      window.localStorage.removeItem("spotify-llm-chat-v1");
      const raw = window.localStorage.getItem(chatMessagesStorageKey(scope, conversationId));
      if (!raw) {
        setMessages([{ role: "system", content: DEFAULT_SYSTEM_MESSAGE }]);
        return;
      }
      const parsed = JSON.parse(raw) as ChatMsg[];
      if (!Array.isArray(parsed)) {
        setMessages([{ role: "system", content: DEFAULT_SYSTEM_MESSAGE }]);
        return;
      }
      const cleaned = parsed.filter(
        (m) => m && (m.role === "system" || m.role === "assistant" || m.role === "user") && typeof m.content === "string",
      );
      if (!cleaned.length) {
        setMessages([{ role: "system", content: DEFAULT_SYSTEM_MESSAGE }]);
        return;
      }
      const hasSystem = cleaned.some((m) => m.role === "system");
      setMessages(hasSystem ? cleaned : [{ role: "system", content: DEFAULT_SYSTEM_MESSAGE }, ...cleaned]);
    } catch {
      setMessages([{ role: "system", content: DEFAULT_SYSTEM_MESSAGE }]);
    }
  }, [conversationId, authReady, spotifyUser?.id]);

  useEffect(() => {
    if (!conversationId || !authReady) return;
    const scope = spotifyUser?.id ?? "anon";
    try {
      const slim = messages.map((m) => ({ role: m.role, content: m.content }));
      window.localStorage.setItem(chatMessagesStorageKey(scope, conversationId), JSON.stringify(slim));
    } catch {
      /* ignore */
    }
  }, [messages, conversationId, authReady, spotifyUser?.id]);

  async function connectSpotify() {
    setAuthBusy(true);
    try {
      const res = await fetchWithTimeout(
        agentApiUrl("/auth/spotify"),
        { credentials: "include" },
        T_AUTH_MS,
      );
      const parsed = await readResponseBody(res);
      const data = (parsed.json ? parsed.data : null) as { auth_url?: string; error?: string } | null;
      if (!res.ok) {
        const hint =
          typeof data?.error === "string"
            ? data.error
            : parsed.raw.trim().slice(0, 400) || `HTTP ${res.status}`;
        setMessages((m) => [
          ...m,
          {
            role: "assistant",
            content:
              `Spotify login failed (${res.status}): ${hint}\n\n` +
              `Check API logs, SPOTIFY_CLIENT_ID/SECRET, and that uvicorn is running.`,
          },
        ]);
        return;
      }
      if (data?.auth_url) {
        window.location.href = data.auth_url;
      } else {
        setMessages((m) => [
          ...m,
          {
            role: "assistant",
            content:
              "Could not start Spotify login (no auth_url in response). Check API /auth/spotify and Spotify app settings.",
          },
        ]);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      const isAbort =
        e instanceof DOMException
          ? e.name === "AbortError" || e.name === "TimeoutError"
          : /abort|timeout/i.test(msg);
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content:
            `Spotify login error: ${msg}` +
            (isAbort ? "\n\nThe request timed out or was cancelled — try again in a normal browser window." : ""),
        },
      ]);
    } finally {
      setAuthBusy(false);
    }
  }

  async function saveLlmKeys() {
    if (!spotifyUser || llmKeysBusy) return;
    const o = llmOpenaiInput.trim();
    const a = llmAnthropicInput.trim();
    if (!o && !a) return;
    const body: Record<string, string> = {};
    if (o) body.openai_key = o;
    if (a) body.anthropic_key = a;
    if (o && a) body.provider = llmProviderChoice;
    setLlmKeysBusy(true);
    setLlmKeysInlineError(null);
    try {
      const res = await fetchWithTimeout(
        agentApiUrl("/auth/llm-keys"),
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
        T_AUTH_MS,
      );
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      if (!res.ok) {
        setLlmKeysInlineError(formatByokSaveError(res, data));
        return;
      }
      setLlmOpenaiInput("");
      setLlmAnthropicInput("");
      setLlmKeysInlineError(null);
      if (data && typeof data === "object" && "byok_server_enabled" in data) {
        setLlmKeyStatus(data as LlmKeyStatus);
      } else {
        void refreshAuthStatus();
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setLlmKeysInlineError(msg);
    } finally {
      setLlmKeysBusy(false);
    }
  }

  async function clearLlmKeys() {
    if (!spotifyUser || llmKeysBusy) return;
    setLlmKeysBusy(true);
    setLlmKeysInlineError(null);
    try {
      const res = await fetchWithTimeout(
        agentApiUrl("/auth/llm-keys"),
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ openai_key: "", anthropic_key: "" }),
        },
        T_AUTH_MS,
      );
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | LlmKeyStatus | null;
      if (res.ok && data && typeof data === "object" && "byok_server_enabled" in data) {
        setLlmKeyStatus(data as LlmKeyStatus);
        setLlmKeysInlineError(null);
      } else if (!res.ok) {
        setLlmKeysInlineError(formatByokSaveError(res, data && typeof data === "object" ? data : null));
      } else {
        void refreshAuthStatus();
      }
    } catch (e) {
      setLlmKeysInlineError(e instanceof Error ? e.message : String(e));
    } finally {
      setLlmKeysBusy(false);
    }
  }

  async function logoutSpotify() {
    setAuthBusy(true);
    try {
      await fetchWithTimeout(agentApiUrl("/auth/logout"), { method: "POST", credentials: "include" }, T_AUTH_MS);
      setSpotifyUser(null);
      setLlmKeyStatus(null);
    } catch {
      setSpotifyUser(null);
      setLlmKeyStatus(null);
    } finally {
      setAuthBusy(false);
    }
  }

  async function sendHitlResume(kind: "approve" | "reject") {
    if (!pendingHitl || busy) return;
    const n = hitlActionCount(pendingHitl);
    const decisions = Array.from({ length: n }, () => ({ type: kind }));
    const requestId = crypto.randomUUID();
    setBusy(true);
    setActivityTrace([]);
    try {
      const res = await fetchWithTimeout(
        agentApiUrl("/chat/resume"),
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "x-request-id": requestId },
          credentials: "include",
          body: JSON.stringify({
            from: fromId,
            conversation_id: conversationId,
            spotify_user_id: spotifyUser?.id,
            decisions,
          }),
        },
        T_CHAT_MS,
      );
      const parsed = await readResponseBody(res);
      if (!parsed.json || parsed.data === null || typeof parsed.data !== "object") {
        setMessages((m) => [
          ...m,
          {
            role: "assistant",
            content: `Could not parse resume response (${agentApiLabel()}). request_id=${requestId}`,
          },
        ]);
        return;
      }
      const data = parsed.data as {
        reply?: string;
        error?: boolean;
        tool_trace?: unknown;
        hitl_pending?: boolean;
        hitl?: unknown;
      };
      const reply = typeof data.reply === "string" ? data.reply : "(no reply)";
      setActivityTrace(normalizeToolTrace(data.tool_trace));
      if (data.hitl_pending && data.hitl && typeof data.hitl === "object") {
        setPendingHitl(data.hitl as Record<string, unknown>);
      } else {
        setPendingHitl(null);
      }
      setMessages((m) => [...m, { role: "assistant", content: reply }]);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: `Resume error (${agentApiLabel()}).\n\n${msg}\n\nrequest_id=${requestId}`,
        },
      ]);
    } finally {
      touchSession(conversationId);
      setBusy(false);
      queueMicrotask(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }));
    }
  }

  async function send() {
    const body = input.trim();
    if (!body || busy) return;
    if (pendingHitl) {
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content:
            "There is a **pending Spotify or file action** waiting for approval. Use **Approve** or **Reject** above the input first.",
        },
      ]);
      return;
    }
    const requestId = crypto.randomUUID();
    setInput("");
    setBusy(true);
    setActivityTrace([]);
    setMessages((m) => [...m, { role: "user", content: body }]);
    setChatSessions((prev) => {
      const next = prev.map((s) => {
        if (s.id !== conversationId) return s;
        const t = body.trim();
        const nextTitle =
          s.title === "New chat" || s.title === "Chat"
            ? `${t.slice(0, 44)}${t.length > 44 ? "…" : ""}` || "Chat"
            : s.title;
        return { ...s, title: nextTitle, updatedAt: Date.now() };
      });
      next.sort((a, b) => b.updatedAt - a.updatedAt);
      writeChatSessions(spotifyUser?.id ?? "anon", next);
      return next;
    });

    const failAssistant = (text: string) => {
      setMessages((m) => [...m, { role: "assistant", content: text }]);
    };

    try {
      if (USE_CHAT_STREAM) {
        const ctrl = new AbortController();
        const timer = window.setTimeout(() => {
          try {
            ctrl.abort(new DOMException(`Request timed out after ${T_CHAT_MS}ms`, "TimeoutError"));
          } catch {
            ctrl.abort();
          }
        }, T_CHAT_MS);
        let res: Response;
        try {
          res = await fetch(agentApiUrl("/chat/stream"), {
            method: "POST",
            headers: { "Content-Type": "application/json", "x-request-id": requestId },
            credentials: "include",
            body: JSON.stringify({
              from: fromId,
              body,
              conversation_id: conversationId,
              spotify_user_id: spotifyUser?.id,
            }),
            signal: ctrl.signal,
          });
        } finally {
          window.clearTimeout(timer);
        }
        if (!res.ok || !res.body) {
          const t = await res.text();
          failAssistant(
            `Stream error (${agentApiLabel()}).\nHTTP ${res.status}.\n${t.slice(0, 400)}…\nrequest_id=${requestId}`,
          );
          return;
        }
        let assistantText = "";
        setMessages((m) => [...m, { role: "assistant", content: "" }]);
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        let gotDone = false;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const { events, rest } = parseSseChunks(buf);
          buf = rest;
          for (const { event, data } of events) {
            if (event === "token" && typeof data.text === "string" && data.text) {
              assistantText += data.text;
              setMessages((m) => {
                const copy = [...m];
                const last = copy.length - 1;
                if (copy[last]?.role === "assistant") {
                  copy[last] = { role: "assistant", content: assistantText };
                }
                return copy;
              });
            } else if (event === "tool" && typeof data.name === "string") {
              const toolName = data.name;
              setActivityTrace((prev) => [
                ...prev,
                {
                  kind: "tool_call" as const,
                  name: toolName,
                  args: {},
                  id: `live-${prev.length}-${toolName}`,
                },
              ]);
            } else if (event === "done") {
              gotDone = true;
              const serverRaw = typeof data.reply === "string" ? data.reply : "";
              const serverTrim = serverRaw.trim();
              const localTrim = assistantText.trim();
              // Server can emit "(no reply)" while tokens already arrived (checkpoint lag). Never wipe UI text.
              const reply =
                serverTrim && serverTrim !== "(no reply)"
                  ? serverRaw
                  : localTrim || serverRaw || "(no reply)";
              assistantText = reply;
              setActivityTrace(normalizeToolTrace(data.tool_trace));
              if (data.hitl_pending && data.hitl && typeof data.hitl === "object") {
                setPendingHitl(data.hitl as Record<string, unknown>);
              } else {
                setPendingHitl(null);
              }
              setMessages((m) => {
                const copy = [...m];
                const last = copy.length - 1;
                if (copy[last]?.role === "assistant") {
                  copy[last] = { role: "assistant", content: reply };
                }
                return copy;
              });
            } else if (event === "error") {
              const msg = typeof data.message === "string" ? data.message : "Stream error";
              setMessages((m) => {
                const copy = [...m];
                const last = copy.length - 1;
                if (copy[last]?.role === "assistant") {
                  copy[last] = { role: "assistant", content: `Error: ${msg}\n\nrequest_id=${requestId}` };
                  return copy;
                }
                return [...copy, { role: "assistant", content: `Error: ${msg}\n\nrequest_id=${requestId}` }];
              });
              return;
            }
          }
        }
        if (!gotDone && assistantText) {
          setMessages((m) => {
            const copy = [...m];
            const last = copy.length - 1;
            if (copy[last]?.role === "assistant") {
              copy[last] = { role: "assistant", content: assistantText };
            }
            return copy;
          });
        }
        return;
      }

      const res = await fetchWithTimeout(
        agentApiUrl("/chat"),
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "x-request-id": requestId },
          credentials: "include",
          body: JSON.stringify({
            from: fromId,
            body,
            conversation_id: conversationId,
            spotify_user_id: spotifyUser?.id,
          }),
        },
        T_CHAT_MS,
      );
      const parsed = await readResponseBody(res);
      if (!parsed.json || parsed.data === null || typeof parsed.data !== "object") {
        const snippet = parsed.raw.trim().slice(0, 280);
        failAssistant(
          `Error calling ${agentApiLabel()}.\n\n` +
            `HTTP ${res.status}. Response was not JSON.\n` +
            (snippet ? `${snippet}${parsed.raw.length > 280 ? "…" : ""}` : "(empty body)") +
            `\nrequest_id=${requestId}`,
        );
        return;
      }
      const data = parsed.data as {
        reply?: string;
        error?: boolean;
        tool_trace?: unknown;
        hitl_pending?: boolean;
        hitl?: unknown;
      };
      const reply = typeof data.reply === "string" ? data.reply : "(no reply)";
      setActivityTrace(normalizeToolTrace(data.tool_trace));
      if (data.hitl_pending && data.hitl && typeof data.hitl === "object") {
        setPendingHitl(data.hitl as Record<string, unknown>);
      } else {
        setPendingHitl(null);
      }
      setMessages((m) => [...m, { role: "assistant", content: reply }]);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      const streamHint =
        /failed to fetch/i.test(msg) && USE_CHAT_STREAM
          ? "\n\nStreaming often surfaces this if the API drops the connection mid-response. Try `NEXT_PUBLIC_CHAT_STREAM=0` in `apps/web/.env.local` (JSON `/chat`), or confirm `curl -sS http://127.0.0.1:8013/health` and watch the API log for tracebacks."
          : /failed to fetch/i.test(msg)
            ? "\n\nConfirm the API is up: `curl -sS http://127.0.0.1:8013/health` and that `AGENT_API_URL` in `apps/web/.env.local` matches."
            : "";
      failAssistant(
        `Error calling ${agentApiLabel()}.\n\n${msg}${streamHint}\n\nrequest_id=${requestId}`,
      );
    } finally {
      touchSession(conversationId);
      setBusy(false);
      queueMicrotask(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }));
    }
  }

  const sidebarBtn = {
    padding: "8px 10px",
    borderRadius: 10,
    border: "1px solid rgba(255,255,255,0.14)",
    background: "rgba(255,255,255,0.06)",
    color: "#f3f4f6",
    cursor: "pointer",
    fontSize: 12,
  } as const;

  return (
    <main
      style={{
        height: "100dvh",
        maxHeight: "100dvh",
        overflow: "hidden",
        background: "#0f172a",
        color: "#e5e7eb",
        boxSizing: "border-box",
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: compact ? "column" : "row",
          maxWidth: compact ? "100%" : 1320,
          margin: "0 auto",
          height: "100%",
          minHeight: 0,
          boxSizing: "border-box",
          padding: compact ? "0 10px 10px" : 0,
        }}
      >
        <aside
          style={{
            width: compact ? "100%" : 272,
            maxHeight: compact ? "min(44vh, 380px)" : undefined,
            flexShrink: 0,
            borderRight: compact ? "none" : "1px solid rgba(255,255,255,0.08)",
            borderBottom: compact ? "1px solid rgba(255,255,255,0.08)" : "none",
            display: "flex",
            flexDirection: "column",
            padding: compact ? "12px 0" : "14px 12px",
            gap: 14,
            minHeight: 0,
            overflowY: "auto",
          }}
        >
          <div
            style={{
              fontSize: 20,
              fontWeight: 800,
              letterSpacing: -0.5,
              lineHeight: 1.15,
              background: "linear-gradient(110deg, #34d399 0%, #a78bfa 42%, #22d3ee 88%)",
              WebkitBackgroundClip: "text",
              color: "transparent",
              backgroundClip: "text",
            }}
          >
            {APP_NAME}
          </div>
          {spotifyUser ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <span style={{ color: "#9ca3af", fontSize: 11, lineHeight: 1.35 }}>{spotifyUserLabel}</span>
              <button
                type="button"
                onClick={() => void logoutSpotify()}
                disabled={authBusy}
                style={{ ...sidebarBtn, opacity: authBusy ? 0.6 : 1 }}
              >
                Log out
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => void connectSpotify()}
              disabled={authBusy}
              style={{
                padding: "8px 12px",
                borderRadius: 10,
                border: "1px solid rgba(52,211,153,0.45)",
                background: "linear-gradient(180deg, rgba(52,211,153,0.22) 0%, rgba(29,185,84,0.12) 100%)",
                color: "#f3f4f6",
                cursor: authBusy ? "not-allowed" : "pointer",
                fontSize: 12,
                fontWeight: 600,
              }}
            >
              {authBusy ? "Connecting…" : "Connect Spotify"}
            </button>
          )}

          <div
            style={{
              borderBottom: "1px solid rgba(255,255,255,0.08)",
              paddingBottom: 12,
              marginBottom: 8,
            }}
          >
            <div style={{ fontSize: 11, fontWeight: 700, color: "#e5e7eb", marginBottom: 6 }}>LLM API keys</div>
            {!spotifyUser ? (
              <p style={{ fontSize: 10, color: "#9ca3af", margin: 0, lineHeight: 1.45 }}>
                Connect Spotify to save your OpenAI or Anthropic key (encrypted on the server).
              </p>
            ) : llmKeyStatus == null ? (
              <p style={{ fontSize: 10, color: "#9ca3af", margin: 0 }}>Loading…</p>
            ) : llmKeyStatus && !llmKeyStatus.byok_server_enabled ? (
              <p style={{ fontSize: 10, color: "#fcd34d", margin: 0, lineHeight: 1.45 }}>
                BYOK not enabled on this server (operator sets <code style={{ color: "#fde68a" }}>USER_LLM_KEYS_FERNET_KEY</code>).
              </p>
            ) : llmKeyStatus ? (
              <>
                <p style={{ fontSize: 10, color: "#9ca3af", margin: "0 0 8px", lineHeight: 1.4 }}>
                  OpenAI {llmKeyStatus.has_openai ? "✓" : "—"} · Anthropic {llmKeyStatus.has_anthropic ? "✓" : "—"}
                  {llmKeyStatus.has_openai && llmKeyStatus.has_anthropic && <> · {llmKeyStatus.provider ?? "?"}</>}
                </p>
                <div style={{ display: "flex", alignItems: "stretch", gap: 6, marginBottom: 8 }}>
                  <input
                    type={showLlmOpenai ? "text" : "password"}
                    autoComplete="off"
                    spellCheck={false}
                    value={llmOpenaiInput}
                    onChange={(e) => {
                      setLlmOpenaiInput(e.target.value);
                      setLlmKeysInlineError(null);
                    }}
                    placeholder={llmKeyStatus.has_openai ? "New OpenAI key" : "OpenAI sk-…"}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      padding: "9px 10px",
                      lineHeight: 1.45,
                      minHeight: 40,
                      boxSizing: "border-box",
                      borderRadius: 8,
                      border: "1px solid rgba(255,255,255,0.12)",
                      background: "rgba(0,0,0,0.35)",
                      color: "#f3f4f6",
                      fontSize: 12,
                    }}
                  />
                  <button
                    type="button"
                    aria-label={showLlmOpenai ? "Hide OpenAI key" : "Show OpenAI key"}
                    onClick={() => setShowLlmOpenai((v) => !v)}
                    style={{
                      flexShrink: 0,
                      width: 40,
                      minHeight: 40,
                      padding: 0,
                      borderRadius: 8,
                      border: "1px solid rgba(255,255,255,0.14)",
                      background: "rgba(255,255,255,0.06)",
                      color: "#9ca3af",
                      cursor: "pointer",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    {showLlmOpenai ? (
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                        <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                        <line x1="1" y1="1" x2="23" y2="23" />
                      </svg>
                    ) : (
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                        <circle cx="12" cy="12" r="3" />
                      </svg>
                    )}
                  </button>
                </div>
                <div style={{ display: "flex", alignItems: "stretch", gap: 6, marginBottom: 8 }}>
                  <input
                    type={showLlmAnthropic ? "text" : "password"}
                    autoComplete="off"
                    spellCheck={false}
                    value={llmAnthropicInput}
                    onChange={(e) => {
                      setLlmAnthropicInput(e.target.value);
                      setLlmKeysInlineError(null);
                    }}
                    placeholder={llmKeyStatus.has_anthropic ? "New Anthropic key" : "Anthropic sk-ant-…"}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      padding: "9px 10px",
                      lineHeight: 1.45,
                      minHeight: 40,
                      boxSizing: "border-box",
                      borderRadius: 8,
                      border: "1px solid rgba(255,255,255,0.12)",
                      background: "rgba(0,0,0,0.35)",
                      color: "#f3f4f6",
                      fontSize: 12,
                    }}
                  />
                  <button
                    type="button"
                    aria-label={showLlmAnthropic ? "Hide Anthropic key" : "Show Anthropic key"}
                    onClick={() => setShowLlmAnthropic((v) => !v)}
                    style={{
                      flexShrink: 0,
                      width: 40,
                      minHeight: 40,
                      padding: 0,
                      borderRadius: 8,
                      border: "1px solid rgba(255,255,255,0.14)",
                      background: "rgba(255,255,255,0.06)",
                      color: "#9ca3af",
                      cursor: "pointer",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    {showLlmAnthropic ? (
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                        <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                        <line x1="1" y1="1" x2="23" y2="23" />
                      </svg>
                    ) : (
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                        <circle cx="12" cy="12" r="3" />
                      </svg>
                    )}
                  </button>
                </div>
                {llmOpenaiInput.trim() && llmAnthropicInput.trim() ? (
                  <select
                    value={llmProviderChoice}
                    onChange={(e) => setLlmProviderChoice(e.target.value as "openai" | "anthropic")}
                    style={{
                      width: "100%",
                      marginBottom: 6,
                      padding: "5px 8px",
                      borderRadius: 8,
                      border: "1px solid rgba(255,255,255,0.15)",
                      background: "rgba(0,0,0,0.35)",
                      color: "#f3f4f6",
                      fontSize: 11,
                    }}
                  >
                    <option value="openai">Use OpenAI</option>
                    <option value="anthropic">Use Anthropic</option>
                  </select>
                ) : null}
                {llmKeysInlineError ? (
                  <p style={{ fontSize: 10, color: "#fecaca", margin: "0 0 8px", lineHeight: 1.45 }}>{llmKeysInlineError}</p>
                ) : null}
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  <button
                    type="button"
                    disabled={llmKeysBusy || (!llmOpenaiInput.trim() && !llmAnthropicInput.trim())}
                    onClick={() => void saveLlmKeys()}
                    style={{
                      padding: "5px 8px",
                      borderRadius: 8,
                      border: "1px solid rgba(52,211,153,0.35)",
                      background: "rgba(52,211,153,0.12)",
                      color: "#f3f4f6",
                      fontSize: 10,
                      cursor: llmKeysBusy ? "not-allowed" : "pointer",
                    }}
                  >
                    {llmKeysBusy ? "…" : "Save"}
                  </button>
                  <button
                    type="button"
                    disabled={llmKeysBusy || (!llmKeyStatus.has_openai && !llmKeyStatus.has_anthropic)}
                    onClick={() => void clearLlmKeys()}
                    style={{
                      padding: "5px 8px",
                      borderRadius: 8,
                      border: "1px solid rgba(248,113,113,0.35)",
                      background: "rgba(248,113,113,0.08)",
                      color: "#fecaca",
                      fontSize: 10,
                      cursor: llmKeysBusy ? "not-allowed" : "pointer",
                    }}
                  >
                    Clear
                  </button>
                </div>
              </>
            ) : null}
          </div>

          <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
            <div
              style={{
                fontSize: 10,
                fontWeight: 700,
                color: "#9ca3af",
                marginBottom: 8,
                letterSpacing: 0.6,
                textTransform: "uppercase",
              }}
            >
              Chats
            </div>
            <button
              type="button"
              onClick={() => newChat()}
              disabled={busy}
              style={{
                ...sidebarBtn,
                width: "100%",
                borderStyle: "dashed",
                marginBottom: 8,
                cursor: busy ? "not-allowed" : "pointer",
              }}
            >
              + New chat
            </button>
            <div
              style={{
                flex: 1,
                overflowY: "auto",
                display: "flex",
                flexDirection: "column",
                gap: 4,
                paddingRight: 2,
              }}
            >
              {sortedSessions.map((s) => {
                const active = s.id === conversationId;
                return (
                  <div
                    key={s.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => selectChat(s.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        selectChat(s.id);
                      }
                    }}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      padding: "8px 8px",
                      borderRadius: 10,
                      border: active ? "1px solid rgba(52,211,153,0.35)" : "1px solid rgba(255,255,255,0.08)",
                      background: active ? "rgba(52,211,153,0.12)" : "rgba(255,255,255,0.04)",
                      cursor: busy ? "not-allowed" : "pointer",
                      fontSize: 12,
                      color: "#e5e7eb",
                    }}
                  >
                    <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {s.title}
                    </span>
                    <button
                      type="button"
                      title="Delete chat"
                      onClick={(e) => deleteChat(s.id, e)}
                      disabled={busy}
                      style={{
                        flexShrink: 0,
                        width: 22,
                        height: 22,
                        lineHeight: "20px",
                        padding: 0,
                        borderRadius: 6,
                        border: "none",
                        background: "rgba(0,0,0,0.25)",
                        color: "#9ca3af",
                        cursor: busy ? "not-allowed" : "pointer",
                        fontSize: 14,
                      }}
                    >
                      ×
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
        </aside>

        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            minWidth: 0,
            minHeight: 0,
            overflow: "hidden",
          }}
        >
          {apiReachable === false && (
            <div
              style={{
                padding: "10px 14px",
                borderBottom: "1px solid rgba(248,113,113,0.25)",
                background: "rgba(248,113,113,0.1)",
                color: "#fecaca",
                fontSize: 12,
              }}
            >
              Can’t reach the API. Start the backend or fix <code style={{ color: "#fde68a" }}>AGENT_API_URL</code>.
            </div>
          )}
          {apiReachable !== false && agentWarmup?.prewarm && agentWarmup.ready === false && (
            <div
              style={{
                padding: "10px 14px",
                borderBottom: "1px solid rgba(96,165,250,0.2)",
                background: "rgba(96,165,250,0.08)",
                color: "#bfdbfe",
                fontSize: 12,
              }}
            >
              Warming up the assistant — first reply may take a moment.
            </div>
          )}

          <div
            style={{
              flex: 1,
              display: "flex",
              flexDirection: compact ? "column" : "row",
              minHeight: 0,
              overflow: "hidden",
            }}
          >
            <section
              style={{
                flex: 1,
                minWidth: 0,
                minHeight: 0,
                display: "flex",
                flexDirection: "column",
                border: "1px solid rgba(167,139,250,0.1)",
                borderRadius: 0,
                background: "linear-gradient(165deg, rgba(255,255,255,0.04) 0%, rgba(15,23,42,0.5) 100%)",
                margin: compact ? "8px 0 0" : 12,
                marginRight: compact ? 0 : 0,
                borderTopLeftRadius: compact ? 12 : 16,
                borderBottomLeftRadius: compact ? 12 : 16,
                borderTopRightRadius: compact ? 12 : !compact && toolsOpen ? 0 : 16,
                borderBottomRightRadius: compact ? 12 : !compact && toolsOpen ? 0 : 16,
                overflow: "hidden",
              }}
            >
            <div style={{ padding: "12px 16px", borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
              <div style={{ color: "#c7cad1", fontSize: 12 }}>{messages[0]?.content}</div>
            </div>

            <div style={{ padding: 16, flex: 1, overflow: "auto", minHeight: 0 }}>
              {list.map((m, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    justifyContent: m.role === "user" ? "flex-end" : "flex-start",
                    marginBottom: 12,
                  }}
                >
                  <div
                    style={{
                      maxWidth: "90%",
                      padding: "10px 12px",
                      borderRadius: 14,
                      whiteSpace: m.role === "assistant" ? "normal" : "pre-wrap",
                      lineHeight: m.role === "assistant" ? 1.28 : 1.35,
                      background:
                        m.role === "user"
                          ? "rgba(29,185,84,0.20)"
                          : "rgba(255,255,255,0.06)",
                      border:
                        m.role === "user"
                          ? "1px solid rgba(29,185,84,0.35)"
                          : "1px solid rgba(255,255,255,0.10)",
                    }}
                  >
                    {m.role === "assistant" ? <ChatMarkdown text={m.content} /> : m.content}
                  </div>
                </div>
              ))}
              <div ref={bottomRef} />
            </div>

            <div
              style={{
                padding: 16,
                borderTop: "1px solid rgba(255,255,255,0.08)",
                display: "flex",
                flexDirection: "column",
                gap: 10,
              }}
            >
              {pendingHitl && (
                <div
                  style={{
                    padding: "12px 14px",
                    borderRadius: 12,
                    border: "1px solid rgba(251,191,36,0.45)",
                    background: "rgba(251,191,36,0.10)",
                    color: "#fde68a",
                    fontSize: 13,
                    lineHeight: 1.45,
                  }}
                >
                  <strong>Approval required</strong> — {hitlSummary(pendingHitl)}. Approve or reject before Spotify or file
                  actions run.
                  <div style={{ marginTop: 10, display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <button
                      type="button"
                      onClick={() => void sendHitlResume("approve")}
                      disabled={busy}
                      style={{
                        padding: "8px 14px",
                        borderRadius: 10,
                        border: "1px solid rgba(34,197,94,0.45)",
                        background: "rgba(34,197,94,0.2)",
                        color: "#f3f4f6",
                        cursor: busy ? "not-allowed" : "pointer",
                        fontWeight: 600,
                        fontSize: 12,
                      }}
                    >
                      Approve
                    </button>
                    <button
                      type="button"
                      onClick={() => void sendHitlResume("reject")}
                      disabled={busy}
                      style={{
                        padding: "8px 14px",
                        borderRadius: 10,
                        border: "1px solid rgba(248,113,113,0.45)",
                        background: "rgba(248,113,113,0.15)",
                        color: "#f3f4f6",
                        cursor: busy ? "not-allowed" : "pointer",
                        fontWeight: 600,
                        fontSize: 12,
                      }}
                    >
                      Reject
                    </button>
                  </div>
                </div>
              )}
              <div style={{ display: "flex", gap: 10, minWidth: 0, width: "100%", alignItems: "stretch" }}>
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send();
                  }
                }}
                placeholder='e.g. "up and coming indie 2026"'
                style={{
                  flex: 1,
                  minWidth: 0,
                  padding: "12px 12px",
                  borderRadius: 12,
                  border: "1px solid rgba(255,255,255,0.12)",
                  background: "rgba(0,0,0,0.25)",
                  color: "#f3f4f6",
                  outline: "none",
                  fontSize: 16,
                }}
                disabled={busy || Boolean(pendingHitl)}
              />
              <button
                type="button"
                onClick={() => void send()}
                disabled={busy || !input.trim() || Boolean(pendingHitl)}
                style={{
                  flexShrink: 0,
                  padding: "12px 16px",
                  minHeight: 44,
                  borderRadius: 12,
                  border: "1px solid rgba(29,185,84,0.35)",
                  background: busy ? "rgba(29,185,84,0.12)" : "rgba(29,185,84,0.18)",
                  color: "#f3f4f6",
                  fontWeight: 700,
                  fontSize: 16,
                  cursor: busy || !input.trim() ? "not-allowed" : "pointer",
                }}
              >
                {busy ? "Sending…" : "Send"}
              </button>
              </div>
            </div>
          </section>

            {!compact && !toolsOpen ? (
              <button
                type="button"
                aria-expanded={false}
                aria-controls="tools-panel"
                aria-label="Show tools panel"
                onClick={() => setToolsOpen(true)}
                style={{
                  width: 44,
                  flexShrink: 0,
                  alignSelf: "stretch",
                  margin: 12,
                  marginLeft: 0,
                  padding: "10px 0",
                  boxSizing: "border-box",
                  border: "1px solid rgba(255,255,255,0.08)",
                  borderRadius: 0,
                  borderTopRightRadius: 16,
                  borderBottomRightRadius: 16,
                  background: "rgba(255,255,255,0.03)",
                  color: "#e5e7eb",
                  cursor: "pointer",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 6,
                  fontSize: 11,
                  fontWeight: 700,
                  lineHeight: 1.2,
                }}
              >
                <span
                  aria-hidden
                  style={{
                    writingMode: "vertical-rl",
                    transform: "rotate(180deg)",
                    letterSpacing: "0.04em",
                    textTransform: "uppercase",
                    color: "#9ca3af",
                  }}
                >
                  Tools
                </span>
                <span aria-hidden style={{ color: "#34d399", fontSize: 16, fontWeight: 800 }}>
                  ‹
                </span>
              </button>
            ) : null}

            {compact && !toolsOpen ? (
              <button
                type="button"
                aria-expanded={false}
                aria-controls="tools-panel"
                aria-label="Show tools panel"
                onClick={() => setToolsOpen(true)}
                style={{
                  flexShrink: 0,
                  width: "100%",
                  margin: "8px 0 0",
                  padding: "10px 12px",
                  borderRadius: 12,
                  border: "1px dashed rgba(255,255,255,0.16)",
                  background: "rgba(255,255,255,0.04)",
                  color: "#c7cad1",
                  cursor: "pointer",
                  fontSize: 12,
                  fontWeight: 600,
                  textAlign: "center",
                }}
              >
                Tools
                {activityTrace.length > 0 ? ` · ${activityTrace.length} step${activityTrace.length === 1 ? "" : "s"}` : ""}{" "}
                · Show
              </button>
            ) : null}

            {toolsOpen ? (
              <aside
                id="tools-panel"
                aria-label="Tool trace"
                style={{
                  width: compact ? "100%" : 252,
                  maxHeight: compact ? "min(26vh, 200px)" : undefined,
                  flexShrink: 0,
                  margin: compact ? "8px 0 0" : 12,
                  marginLeft: compact ? 0 : 0,
                  padding: compact ? 10 : 12,
                  border: "1px solid rgba(255,255,255,0.08)",
                  borderRadius: compact ? 12 : 0,
                  borderTopRightRadius: compact ? 12 : 16,
                  borderBottomRightRadius: compact ? 12 : 16,
                  borderTopLeftRadius: compact ? 12 : 0,
                  borderBottomLeftRadius: compact ? 12 : 0,
                  background: "rgba(255,255,255,0.03)",
                  minHeight: 0,
                  display: "flex",
                  flexDirection: "column",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    flexShrink: 0,
                    position: "sticky",
                    top: 0,
                    zIndex: 2,
                    alignSelf: "stretch",
                    marginLeft: compact ? -10 : -12,
                    marginRight: compact ? -10 : -12,
                    marginTop: compact ? -10 : -12,
                    paddingLeft: compact ? 10 : 12,
                    paddingRight: compact ? 10 : 12,
                    paddingTop: compact ? 10 : 12,
                    paddingBottom: compact ? 8 : 10,
                    background: "#0f172a",
                    borderBottom: "1px solid rgba(255,255,255,0.08)",
                  }}
                >
                  <div style={{ fontSize: 13, fontWeight: 700, color: "#e5e7eb" }}>Tools</div>
                  <p style={{ fontSize: 10, color: "#9ca3af", lineHeight: 1.45, margin: "8px 0 0" }}>
                    Steps from this turn. Some Spotify actions need your approval first.
                  </p>
                </div>
                <div
                  style={{
                    flex: 1,
                    /* Floor height: flex:1 with minHeight:0 alone can shrink below one line and clip "No tools yet." */
                    minHeight: compact ? 40 : 52,
                    overflowY: "auto",
                    overflowX: "hidden",
                    display: "flex",
                    flexDirection: "column",
                  }}
                >
                  {activityTrace.length > 0 ? (
                    <ToolTrace entries={activityTrace} variant="panel" />
                  ) : (
                    <div
                      style={{
                        fontSize: 11,
                        color: "#6b7280",
                        lineHeight: 1.5,
                        padding: compact ? "8px 0 6px" : "10px 0 8px",
                        flexShrink: 0,
                      }}
                    >
                      {busy ? "Waiting…" : "No tools yet."}
                    </div>
                  )}
                </div>
                <div
                  style={{
                    flexShrink: 0,
                    paddingTop: 10,
                    borderTop: "1px solid rgba(255,255,255,0.08)",
                  }}
                >
                  <button
                    type="button"
                    aria-controls="tools-panel"
                    title="Collapse tools panel"
                    aria-label="Collapse tools panel"
                    onClick={() => setToolsOpen(false)}
                    style={{
                      width: "100%",
                      padding: "8px 10px",
                      borderRadius: 8,
                      border: "1px solid rgba(255,255,255,0.12)",
                      background: "rgba(0,0,0,0.22)",
                      color: "#9ca3af",
                      cursor: "pointer",
                      fontSize: 11,
                      fontWeight: 600,
                    }}
                  >
                    Collapse tools panel
                  </button>
                </div>
              </aside>
            ) : null}
          </div>
        </div>
      </div>
    </main>
  );
}
