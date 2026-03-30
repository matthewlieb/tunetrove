"use client";

import type { CSSProperties } from "react";

export type ToolTraceEntry =
  | {
      kind: "tool_call";
      name: string;
      args: Record<string, unknown>;
      id: string;
    }
  | {
      kind: "tool_result";
      name: string;
      tool_call_id: string;
      content: string;
    };

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function normalizeToolTrace(raw: unknown): ToolTraceEntry[] {
  if (!Array.isArray(raw)) return [];
  const out: ToolTraceEntry[] = [];
  for (const item of raw) {
    if (!isRecord(item)) continue;
    const kind = item.kind;
    if (kind === "tool_call" && typeof item.name === "string") {
      const args = isRecord(item.args) ? item.args : {};
      out.push({
        kind: "tool_call",
        name: item.name,
        args,
        id: typeof item.id === "string" ? item.id : "",
      });
    } else if (kind === "tool_result" && typeof item.content === "string") {
      out.push({
        kind: "tool_result",
        name: typeof item.name === "string" ? item.name : "",
        tool_call_id: typeof item.tool_call_id === "string" ? item.tool_call_id : "",
        content: item.content,
      });
    }
  }
  return out;
}

const cardBase: CSSProperties = {
  marginTop: 8,
  padding: "10px 12px",
  borderRadius: 10,
  fontSize: 12,
  lineHeight: 1.4,
  border: "1px solid rgba(127, 200, 255, 0.35)",
  background: "rgba(127, 200, 255, 0.08)",
  color: "#e5e7eb",
  textAlign: "left" as const,
};

export function ToolTrace({
  entries,
  variant = "inline",
}: {
  entries: ToolTraceEntry[];
  variant?: "inline" | "panel";
}) {
  if (!entries.length) return null;
  const blockMaxH = variant === "panel" ? 200 : 140;
  return (
    <div
      style={{
        marginTop: variant === "panel" ? 0 : 8,
        width: "100%",
        ...(variant === "panel"
          ? {
              maxHeight: "min(58vh, 520px)",
              overflowY: "auto" as const,
              paddingRight: 4,
            }
          : {}),
      }}
    >
      {variant === "inline" ? (
        <div style={{ fontSize: 11, color: "#9ca3af", marginBottom: 4 }}>Tool trace</div>
      ) : null}
      {entries.map((e, i) =>
        e.kind === "tool_call" ? (
          <div key={`c-${e.id || i}`} style={{ ...cardBase, minWidth: 0 }}>
            <div style={{ fontWeight: 700, color: "#7fc8ff" }}>Calling {e.name}</div>
            <pre
              style={{
                margin: "8px 0 0",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                fontSize: 11,
                color: "#d1d5db",
                maxHeight: blockMaxH,
                overflow: "auto",
              }}
            >
              {JSON.stringify(e.args, null, 2)}
            </pre>
          </div>
        ) : (
          <div
            key={`r-${e.tool_call_id || i}`}
            style={{
              ...cardBase,
              borderColor: "rgba(167, 243, 208, 0.35)",
              background: "rgba(167, 243, 208, 0.08)",
              minWidth: 0,
            }}
          >
            <div style={{ fontWeight: 700, color: "#6ee7b7" }}>
              Result{e.name ? ` · ${e.name}` : ""}
            </div>
            <pre
              style={{
                margin: "8px 0 0",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                fontSize: 11,
                color: "#d1d5db",
                maxHeight: blockMaxH,
                overflow: "auto",
              }}
            >
              {e.content}
            </pre>
          </div>
        ),
      )}
    </div>
  );
}
