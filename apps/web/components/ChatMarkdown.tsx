"use client";

import type { CSSProperties } from "react";
import ReactMarkdown from "react-markdown";

type Props = { text: string };

const linkStyle: CSSProperties = {
  color: "#6ee7b7",
  textDecoration: "underline",
  textDecorationColor: "rgba(110,231,183,0.35)",
  textUnderlineOffset: "3px",
  fontWeight: 500,
};

export function ChatMarkdown({ text }: Props) {
  return (
    <div className="chat-md-root" style={{ fontSize: "inherit", lineHeight: "inherit" }}>
      <ReactMarkdown
        components={{
          a: ({ href, children, ...rest }) => (
            <a {...rest} href={href} target="_blank" rel="noopener noreferrer" style={linkStyle}>
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
