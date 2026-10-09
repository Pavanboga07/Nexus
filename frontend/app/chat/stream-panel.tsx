"use client";

import { useEffect, useRef } from "react";

/** The "Live search" composer: sends a prompt through the SSE stream. */
export function StreamPanel({
  streamInput,
  setStreamInput,
  streaming,
  askBusy,
  runStream,
  stopStream,
}: {
  streamInput: string;
  setStreamInput: (v: string) => void;
  streaming: boolean;
  askBusy: boolean;
  runStream: () => void;
  stopStream: () => void;
}) {
  const boxRef = useRef<HTMLTextAreaElement>(null);
  const disabled = streaming || askBusy;

  // Grow with the text, up to a cap.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [streamInput]);

  const canSend = !streaming && !askBusy && streamInput.trim().length > 0;

  return (
    <section aria-labelledby="stream-heading">
      <h2 id="stream-heading" className="sr-only">
        Live search
      </h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (streaming) stopStream();
          else void runStream();
        }}
        className="rounded-2xl border border-line bg-bg-raise p-2 pl-4 shadow-card transition-colors focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/20"
      >
        <div className="flex items-end gap-2">
          <div className="min-w-0 flex-1">
            <label htmlFor="stream-box" className="sr-only">
              Ask live
            </label>
            <textarea
              ref={boxRef}
              id="stream-box"
              value={streamInput}
              onChange={(e) => setStreamInput(e.target.value)}
              onKeyDown={(e) => {
                if (
                  (e.metaKey || e.ctrlKey) &&
                  e.key === "Enter" &&
                  !streaming
                ) {
                  e.preventDefault();
                  void runStream();
                }
              }}
              placeholder="Ask live… (web answer with sources)"
              rows={1}
              disabled={disabled}
              aria-describedby="stream-hint"
              className="max-h-40 w-full resize-none overflow-y-auto bg-transparent py-2 text-sm text-ink placeholder:text-ink-3 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <button
            type="submit"
            disabled={!streaming && !canSend}
            aria-label={streaming ? "Stop generation" : "Ask live"}
            className={`inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-white transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-40 ${
              streaming
                ? "bg-danger shadow-md hover:brightness-110 active:scale-95"
                : "bg-gradient-to-br from-accent to-accent-d shadow-glow hover:brightness-110 active:scale-95"
            }`}
          >
            {streaming ? (
              <span
                aria-hidden="true"
                className="block h-3 w-3 rounded-[2px] bg-white"
              />
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
              {streaming ? "Stop generation" : "Ask live"}
            </span>
          </button>
        </div>
        <div className="flex items-center justify-between px-2 pb-0.5 pt-1">
          <p id="stream-hint" className="text-[11px] text-ink-3">
            ⌘/Ctrl+Enter to send · streams with citations
          </p>
          <p
            aria-live="polite"
            className={`font-mono text-[11px] tabular-nums ${
              streamInput.length > 4000 ? "text-warning-text" : "text-ink-3"
            }`}
          >
            {streamInput.length}
          </p>
        </div>
      </form>
    </section>
  );
}
