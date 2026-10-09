"use client";

import { useEffect, useRef } from "react";

/** The "Ask" composer: sends a question to the selected paired peer. */
export function AskPanel({
  peers,
  question,
  setQuestion,
  peerId,
  sendAsk,
  askBusy,
  streaming,
}: {
  peers: { agent_id: string }[];
  question: string;
  setQuestion: (v: string) => void;
  peerId: string;
  sendAsk: () => void;
  askBusy: boolean;
  streaming: boolean;
}) {
  const boxRef = useRef<HTMLTextAreaElement>(null);
  const disabled = askBusy || streaming;

  // Grow with the text, up to a cap.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [question]);

  const canSend =
    !askBusy && !streaming && !!peerId && question.trim().length > 0;

  return (
    <section
      aria-labelledby="ask-heading"
      className="rounded-2xl border border-line bg-bg-raise p-2 pl-4 shadow-card transition-colors focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/20"
    >
      <h2 id="ask-heading" className="sr-only">
        Ask
      </h2>
      {peers.length === 0 ? (
        <p className="px-2 py-2 text-sm text-ink-3">
          No paired peers yet. Pair one first — the agent still answers your
          live questions above.
        </p>
      ) : (
        <div>
          <div className="flex items-end gap-2">
            <div className="min-w-0 flex-1">
              <label htmlFor="question-box" className="sr-only">
                Message a paired peer
              </label>
              <textarea
                ref={boxRef}
                id="question-box"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    sendAsk();
                  }
                }}
                placeholder="Ask a paired peer… (Enter to send, Shift+Enter for a new line)"
                rows={2}
                disabled={disabled}
                aria-describedby="ask-hint"
                className="max-h-40 w-full resize-none overflow-y-auto bg-transparent py-2 text-sm text-ink placeholder:text-ink-3 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
              />
            </div>
            <button
              type="button"
              onClick={sendAsk}
              disabled={!canSend}
              aria-label="Send message"
              className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent-d text-white shadow-glow transition-all hover:brightness-110 active:scale-95 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none"
            >
              {askBusy ? (
                <span aria-hidden="true" className="text-sm leading-none">
                  …
                </span>
              ) : (
                <svg
                  aria-hidden="true"
                  width="16"
                  height="16"
                  viewBox="0 0 16 16"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <line x1="8" y1="14" x2="8" y2="2" />
                  <polyline points="3 7 8 2 13 7" />
                </svg>
              )}
              <span className="sr-only">
                {askBusy ? "Sending…" : "Send"}
              </span>
            </button>
          </div>
          <div className="flex items-center justify-between px-2 pb-0.5 pt-1">
            <p id="ask-hint" className="text-[11px] text-ink-3">
              Enter or ⌘/Ctrl+Enter to send · the peer approves before
              answering
            </p>
            <p
              aria-live="polite"
              className="font-mono text-[11px] tabular-nums text-ink-3"
            >
              {question.length}
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
