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
    <p role="alert">
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
      nodes.push(<code key={`${keyPrefix}-c${n++}`}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**")) {
      nodes.push(
        <strong key={`${keyPrefix}-b${n++}`}>{token.slice(2, -2)}</strong>
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
          <pre key={i}>
            <code>{seg.text}</code>
          </pre>
        ) : (
          <span key={i}>
            {seg.text.split(/\n{2,}/).map((para, j) => (
              <p key={j}>{renderInline(para, `${i}-${j}`)}</p>
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
    <main>
      <h1>Chat</h1>
      <p>Ask a paired peer. Sensitive disclosures are denied by default.</p>

      {status && (
        <p role="status" aria-live="polite">
          {status}
        </p>
      )}
      {error && <ErrorState message={error} />}

      <section aria-labelledby="ask-heading">
        <h2 id="ask-heading">Ask</h2>
        {peers.length === 0 ? (
          <p>No paired peers yet. Pair one first.</p>
        ) : (
          <div>
            <label htmlFor="peer-picker">Peer</label>
            <select
              id="peer-picker"
              value={peerId}
              onChange={(e) => setPeerId(e.target.value)}
              disabled={askBusy}
            >
              {peers.map((peer) => (
                <option key={peer.agent_id} value={peer.agent_id}>
                  {peer.display_name} ({peer.fingerprint})
                </option>
              ))}
            </select>
            <label htmlFor="question-box">Question</label>
            <textarea
              id="question-box"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="What do you need?"
              rows={3}
              disabled={askBusy}
            />
            <button
              type="button"
              onClick={sendAsk}
              disabled={
                askBusy || !peerId || question.trim().length === 0
              }
            >
              {askBusy ? "Sending…" : "Send"}
            </button>
          </div>
        )}
      </section>

      <section aria-labelledby="stream-heading">
        <h2 id="stream-heading">Live search</h2>
        <p>Streams a cited answer from the web. First token arrives fast.</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void runStream();
          }}
        >
          <label htmlFor="stream-box">Question</label>
          <textarea
            id="stream-box"
            value={streamInput}
            onChange={(e) => setStreamInput(e.target.value)}
            placeholder="What is happening today?"
            rows={2}
            disabled={streaming}
          />
          <button
            type="submit"
            disabled={streaming || streamInput.trim().length === 0}
          >
            {streaming ? "Streaming…" : "Ask live"}
          </button>
        </form>
        {streamError && <ErrorState message={streamError} />}
        {toolCards.length > 0 && (
          <ul aria-label="Tool activity">
            {toolCards.map((card, i) => (
              <li
                key={`${card.tool}-${i}`}
                aria-busy={card.status === "pending"}
              >
                {card.tool}:{" "}
                {card.status === "pending" ? "searching…" : "done"}
                {card.status === "completed" && card.result && (
                  <span> — {card.result.slice(0, 120)}</span>
                )}
              </li>
            ))}
          </ul>
        )}
        {streaming && streamText === "" && toolCards.length === 0 && (
          <p aria-live="polite">Thinking…</p>
        )}
        {streamText !== null && streamText !== "" && (
          <article aria-live="polite">
            <SafeMarkdown text={streamText} />
          </article>
        )}
        {citations.length > 0 && (
          <footer>
            <h3>Sources</h3>
            <ul>
              {citations.map((url) => (
                <li key={url}>
                  <a href={url} target="_blank" rel="noopener noreferrer">
                    {url}
                  </a>
                </li>
              ))}
            </ul>
          </footer>
        )}
      </section>

      <section aria-labelledby="approvals-heading">
        <h2 id="approvals-heading">Approvals</h2>
        {cards.length === 0 ? (
          <p>Nothing waiting for review.</p>
        ) : (
          <ul>
            {cards.map((card) => (
              <li key={card.approval_id}>
                <h3>
                  {card.action} for {card.requester}
                </h3>
                <dl>
                  <dt>Who</dt>
                  <dd>{card.requester}</dd>
                  <dt>What</dt>
                  <dd>
                    {card.question} ({card.data_category})
                  </dd>
                  <dt>Why</dt>
                  <dd>{card.purpose}</dd>
                  <dt>Expiry</dt>
                  <dd>{card.expires_at}</dd>
                </dl>
                <button
                  type="button"
                  onClick={() => decide(card.approval_id, "approve")}
                  disabled={deciding === card.approval_id}
                >
                  {deciding === card.approval_id ? "Working…" : "Approve"}
                </button>
                <button
                  type="button"
                  onClick={() => decide(card.approval_id, "reject")}
                  disabled={deciding === card.approval_id}
                >
                  {deciding === card.approval_id ? "Working…" : "Deny"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="messages-heading">
        <h2 id="messages-heading">Messages</h2>
        {messages.length === 0 ? (
          <p>No messages yet.</p>
        ) : (
          <ul>
            {messages.map((msg) => (
              <li key={msg.message_id}>
                {msg.message_type} ({msg.status}): {msg.sender} →{" "}
                {msg.recipient}
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
