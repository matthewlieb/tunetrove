const PROXY_PREFIX = "/api/agent";

function normalizePath(path: string): string {
  return path.startsWith("/") ? path : `/${path}`;
}

/**
 * Base URL for FastAPI routes (`/chat`, `/auth/status`, …).
 * With default proxy: same-origin `/api/agent/...` (cookies + no CORS).
 */
export function agentApiUrl(path: string): string {
  const p = normalizePath(path);
  const useProxy = process.env.NEXT_PUBLIC_USE_AGENT_PROXY !== "0";
  if (useProxy) {
    if (typeof window !== "undefined") {
      return `${PROXY_PREFIX}${p}`;
    }
    const origin = process.env.NEXT_PUBLIC_APP_ORIGIN?.replace(/\/$/, "") ?? "";
    return origin ? `${origin}${PROXY_PREFIX}${p}` : `${PROXY_PREFIX}${p}`;
  }
  const base = (process.env.NEXT_PUBLIC_AGENT_API_BASE_URL ?? "").replace(/\/$/, "");
  return `${base}${p}`;
}

/** Short label for user-facing error lines. */
export function agentApiLabel(): string {
  if (process.env.NEXT_PUBLIC_USE_AGENT_PROXY === "0") {
    const b = process.env.NEXT_PUBLIC_AGENT_API_BASE_URL?.replace(/\/$/, "") ?? "";
    try {
      return b ? new URL(b).host : "API";
    } catch {
      return "API";
    }
  }
  return "API";
}

export async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  timeoutMs: number,
): Promise<Response> {
  const ctrl = new AbortController();
  const id = globalThis.setTimeout(() => {
    try {
      ctrl.abort(new DOMException(`timeout after ${timeoutMs}ms`, "TimeoutError"));
    } catch {
      ctrl.abort();
    }
  }, timeoutMs);
  try {
    return await fetch(input, { ...init, signal: ctrl.signal });
  } finally {
    globalThis.clearTimeout(id);
  }
}
