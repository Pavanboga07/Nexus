"use client";

import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";

import { closeUnclosedFences, splitSegments } from "./markdown";

const API_BASE =
  process.env.NEXT_PUBLIC_NEXUS_API ?? "http://127.0.0.1:8001";

type Peer = {
  agent_id: string;
  display_name: string;
  fingerprint: string;
};

type ApprovalCard = {
  approval_id: string;
  correlation_id: string;
  requester: string;
  action: string;
  data_category: string;
  purpose: string;
  question: string;
  expires_at: string;
  status: string;
};

type ChatMessage = {
  message_id: string;
  correlation_id: string;
  sender: string;
  recipient: string;
  message_type: string;
  status: string;
  envelope: Record<string, unknown>;
};

function ErrorState({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="rounded-lg border border-red-900 bg-red-950 px-3 py-2 text-sm text-red-200"
    >
      {message}
    </p>
  );
}

type StreamToolCard = {
  tool: string;
  status: "pending" | "completed";
  result?: string;
};

/** Inline markdown for streamed text. Never uses dangerouslySetInnerHTML:
 * React escapes everything; links are restricted to http(s)/mailto. */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  // `code`, **bold**, [label](url) — leftovers render as plain text.
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let n = 0;
  const pushText = (chunk: string) => {
    if (chunk) nodes.push(<span key={`${keyPrefix}-t${n++}`}>{chunk}</span>);
  };
  while ((match = pattern.exec(text)) !== null) {
    pushText(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("`")) {
      nodes.push(
        <code
          key={`${keyPrefix}-c${n++}`}
          className="rounded bg-neutral-800 px-1 py-0.5 font-mono text-xs text-emerald-200"
        >
          {token.slice(1, -1)}
        </code>
      );
    } else if (token.startsWith("**")) {
      nodes.push(
        <strong
          key={`${keyPrefix}-b${n++}`}
          className="font-semibold text-neutral-100"
        >
          {token.slice(2, -2)}
        </strong>
      );
    } else {
      const labelEnd = token.indexOf("]");
      const label = token.slice(1, labelEnd);
      const href = token.slice(labelEnd + 2, -1);
      if (/^(https?:|mailto:)/i.test(href)) {
        nodes.push(
          <a
            key={`${keyPrefix}-a${n++}`}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-emerald-400 underline decoration-emerald-800 underline-offset-2 hover:text-emerald-300"
          >
            {label}
          </a>
        );
      } else {
        pushText(token);
      }
    }
    last = match.index + token.length;
  }
  pushText(text.slice(last));
  return nodes;
}

function SafeMarkdown({ text }: { text: string }) {
  // Tolerates partial tokens: an unclosed fence is closed before render.
  const segments = splitSegments(closeUnclosedFences(text));
  return (
    <>
      {segments.map((seg, i) =>
        seg.kind === "code" ? (
          <pre
            key={i}
            className="overflow-x-auto rounded-lg border border-neutral-800 bg-neutral-950 p-3 font-mono text-xs leading-relaxed text-neutral-200"
          >
            <code>{seg.text}</code>
          </pre>
        ) : (
          <span key={i} className="block space-y-2">
            {seg.text.split(/\n{2,}/).map((para, j) => (
              <p
                key={j}
                className="text-sm leading-relaxed text-neutral-200"
              >
                {renderInline(para, `${i}-${j}`)}
              </p>
            ))}
          </span>
        )
      )}
    </>
  );
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = (await res.json().catch(() => ({}))) as T & {
    detail?: string;
  };
  if (!res.ok) {
    throw new Error(body.detail ?? `request failed (${res.status})`);
  }
  return body;
}

