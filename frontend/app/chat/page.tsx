"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import Link from "next/link";

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

type DeliveryStatus = {
  mode: "relayed" | "queued" | "delivery_failed" | "local-only";
  status?: string;
  reason?: string;
};

/** Observable delivery state for a stored row: "sent" means stored
 * locally with background delivery in flight (refresh to confirm). */
function deliveryLabel(status: string): string {
  if (status === "relayed") return "relayed";
  if (status === "delivery_failed") return "Delivery failed — retry";
  if (status === "sent") return "Queued — delivering in background";
  return status;
}

type ToAnswer = {
  correlation_id: string;
  requester: string;
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

/** Display-only: readable text from a stored envelope payload. */
function messageBody(msg: ChatMessage): string {
  const payload =
    (msg.envelope as { payload?: Record<string, unknown> })?.payload ?? {};
  for (const key of ["answer", "question", "text", "message", "reason"]) {
    const value = payload[key];
    if (typeof value === "string" && value.trim().length > 0) return value;
  }
  return "";
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
  // One recall/extraction scope per page load: stream recall injects
  // only this session's memories, and extraction records the same id.
  const [sessionId, setSessionId] = useState(
    () =>
      `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
  );
  // Sidebar visibility only: presentation state, no chat semantics.
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [live, setLive] = useState<"off" | "on" | "down">("off");
  const [toAnswer, setToAnswer] = useState<ToAnswer[]>([]);
  const [answerText, setAnswerText] = useState<Record<string, string>>({});
  const [answering, setAnswering] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);

  // Polled rows indexed by id/correlation for delivery-state lookups.
  const messagesById = new Map(messages.map((m) => [m.message_id, m]));
  const messagesByCorrelation = new Map(
    messages.map((m) => [m.correlation_id, m])
  );

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

  // Live relay deliveries: the backend holds the signed relay socket
  // (/ask/live bridges it); every delivery refreshes approvals/messages.
  // Best-effort: manual refresh below always works when live is down.
  useEffect(() => {
    let closed = false;
    let ws: WebSocket | null = null;
    try {
      ws = new WebSocket(`${API_BASE.replace(/^http/, "ws")}/ask/live`);
    } catch {
      setLive("down");
      return () => {};
    }
    ws.onopen = () => {
      if (!closed) setLive("on");
    };
    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data as string) as { type?: string };
        if (msg.type === "delivery") void refresh();
      } catch {
        /* ignore malformed frames */
      }
    };
    const markDown = () => {
      if (!closed) setLive("down");
    };
    ws.onerror = markDown;
    ws.onclose = markDown;
    return () => {
      closed = true;
      try {
        ws?.close();
      } catch {
        /* teardown is best-effort */
      }
    };
  }, [refresh]);

  const sendAsk = useCallback(async () => {
    if (!peerId || question.trim().length === 0) return;
    setAskBusy(true);
    setError(null);
    setStatus(null);
    try {
      const data = await api<{ delivery?: DeliveryStatus }>("/ask", {
        method: "POST",
        body: JSON.stringify({ peer_agent_id: peerId, question }),
      });
      const delivery = data.delivery;
      if (delivery?.mode === "relayed") {
        setStatus(
          `Question sent via relay (${delivery.status ?? "queued"}). The peer approves before answering.`
        );
      } else if (delivery?.mode === "queued") {
        setStatus(
          "Queued — delivering in background; refresh to confirm."
        );
      } else if (delivery?.mode === "delivery_failed") {
        setStatus(
          `Delivery failed (${delivery.reason ?? "unknown"}) — retry from the message below.`
        );
      } else if (delivery?.mode === "local-only") {
        setStatus(
          `Relay unreachable (${delivery.reason ?? "unknown"}) — question stored locally only.`
        );
      } else {
        setStatus("Question sent. The peer approves before answering.");
      }
      setQuestion("");
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
        const card = cards.find((c) => c.approval_id === approvalId) ?? null;
        const data = await api<{ delivery?: DeliveryStatus }>(
          `/ask/approvals/${approvalId}/${verdict}`,
          { method: "POST" }
        );
        const via =
          data.delivery?.mode === "relayed"
            ? ` via relay (${data.delivery.status ?? "queued"})`
            : data.delivery?.mode === "queued"
              ? " (queued — delivering in background; refresh to confirm)"
              : data.delivery?.mode === "delivery_failed"
                ? " (delivery failed — retry from the message below)"
                : data.delivery?.mode === "local-only"
                  ? " (relay unreachable — stored locally only)"
                  : "";
        if (verdict === "approve") {
          setStatus(`Approved${via}. An answer can follow.`);
          if (card) {
            setToAnswer((prev) =>
              prev.some((t) => t.correlation_id === card.correlation_id)
                ? prev
                : [
                    ...prev,
                    {
                      correlation_id: card.correlation_id,
                      requester: card.requester,
                    },
                  ]
            );
          }
        } else {
          setStatus(`Denied${via}.`);
        }
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Decision failed.");
      } finally {
        setDeciding(null);
      }
    },
    [cards, refresh]
  );

  const sendAnswer = useCallback(
    async (correlationId: string) => {
      const target = toAnswer.find(
        (t) => t.correlation_id === correlationId
      );
      const text = (answerText[correlationId] ?? "").trim();
      if (!target || text.length === 0 || answering) return;
      setAnswering(correlationId);
      setError(null);
      try {
        const data = await api<{ delivery?: DeliveryStatus }>(
          "/ask/response",
          {
            method: "POST",
            body: JSON.stringify({
              peer_agent_id: target.requester,
              correlation_id: target.correlation_id,
              answer: text,
            }),
          }
        );
        const delivery = data.delivery;
        setStatus(
          delivery?.mode === "relayed"
            ? `Answer sent via relay (${delivery.status ?? "queued"}).`
            : delivery?.mode === "queued"
              ? "Queued — delivering in background; refresh to confirm."
              : delivery?.mode === "delivery_failed"
                ? `Delivery failed (${delivery.reason ?? "unknown"}) — retry from the message below.`
                : delivery?.mode === "local-only"
                  ? `Relay unreachable (${delivery.reason ?? "unknown"}) — answer stored locally only.`
                  : "Answer sent."
        );
        setToAnswer((prev) =>
          prev.filter((t) => t.correlation_id !== correlationId)
        );
        setAnswerText((prev) => {
          const next = { ...prev };
          delete next[correlationId];
          return next;
        });
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not send answer.");
      } finally {
        setAnswering(null);
      }
    },
    [toAnswer, answerText, answering, refresh]
  );

  const retryDelivery = useCallback(
    async (messageId: string) => {
      const row =
        messagesById.get(messageId) ??
        messagesByCorrelation.get(messageId);
      if (!row || row.status !== "delivery_failed" || retrying) return;
      setRetrying(row.message_id);
      setError(null);
      try {
        await api(`/ask/messages/${row.message_id}/retry`, {
          method: "POST",
        });
        setStatus(
          "Retry queued — delivering in background; refresh to confirm."
        );
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Retry failed.");
      } finally {
        setRetrying(null);
      }
    },
    [messagesById, messagesByCorrelation, retrying, refresh]
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
        `${API_BASE}/chat/stream?message=${encodeURIComponent(prompt)}&session_id=${encodeURIComponent(sessionId)}`,
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
  }, [streamInput, streaming, sessionId]);

  // "New chat" rotates the recall/extraction scope and clears the
  // session-scoped stream turn. Stored ask rows are global and reload
  // from the server, so there is nothing local to clear for them.
  const newChat = useCallback(() => {
    if (streaming) return;
    setStreamInput("");
    setStreamText(null);
    setToolCards([]);
    setCitations([]);
    setStreamError(null);
    setSessionId(
      `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
    );
    setSidebarOpen(false);
  }, [streaming]);

  // Display-only derived values for the conversational thread layout.
  const selectedPeer = peers.find((p) => p.agent_id === peerId) ?? null;
  const pendingCount = cards.length;
  const cardsByCorrelation = new Map<string, ApprovalCard[]>();
  for (const card of cards) {
    const list = cardsByCorrelation.get(card.correlation_id) ?? [];
    list.push(card);
    cardsByCorrelation.set(card.correlation_id, list);
  }
  const seenCorrelations = new Set(messages.map((m) => m.correlation_id));
  const trailingCards = cards.filter(
    (c) => !seenCorrelations.has(c.correlation_id)
  );
  const hasStreamTurn =
    (streamText !== null && streamText !== "") ||
    toolCards.length > 0 ||
    citations.length > 0 ||
    streaming ||
    streamError !== null;
  const threadEmpty =
    messages.length === 0 &&
    cards.length === 0 &&
    !hasStreamTurn &&
    !status &&
    !error;

  return (
    <div className="flex min-h-screen bg-neutral-950 text-neutral-200">
      {sidebarOpen && (
        <button
          type="button"
          aria-label="Close sidebar"
          onClick={() => setSidebarOpen(false)}
          className="fixed inset-0 z-30 bg-black/60 md:hidden"
        />
      )}
      <aside
        id="chat-sidebar"
        aria-label="Chat sidebar"
        className={`fixed inset-y-0 left-0 z-40 flex w-64 shrink-0 -translate-x-full flex-col border-r border-neutral-800 bg-neutral-900 transition-transform md:static md:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : ""
        }`}
      >
        <div className="flex items-center gap-2 p-3">
          <button
            type="button"
            onClick={newChat}
            disabled={streaming}
            aria-label="Start a new chat"
            className="inline-flex flex-1 items-center justify-center gap-2 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm font-medium text-neutral-100 hover:border-emerald-700 hover:text-emerald-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <span aria-hidden="true" className="text-base leading-none">
              +
            </span>
            New chat
          </button>
          <button
            type="button"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close sidebar"
            className="inline-flex items-center justify-center rounded-lg border border-neutral-800 px-2 py-2 text-sm text-neutral-400 hover:text-neutral-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 md:hidden"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>
        <nav
          aria-label="Secondary"
          className="flex flex-col gap-1 px-3 text-sm"
        >
          <Link
            href="/people"
            className="rounded-lg px-3 py-2 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-600"
          >
            People
          </Link>
          <Link
            href="/memory"
            className="rounded-lg px-3 py-2 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-600"
          >
            Memory
          </Link>
        </nav>
        <div className="mt-auto space-y-1 border-t border-neutral-800 p-3">
          <p
            aria-live="polite"
            className="truncate font-mono text-[11px] text-neutral-500"
          >
            {selectedPeer
              ? `${selectedPeer.display_name} · ${selectedPeer.fingerprint}`
              : "no peer selected"}
          </p>
          <p className="text-xs text-neutral-500">
            {live === "on" ? "live relay on" : "live relay off"}
          </p>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-neutral-800 bg-neutral-950/95 px-3 py-2 backdrop-blur">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open sidebar"
            aria-expanded={sidebarOpen}
            aria-controls="chat-sidebar"
            className="inline-flex items-center justify-center rounded-md border border-neutral-800 px-2 py-1.5 text-neutral-300 hover:text-neutral-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 md:hidden"
          >
            <svg
              aria-hidden="true"
              width="18"
              height="18"
              viewBox="0 0 18 18"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            >
              <line x1="2" y1="4" x2="16" y2="4" />
              <line x1="2" y1="9" x2="16" y2="9" />
              <line x1="2" y1="14" x2="16" y2="14" />
            </svg>
          </button>
          <h1 className="text-sm font-semibold tracking-tight text-neutral-50">
            Chat
          </h1>
        {peers.length === 0 ? (
          <p className="text-xs text-neutral-500">No paired peers yet.</p>
        ) : (
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1">
            <label
              htmlFor="peer-picker"
              className="text-xs font-medium uppercase tracking-wide text-neutral-500"
            >
              Peer
            </label>
            <select
              id="peer-picker"
              value={peerId}
              onChange={(e) => setPeerId(e.target.value)}
              disabled={askBusy || streaming}
              aria-label="Peer"
              className="min-w-0 max-w-full flex-1 rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-200 focus:border-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-600/40 disabled:cursor-not-allowed disabled:opacity-50 sm:max-w-xs"
            >
              {peers.map((peer) => (
                <option key={peer.agent_id} value={peer.agent_id}>
                  {peer.display_name} ({peer.fingerprint})
                </option>
              ))}
            </select>
            <p aria-live="polite" className="min-w-0 truncate font-mono text-[11px] text-neutral-500">
              {selectedPeer
                ? `connected · ${selectedPeer.display_name} · ${selectedPeer.fingerprint}`
                : "no peer selected"}
            </p>
          </div>
        )}
        <span
          aria-label={`${pendingCount} pending approvals`}
          title={`${pendingCount} pending approvals`}
          className={
            pendingCount > 0
              ? "inline-flex items-center rounded-full border border-amber-900 bg-amber-950/40 px-2 py-0.5 text-xs font-medium text-amber-200"
              : "inline-flex items-center rounded-full border border-neutral-800 bg-neutral-950 px-2 py-0.5 text-xs text-neutral-500"
          }
        >
          {pendingCount} pending
        </span>
        <span
          aria-label={live === "on" ? "Live relay updates on" : "Live relay updates off"}
          title={
            live === "on"
              ? "Relay deliveries refresh this view live"
              : "Live updates unavailable — approvals refresh on send/decide"
          }
          className={
            live === "on"
              ? "inline-flex items-center rounded-full border border-emerald-900 bg-emerald-950/40 px-2 py-0.5 text-xs font-medium text-emerald-300"
              : "inline-flex items-center rounded-full border border-neutral-800 bg-neutral-950 px-2 py-0.5 text-xs text-neutral-500"
          }
        >
          {live === "on" ? "live" : "live off"}
        </span>
      </header>

        <main className="flex flex-1 flex-col px-3 py-4 sm:px-4">
          <section
            aria-labelledby="messages-heading"
            className="mx-auto flex w-full max-w-3xl flex-1 flex-col"
          >
            <h2 id="messages-heading" className="sr-only">
              Conversation
            </h2>
            {threadEmpty ? (
              <div className="flex flex-1 items-center justify-center p-8">
                <p className="text-center text-sm text-neutral-500">
                  No messages yet.
                </p>
              </div>
            ) : (
              <ul className="flex-1 space-y-4">
            {status && (
              <li>
                <p
                  role="status"
                  aria-live="polite"
                  className="mx-auto max-w-md rounded-lg border border-emerald-900 bg-emerald-950 px-3 py-2 text-center text-sm text-emerald-200"
                >
                  {status}
                </p>
              </li>
            )}
            {error && (
              <li>
                <ErrorState message={error} />
              </li>
            )}

            {messages.map((msg) => {
              const isRequest = msg.message_type === "request";
              const body = messageBody(msg);
              const inlineCards =
                cardsByCorrelation.get(msg.correlation_id) ?? [];
              return (
                <Fragment key={msg.message_id}>
                  <li
                    className={
                      isRequest ? "flex justify-end" : "flex gap-2.5"
                    }
                  >
                    {!isRequest && (
                      <div
                        aria-hidden="true"
                        className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-emerald-900 bg-emerald-950 text-xs font-semibold text-emerald-300"
                      >
                        N
                      </div>
                    )}
                    <div
                      className={
                        isRequest
                          ? "ml-auto max-w-[85%] rounded-2xl rounded-br-sm border border-emerald-900 bg-emerald-950/40 px-3 py-2"
                          : "min-w-0 flex-1 rounded-2xl border border-neutral-800 bg-neutral-950 px-3 py-2"
                      }
                    >
                      <p className="font-mono text-[11px] uppercase tracking-wide text-neutral-500">
                        {msg.message_type} ({deliveryLabel(msg.status)})
                      </p>
                      {body ? (
                        isRequest ? (
                          <p className="text-sm text-neutral-100">{body}</p>
                        ) : (
                          <SafeMarkdown text={body} />
                        )
                      ) : (
                        <p className="text-sm text-neutral-200">
                          {msg.sender} → {msg.recipient}
                        </p>
                      )}
                      {body && (
                        <p className="mt-1 text-xs text-neutral-500">
                          {msg.sender} → {msg.recipient}
                        </p>
                      )}
                      {msg.status === "delivery_failed" && (
                        <button
                          type="button"
                          onClick={() => void retryDelivery(msg.message_id)}
                          disabled={retrying === msg.message_id}
                          aria-label={`Retry delivery of ${msg.message_id}`}
                          className="mt-2 inline-flex items-center justify-center rounded-md border border-amber-800 bg-amber-900/60 px-3 py-1 text-xs font-medium text-amber-100 hover:bg-amber-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {retrying === msg.message_id
                            ? "Retrying…"
                            : "Retry delivery"}
                        </button>
                      )}
                    </div>
                  </li>
                  {inlineCards.map((card) => (
                    <li key={card.approval_id} className="flex justify-start">
                      <div
                        aria-label={`Approval ${card.action} for ${card.requester}`}
                        className="w-full space-y-3 rounded-2xl border border-amber-900/60 bg-neutral-950 px-3 py-2"
                      >
                        <h3 className="text-sm font-semibold text-neutral-100">
                          {card.action} for {card.requester}
                        </h3>
                        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                          <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                            Who
                          </dt>
                          <dd className="text-neutral-200">
                            {card.requester}
                          </dd>
                          <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                            What
                          </dt>
                          <dd className="text-neutral-200">
                            {card.question} ({card.data_category})
                          </dd>
                          <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                            Why
                          </dt>
                          <dd className="text-neutral-200">
                            {card.purpose}
                          </dd>
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
                            {deciding === card.approval_id
                              ? "Working…"
                              : "Approve"}
                          </button>
                          <button
                            type="button"
                            onClick={() => decide(card.approval_id, "reject")}
                            disabled={deciding === card.approval_id}
                            className="inline-flex items-center justify-center rounded-md border border-red-800 bg-red-900/60 px-4 py-2 text-sm font-medium text-red-100 hover:bg-red-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            {deciding === card.approval_id
                              ? "Working…"
                              : "Deny"}
                          </button>
                        </div>
                      </div>
                    </li>
                  ))}
                </Fragment>
              );
            })}

            {trailingCards.map((card) => (
              <li key={card.approval_id} className="flex justify-start">
                <div
                  aria-label={`Approval ${card.action} for ${card.requester}`}
                  className="w-full space-y-3 rounded-2xl border border-amber-900/60 bg-neutral-950 px-3 py-2"
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
                </div>
              </li>
            ))}
            {hasStreamTurn && (
              <li className="flex gap-2.5">
                <div
                  aria-hidden="true"
                  className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-emerald-900 bg-emerald-950 text-xs font-semibold text-emerald-300"
                >
                  N
                </div>
                <div className="min-w-0 flex-1 space-y-2 rounded-2xl border border-neutral-800 bg-neutral-950 px-3 py-2">
                  {streamError && <ErrorState message={streamError} />}
                  {toolCards.length > 0 && (
                    <ul
                      aria-label="Tool activity"
                      className="flex flex-wrap gap-2"
                    >
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
                            {card.status === "pending"
                              ? "searching…"
                              : "done"}
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
                  {streaming &&
                    streamText === "" &&
                    toolCards.length === 0 && (
                      <p
                        aria-live="polite"
                        className="animate-pulse text-sm text-neutral-500"
                      >
                        Thinking…
                      </p>
                    )}
                  {streamText !== null && streamText !== "" && (
                    <article aria-live="polite" className="space-y-2">
                      <SafeMarkdown text={streamText} />
                    </article>
                  )}
                  {citations.length > 0 && (
                    <ul
                      aria-label="Sources"
                      className="space-y-1 border-t border-neutral-800 pt-2"
                    >
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
                  )}
                </div>
              </li>
            )}
              </ul>
            )}
          </section>

          {toAnswer.length > 0 && (
            <section
              aria-labelledby="answer-heading"
              className="mt-4 space-y-3 rounded-lg border border-neutral-800 bg-neutral-900 p-3"
            >
          <h2
            id="answer-heading"
            className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
          >
            Approved — send an answer
          </h2>
          <ul className="space-y-3">
            {toAnswer.map((item) => (
              <li
                key={item.correlation_id}
                className="space-y-2 rounded-lg border border-neutral-800 bg-neutral-950 p-3"
              >
                <p className="font-mono text-[11px] text-neutral-500">
                  to {item.requester} · {item.correlation_id}
                </p>
                <label
                  htmlFor={`answer-${item.correlation_id}`}
                  className="sr-only"
                >
                  Answer
                </label>
                <textarea
                  id={`answer-${item.correlation_id}`}
                  value={answerText[item.correlation_id] ?? ""}
                  onChange={(e) =>
                    setAnswerText((prev) => ({
                      ...prev,
                      [item.correlation_id]: e.target.value,
                    }))
                  }
                  placeholder="Type the answer…"
                  rows={2}
                  disabled={answering === item.correlation_id}
                  className="w-full resize-none rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 placeholder:text-neutral-600 focus:border-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-600/40 disabled:cursor-not-allowed disabled:opacity-50"
                />
                <button
                  type="button"
                  onClick={() => void sendAnswer(item.correlation_id)}
                  disabled={
                    answering === item.correlation_id ||
                    (answerText[item.correlation_id] ?? "").trim().length === 0
                  }
                  className="inline-flex items-center justify-center rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {answering === item.correlation_id
                    ? "Sending…"
                    : "Send answer"}
                </button>
              </li>
            ))}
            </ul>
          </section>
          )}
        </main>

        <footer className="sticky bottom-0 z-20 border-t border-neutral-800 bg-neutral-950 px-3 pb-4 pt-2 sm:px-4">
          <div className="mx-auto w-full max-w-3xl space-y-2">
            <section aria-labelledby="stream-heading">
              <h2 id="stream-heading" className="sr-only">
                Live search
              </h2>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void runStream();
                }}
                className="flex items-center gap-2"
              >
                <div className="flex-1">
                  <label htmlFor="stream-box" className="sr-only">
                    Ask live
                  </label>
                  <textarea
                    id="stream-box"
                    value={streamInput}
                    onChange={(e) => setStreamInput(e.target.value)}
                    placeholder="Ask live… (web answer with sources)"
                    rows={1}
                    disabled={streaming || askBusy}
                    className="w-full resize-none rounded-full border border-neutral-800 bg-neutral-900 px-3 py-1.5 text-sm text-neutral-200 placeholder:text-neutral-600 focus:border-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-600/40 disabled:cursor-not-allowed disabled:opacity-50"
                  />
                </div>
                <button
                  type="submit"
                  disabled={
                    streaming || askBusy || streamInput.trim().length === 0
                  }
                  className="inline-flex shrink-0 items-center justify-center rounded-full border border-neutral-700 bg-neutral-950 px-3 py-1.5 text-xs font-medium text-neutral-200 hover:border-emerald-700 hover:text-emerald-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {streaming ? "Streaming…" : "Ask live"}
                </button>
              </form>
            </section>

            <section
              aria-labelledby="ask-heading"
              className="rounded-2xl border border-neutral-700 bg-neutral-900 p-2 focus-within:border-emerald-600 focus-within:ring-2 focus-within:ring-emerald-600/40"
            >
              <h2 id="ask-heading" className="sr-only">
                Ask
              </h2>
              {peers.length === 0 ? (
          <p className="text-sm text-neutral-500">
            No paired peers yet. Pair one first.
          </p>
        ) : (
            <div className="flex items-end gap-2">
              <div className="flex-1">
                <label htmlFor="question-box" className="sr-only">
                  Message
                </label>
                <textarea
                  id="question-box"
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void sendAsk();
                    }
                  }}
                  placeholder="Message… (Enter to send, Shift+Enter for a new line)"
                  rows={2}
                  disabled={askBusy || streaming}
                  className="w-full resize-none bg-transparent px-3 py-2 text-sm text-neutral-200 placeholder:text-neutral-600 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
                />
              </div>
              <button
                type="button"
                onClick={sendAsk}
                disabled={
                  askBusy ||
                  streaming ||
                  !peerId ||
                  question.trim().length === 0
                }
                aria-label="Send message"
                className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-emerald-600 text-white hover:bg-emerald-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
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
              )}
            </section>
          </div>
        </footer>
      </div>
    </div>
  );
}
