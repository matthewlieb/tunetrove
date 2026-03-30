"use client";

import ReactMarkdown from "react-markdown";

type Props = { text: string };

export function ChatMarkdown({ text }: Props) {
  return (
    <div className="chat-md-root" style={{ fontSize: "inherit", lineHeight: "inherit" }}>
      <ReactMarkdown
        components={{
          a: ({ href, children }) => {
            if (!href) return <span>{children}</span>;
            return (
              <a className="chat-md-link" href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