export default function ChatPage() {
  const [peers, setPeers] = useState<Peer[]>([]);
  const [peerId, setPeerId] = useState("");
  const [question, setQuestion] = useState("");
  const [cards, setCards] = useState<ApprovalCard[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [askBusy, setAskBusy] = useState(false);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streamInput, setStreamInput] = useState("");
  const [streamText, setStreamText] = useState<string | null>(null);
  const [toolCards, setToolCards] = useState<StreamToolCard[]>([]);
  const [citations, setCitations] = useState<string[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [peerData, cardData, msgData] = await Promise.all([
        api<{ peers: Peer[] }>("/pairing/peers"),
        api<{ approvals: ApprovalCard[] }>("/ask/approvals"),
        api<{ messages: ChatMessage[] }>("/ask/messages"),
      ]);
      setPeers(peerData.peers);
      setCards(cardData.approvals);
      setMessages(msgData.messages);
      if (!peerId && peerData.peers.length > 0) {
        setPeerId(peerData.peers[0].agent_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load chat.");
    }
  }, [peerId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const sendAsk = useCallback(async () => {
    if (!peerId || question.trim().length === 0) return;
    setAskBusy(true);
    setError(null);
    setStatus(null);
    try {
      await api("/ask", {
        method: "POST",
        body: JSON.stringify({ peer_agent_id: peerId, question }),
      });
      setQuestion("");
      setStatus("Question sent. The peer approves before answering.");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send.");
    } finally {
      setAskBusy(false);
    }
  }, [peerId, question, refresh]);

  const decide = useCallback(
    async (approvalId: string, verdict: "approve" | "reject") => {
      setDeciding(approvalId);
      setError(null);
      try {
        await api(`/ask/approvals/${approvalId}/${verdict}`, {
          method: "POST",
        });
        setStatus(
          verdict === "approve" ? "Approved. An answer can follow." : "Denied."
        );
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Decision failed.");
      } finally {
        setDeciding(null);
      }
    },
    [refresh]
  );

  const runStream = useCallback(async () => {
    const prompt = streamInput.trim();
    if (prompt.length === 0 || streaming) return;
    setStreaming(true);
    setStreamError(null);
    setStreamText("");
    setToolCards([]);
    setCitations([]);
    try {
      const res = await fetch(
        `${API_BASE}/chat/stream?message=${encodeURIComponent(prompt)}`,
        { headers: { Accept: "text/event-stream" } }
      );
      if (!res.ok || !res.body) {
        const body = (await res.json().catch(() => ({}))) as {
          detail?: string;
        };
        throw new Error(body.detail ?? `stream failed (${res.status})`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let cut: number;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, cut);
          buf = buf.slice(cut + 2);
          for (const line of frame.split("\n")) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data:")) continue;
            const evt = JSON.parse(trimmed.slice(5)) as {
              type: string;
              text?: string;
              tool?: string;
              result?: string;
              citations?: string[];
              message?: string;
            };
            if (evt.type === "token" && evt.text) {
              setStreamText((prev) => (prev ?? "") + (evt.text as string));
            } else if (evt.type === "tool_pending" && evt.tool) {
              const tool = evt.tool;
              setToolCards((prev) => [...prev, { tool, status: "pending" }]);
            } else if (evt.type === "tool_completed" && evt.tool) {
              const tool = evt.tool;
              const result = evt.result;
              setToolCards((prev) => {
                const next = [...prev];
                const open = next.findIndex(
                  (c) => c.tool === tool && c.status === "pending"
                );
                if (open >= 0) {
                  next[open] = { tool, status: "completed", result };
                } else {
                  next.push({ tool, status: "completed", result });
                }
                return next;
              });
            } else if (evt.type === "done") {
              if (evt.text) {
                const text = evt.text;
                setStreamText((prev) =>
                  prev && prev.length > 0 ? prev : text
                );
              }
              setCitations(evt.citations ?? []);
            } else if (evt.type === "error") {
              throw new Error(evt.message ?? "Stream failed.");
            }
          }
        }
      }
    } catch (err) {
      setStreamError(err instanceof Error ? err.message : "Stream failed.");
    } finally {
      setStreaming(false);
    }
  }, [streamInput, streaming]);

  return (
    <main className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-50">
          Chat
        </h1>
        <p className="text-sm text-neutral-400">
          Ask a paired peer. Sensitive disclosures are denied by default.
        </p>
      </div>

      {status && (
        <p
          role="status"
          aria-live="polite"
          className="rounded-lg border border-emerald-900 bg-emerald-950 px-3 py-2 text-sm text-emerald-200"
        >
          {status}
        </p>
      )}
      {error && <ErrorState message={error} />}

      <section
        aria-labelledby="ask-heading"
        className="space-y-4 rounded-lg border border-neutral-800 bg-neutral-900 p-4 sm:p-5"
      >
        <h2
          id="ask-heading"
          className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
        >
          Ask
        </h2>
        {peers.length === 0 ? (
          <p className="text-sm text-neutral-500">
            No paired peers yet. Pair one first.
          </p>
        ) : (
          <div className="space-y-3">
            <div className="space-y-1">
              <label
                htmlFor="peer-picker"
                className="block text-xs font-medium uppercase tracking-wide text-neutral-500"
              >
                Peer
              </label>
              <select
                id="peer-picker"
                value={peerId}
                onChange={(e) => setPeerId(e.target.value)}
                disabled={askBusy}
                className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 focus:border-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-600/40 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {peers.map((peer) => (
                  <option key={peer.agent_id} value={peer.agent_id}>
                    {peer.display_name} ({peer.fingerprint})
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
              <div className="flex-1 space-y-1">
                <label
                  htmlFor="question-box"
                  className="block text-xs font-medium uppercase tracking-wide text-neutral-500"
                >
                  Question
                </label>
                <textarea
                  id="question-box"
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  placeholder="What do you need?"
                  rows={3}
                  disabled={askBusy}
                  className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 placeholder:text-neutral-600 focus:border-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-600/40 disabled:cursor-not-allowed disabled:opacity-50"
                />
              </div>
              <button
                type="button"
                onClick={sendAsk}
                disabled={
                  askBusy || !peerId || question.trim().length === 0
                }
                className="inline-flex shrink-0 items-center justify-center rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {askBusy ? "Sending…" : "Send"}
              </button>
            </div>
          </div>
        )}
      </section>

      <section
        aria-labelledby="stream-heading"
        className="space-y-4 rounded-lg border border-neutral-800 bg-neutral-900 p-4 sm:p-5"
      >
        <div className="space-y-1">
          <h2
            id="stream-heading"
            className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
          >
            Live search
          </h2>
          <p className="text-sm text-neutral-400">
            Streams a cited answer from the web. First token arrives fast.
          </p>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void runStream();
          }}
          className="flex flex-col gap-3 sm:flex-row sm:items-end"
        >
          <div className="flex-1 space-y-1">
            <label
              htmlFor="stream-box"
              className="block text-xs font-medium uppercase tracking-wide text-neutral-500"
            >
              Question
            </label>
            <textarea
              id="stream-box"
              value={streamInput}
              onChange={(e) => setStreamInput(e.target.value)}
              placeholder="What is happening today?"
              rows={2}
              disabled={streaming}
              className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 placeholder:text-neutral-600 focus:border-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-600/40 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <button
            type="submit"
            disabled={streaming || streamInput.trim().length === 0}
            className="inline-flex shrink-0 items-center justify-center rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {streaming ? "Streaming…" : "Ask live"}
          </button>
        </form>
        {streamError && <ErrorState message={streamError} />}
        {toolCards.length > 0 && (
          <ul aria-label="Tool activity" className="flex flex-wrap gap-2">
            {toolCards.map((card, i) => (
              <li
                key={`${card.tool}-${i}`}
                aria-busy={card.status === "pending"}
                className={
                  card.status === "pending"
                    ? "inline-flex max-w-full items-center gap-2 rounded-full border border-amber-900 bg-amber-950/40 px-3 py-1 font-mono text-xs text-amber-200"
                    : "inline-flex max-w-full items-center gap-2 rounded-full border border-emerald-900 bg-emerald-950/40 px-3 py-1 font-mono text-xs text-emerald-200"
                }
              >
                <span className="truncate">
                  {card.tool}:{" "}
                  {card.status === "pending" ? "searching…" : "done"}
                </span>
                {card.status === "completed" && card.result && (
                  <span className="max-w-[12rem] truncate text-neutral-400">
                    — {card.result.slice(0, 120)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
        {streaming && streamText === "" && toolCards.length === 0 && (
          <p aria-live="polite" className="animate-pulse text-sm text-neutral-500">
            Thinking…
          </p>
        )}
        {streamText !== null && streamText !== "" && (
          <article
            aria-live="polite"
            className="space-y-2 rounded-lg border border-neutral-800 bg-neutral-950 p-4"
          >
            <SafeMarkdown text={streamText} />
          </article>
        )}
        {citations.length > 0 && (
          <footer className="space-y-2 border-t border-neutral-800 pt-3">
            <h3 className="text-xs font-semibold uppercase tracking-widest text-neutral-500">
              Sources
            </h3>
            <ul className="space-y-1">
              {citations.map((url) => (
                <li key={url}>
                  <a
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="block truncate text-xs text-emerald-400 hover:text-emerald-300 hover:underline"
                  >
                    {url}
                  </a>
                </li>
              ))}
            </ul>
          </footer>
        )}
      </section>

      <section
        aria-labelledby="approvals-heading"
        className="space-y-4 rounded-lg border border-neutral-800 bg-neutral-900 p-4 sm:p-5"
      >
        <h2
          id="approvals-heading"
          className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
        >
          Approvals
        </h2>
        {cards.length === 0 ? (
          <p className="text-sm text-neutral-500">
            Nothing waiting for review.
          </p>
        ) : (
          <ul className="space-y-3">
            {cards.map((card) => (
              <li
                key={card.approval_id}
                className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-950 p-4"
              >
                <h3 className="text-sm font-semibold text-neutral-100">
                  {card.action} for {card.requester}
                </h3>
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                  <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                    Who
                  </dt>
                  <dd className="text-neutral-200">{card.requester}</dd>
                  <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                    What
                  </dt>
                  <dd className="text-neutral-200">
                    {card.question} ({card.data_category})
                  </dd>
                  <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                    Why
                  </dt>
                  <dd className="text-neutral-200">{card.purpose}</dd>
                  <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                    Expiry
                  </dt>
                  <dd className="font-mono text-xs text-neutral-400">
                    {card.expires_at}
                  </dd>
                </dl>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <button
                    type="button"
                    onClick={() => decide(card.approval_id, "approve")}
                    disabled={deciding === card.approval_id}
                    className="inline-flex items-center justify-center rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {deciding === card.approval_id ? "Working…" : "Approve"}
                  </button>
                  <button
                    type="button"
                    onClick={() => decide(card.approval_id, "reject")}
                    disabled={deciding === card.approval_id}
                    className="inline-flex items-center justify-center rounded-md border border-red-800 bg-red-900/60 px-4 py-2 text-sm font-medium text-red-100 hover:bg-red-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {deciding === card.approval_id ? "Working…" : "Deny"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section
        aria-labelledby="messages-heading"
        className="space-y-4 rounded-lg border border-neutral-800 bg-neutral-900 p-4 sm:p-5"
      >
        <h2
          id="messages-heading"
          className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
        >
          Messages
        </h2>
        {messages.length === 0 ? (
          <p className="text-sm text-neutral-500">No messages yet.</p>
        ) : (
          <ul className="space-y-2">
            {messages.map((msg) => {
              const isRequest = msg.message_type === "request";
              return (
                <li
                  key={msg.message_id}
                  className={
                    isRequest
                      ? "ml-auto max-w-[85%] rounded-lg border border-emerald-900 bg-emerald-950/40 px-3 py-2"
                      : "mr-auto max-w-[85%] rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-2"
                  }
                >
                  <p className="font-mono text-[11px] uppercase tracking-wide text-neutral-500">
                    {msg.message_type} ({msg.status})
                  </p>
                  <p className="text-sm text-neutral-200">
                    {msg.sender} → {msg.recipient}
                  </p>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </main>
  );
}
