"use client";

import ReactMarkdown from "react-markdown";

type Props = { text: string };

export function ChatMarkdown({ text }: Props) {
  return (
    <ReactMarkdown
      components={{
        a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" />,
      }}
    >
      {text}
    </ReactMarkdown>
  );
}
