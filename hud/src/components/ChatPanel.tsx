import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import { Highlight, themes } from "prism-react-renderer";
import type { ChatMessage } from "../types";

interface ChatPanelProps {
  messages: ChatMessage[];
  currentStreamText: string;
  listening: boolean;
  onSend: (text: string) => Promise<void>;
}

function CodeBlock(props: { code: string; language: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(props.code);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div className="code-wrap">
      <button className="copy-btn" onClick={() => void copy()}>
        {copied ? "Copied" : "Copy"}
      </button>
      <Highlight code={props.code} language={(props.language || "text") as never} theme={themes.nightOwl}>
        {({ className, style, tokens, getLineProps, getTokenProps }) => (
          <pre className={className} style={style}>
            {tokens.map((line, i) => (
              <div key={i} {...getLineProps({ line })}>
                {line.map((token, key) => (
                  <span key={key} {...getTokenProps({ token })} />
                ))}
              </div>
            ))}
          </pre>
        )}
      </Highlight>
    </div>
  );
}

export function ChatPanel({ messages, currentStreamText, listening, onSend }: ChatPanelProps) {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const displayed = useMemo(() => {
    if (!currentStreamText) {
      return messages;
    }
    return [
      ...messages,
      {
        id: "streaming",
        role: "assistant" as const,
        text: currentStreamText,
        timestamp: Date.now(),
      },
    ];
  }, [messages, currentStreamText]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [displayed]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = input.trim();
    if (!text) {
      return;
    }
    setInput("");
    await onSend(text);
  };

  return (
    <section className="panel chat-panel">
      <div className="messages">
        {displayed.map((message) => (
          <article key={message.id} className={`message ${message.role}`}>
            {message.role === "assistant" ? (
              <Markdown
                components={{
                  code(props) {
                    const className = String(props.className || "");
                    const language = className.replace("language-", "");
                    const text = String(props.children || "").replace(/\n$/, "");
                    if (className.startsWith("language-")) {
                      return <CodeBlock code={text} language={language || "text"} />;
                    }
                    return <code className="inline-code">{props.children}</code>;
                  },
                }}
              >
                {message.text}
              </Markdown>
            ) : (
              <p>{message.text}</p>
            )}
          </article>
        ))}
        <div ref={bottomRef} />
      </div>

      <form className="chat-input-row" onSubmit={(event) => void submit(event)}>
        <input
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder={listening ? "Listening..." : "Type a command..."}
        />
      </form>
    </section>
  );
}
