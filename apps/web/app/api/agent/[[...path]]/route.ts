import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

/**
 * Explicit server proxy to FastAPI (replaces next.config rewrites).
 * - Forwards cookies/headers so Spotify session on 127.0.0.1 works with the UI on :3003.
 * - On connection failures, returns JSON so the chat UI never sees a bare HTML 502.
 */
const UPSTREAM = (process.env.AGENT_API_URL || "http://127.0.0.1:8013").replace(/\/$/, "");

/** Forward all Set-Cookie headers; a single `headers.set` drops extras and breaks OAuth session. */
function appendSetCookies(from: Headers, to: Headers): void {
  const ext = from as Headers & { getSetCookie?: () => string[] };
  if (typeof ext.getSetCookie === "function") {
    for (const c of ext.getSetCookie()) {
      to.append("Set-Cookie", c);
    }
    return;
  }
  const single = from.get("set-cookie");
  if (single) to.append("Set-Cookie", single);
}

/**
 * Upstream fetch budget. POST /chat and POST /chat/stream can run for many minutes (LLM + tools);
 * a single global 170s cap caused SSE to die mid-turn → browser "Failed to fetch".
 * Override with AGENT_API_FETCH_TIMEOUT_MS (ms). Vercel/serverless may still impose a lower max.
 */
function upstreamTimeoutMs(method: string, segments: string[]): number {
  const raw = process.env.AGENT_API_FETCH_TIMEOUT_MS;
  if (raw && /^\d+$/.test(raw.trim())) return Math.max(5_000, parseInt(raw.trim(), 10));
  const subpath = segments.join("/");
  if (method === "POST" && (subpath === "chat/stream" || subpath === "chat" || subpath === "chat/resume")) {
    return 600_000;
  }
  if (method === "POST" || method === "PUT" || method === "PATCH") {
    return 170_000;
  }
  return 90_000;
}

function isAbortOrTimeout(err: unknown): boolean {
  if (err instanceof DOMException) {
    return err.name === "AbortError" || err.name === "TimeoutError";
  }
  if (err instanceof Error) {
    return err.name === "TimeoutError" || /aborted|timeout/i.test(err.message);
  }
  return false;
}

function targetUrl(req: NextRequest, segments: string[]): string {
  const path = segments.length ? `/${segments.join("/")}` : "";
  return `${UPSTREAM}${path}${req.nextUrl.search}`;
}

async function forward(req: NextRequest, segments: string[]): Promise<NextResponse> {
  const url = targetUrl(req, segments);
  const headers = new Headers();
  req.headers.forEach((value, key) => {
    const k = key.toLowerCase();
    if (k === "host" || k === "connection" || k === "content-length") return;
    headers.set(key, value);
  });
  // Help the API infer original scheme/host when behind Vercel HTTPS (logging, future scheme checks).
  const proto = req.nextUrl.protocol.replace(":", "");
  if (proto) {
    headers.set("x-forwarded-proto", proto);
  }
  const xfHost = req.headers.get("x-forwarded-host") || req.headers.get("host");
  if (xfHost) {
    headers.set("x-forwarded-host", xfHost);
  }

  let body: BodyInit | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.arrayBuffer();
  }

  const ms = upstreamTimeoutMs(req.method, segments);
  const signal = AbortSignal.timeout(ms);

  try {
    const res = await fetch(url, {
      method: req.method,
      headers,
      body,
      redirect: "manual",
      cache: "no-store",
      signal,
    });

    const out = new NextResponse(res.body, {
      status: res.status,
      statusText: res.statusText,
    });
    res.headers.forEach((value, key) => {
      const k = key.toLowerCase();
      if (k === "content-encoding" || k === "transfer-encoding") return;
      if (k === "set-cookie") return;
      out.headers.set(key, value);
    });
    appendSetCookies(res.headers, out.headers);
    return out;
  } catch (err) {
    if (isAbortOrTimeout(err)) {
      return NextResponse.json(
        {
          reply: `Agent API at ${UPSTREAM} did not respond within ${ms}ms (wedged import, overload, or network). Restart uvicorn, free port 8013, or open the app in Chrome/Safari instead of an embedded IDE browser.`,
          error: true,
          tool_trace: [],
        },
        { status: 504 },
      );
    }
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      {
        reply: `Cannot reach the agent API at ${UPSTREAM}. Start uvicorn (port 8013) or fix AGENT_API_URL in apps/web/.env.local. (${msg})`,
        error: true,
        tool_trace: [],
      },
      { status: 502 },
    );
  }
}

type RouteCtx = { params: Promise<{ path?: string[] }> };

export async function GET(req: NextRequest, ctx: RouteCtx) {
  const { path } = await ctx.params;
  return forward(req, path ?? []);
}

export async function HEAD(req: NextRequest, ctx: RouteCtx) {
  const { path } = await ctx.params;
  return forward(req, path ?? []);
}

export async function POST(req: NextRequest, ctx: RouteCtx) {
  const { path } = await ctx.params;
  return forward(req, path ?? []);
}

export async function PUT(req: NextRequest, ctx: RouteCtx) {
  const { path } = await ctx.params;
  return forward(req, path ?? []);
}

export async function PATCH(req: NextRequest, ctx: RouteCtx) {
  const { path } = await ctx.params;
  return forward(req, path ?? []);
}

export async function DELETE(req: NextRequest, ctx: RouteCtx) {
  const { path } = await ctx.params;
  return forward(req, path ?? []);
}
