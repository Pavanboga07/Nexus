"use client";

import { Fragment, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { closeUnclosedFences, splitSegments } from "./markdown";
import { API_BASE, api, isExpiredError, wsBase } from "../components/api";
import { LlmStatusLine } from "../components/llm-key-settings";
import { authHeaders } from "../components/operator";

const SESSION_KEY = "nexus-session-id";

function mintSessionId(): string {
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function loadOrCreateSessionId(): string {
  if (typeof localStorage === "undefined") return mintSessionId();
  try {
    const existing = localStorage.getItem(SESSION_KEY);
    if (existing) return existing;
  } catch {
  }
  const fresh = mintSessionId();
  try {
    localStorage.setItem(SESSION_KEY, fresh);
  } catch {
  }
  return fresh;
}

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

function deliveryLabel(status: string): string {
  if (status === "relayed") return "relayed";
  if (status === "delivery_failed") return "Delivery failed — retry";
  if (status === "sent") return "Queued — delivering in background";
  return status;
}

function typeLabel(messageType: string): string {
  switch (messageType) {
    case "request":
      return "Question";
    case "response":
      return "Answer";
    case "approval_request":
      return "Approval needed";
    case "approve":
      return "Approved";
    case "reject":
      return "Denied";
    case "error":
      return "Error";
    default:
      return messageType || "message";
  }
}

/** Human name for an agent id: peer display name when known, else a
 * short fingerprint instead of the full crypto id. */
function peerDisplayName(
  peers: { agent_id: string; display_name: string }[],
  id: string
): string {
  const known = peers.find((p) => p.agent_id === id);
  if (known?.display_name) return known.display_name;
  const m = id.match(/^nexus:[a-z0-9]+:([0-9a-f]{8})/i);
  if (m) return `agent ${m[1]}`;
  return id.length > 24 ? `${id.slice(0, 12)}…` : id;
}

type CommGroup = {
  correlationId: string;
  request: ChatMessage | null;
  response: ChatMessage | null;
  others: ChatMessage[];
};

function groupCompletedExchanges(messages: ChatMessage[]): {
  groups: CommGroup[];
  leftovers: ChatMessage[];
} {
  const byCorr = new Map<string, ChatMessage[]>();
  for (const msg of messages) {
    const list = byCorr.get(msg.correlation_id) ?? [];
    list.push(msg);
    byCorr.set(msg.correlation_id, list);
  }
  const groups: CommGroup[] = [];
  const grouped = new Set<string>();
  byCorr.forEach((list, corr) => {
    if (!corr) return;
    const request =
      list.find((m: ChatMessage) => m.message_type === "request") ?? null;
    const response =
      list.find((m: ChatMessage) => m.message_type === "response") ?? null;
    if (request && response) {
      grouped.add(corr);
      groups.push({
        correlationId: corr,
        request,
        response,
        others: list.filter((m: ChatMessage) => m !== request && m !== response),
      });
    }
  });
  return {
    groups,
    leftovers: messages.filter((m) => !grouped.has(m.correlation_id)),
  };
}

function bestDelivery(msgs: ChatMessage[]): string {
  const rank = (s: string) =>
    s === "relayed" ? 0 : s === "sent" ? 1 : s === "stored" ? 2 : 3;
  const sorted = [...msgs].sort((a, b) => rank(a.status) - rank(b.status));
  return sorted.length > 0 ? deliveryLabel(sorted[0].status) : "stored";
}

/** One completed agent-to-agent exchange, collapsed with expandable
 * details. Only rendered when request + response both exist, so every
 * checkmark reflects something that actually happened. */
function CommCard({
  group,
  peers,
  pending,
  retryingId,
  onRetry,
}: {
  group: CommGroup;
  peers: { agent_id: string; display_name: string; fingerprint: string }[];
  pending: boolean;
  retryingId: string | null;
  onRetry: (messageId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const asker = group.request
    ? peerDisplayName(peers, group.request.sender)
    : "An agent";
  const answerer = group.response
    ? peerDisplayName(peers, group.response.sender)
    : "A peer agent";
  const question = group.request ? messageBody(group.request) : "";
  const answer = group.response ? messageBody(group.response) : "";
  const delivery = bestDelivery(
    [group.request, group.response, ...group.others].filter(
      (m): m is ChatMessage => m !== null
    )
  );
  const failedMsg: ChatMessage | undefined = [
    group.request,
    group.response,
    ...group.others,
  ].find(
    (m): m is ChatMessage => m !== null && m.status === "delivery_failed"
  );
  return (
    <li className="rounded-xl border border-line bg-bg-subtle">
      <div className="px-4 py-3">
        <p className="text-xs font-semibold uppercase tracking-widest text-ink-3">
          Agent communication
        </p>
        <p className="mt-1 text-sm text-ink">
          {asker} asked {answerer}
          {question ? ` — “${question.slice(0, 120)}${question.length > 120 ? "…" : ""}”` : ""}
        </p>
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
          <li>
            <span aria-hidden="true" className="text-emerald-600">✓</span>{" "}
            {pending ? "Waiting for approval" : "Approved"}
          </li>
          <li>
            <span aria-hidden="true" className="text-emerald-600">✓</span>{" "}
            {delivery}
          </li>
          {answer && (
            <li>
              <span aria-hidden="true" className="text-emerald-600">✓</span>{" "}
              Answer received
            </li>
          )}
        </ul>
        {answer && !open && (
          <p className="mt-2 line-clamp-2 text-sm text-ink-2">{answer}</p>
        )}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="mt-2 text-xs font-medium text-accent hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {open ? "Hide details" : "View details →"}
        </button>
      </div>
      {open && (
        <div className="space-y-3 border-t border-line px-4 py-3">
          {[group.request, group.response, ...group.others]
            .filter((m): m is ChatMessage => m !== null)
            .map((m) => {
              const text = messageBody(m);
              return (
                <div key={m.message_id} className="space-y-1">
                  <p className="font-mono text-[11px] uppercase tracking-wide text-ink-3">
                    {typeLabel(m.message_type)} ({deliveryLabel(m.status)})
                  </p>
                  {text ? (
                    <p className="text-sm text-ink">{text}</p>
                  ) : (
                    <p className="text-sm text-ink-2">
                      {peerDisplayName(peers, m.sender)} →{" "}
                      {peerDisplayName(peers, m.recipient)}
                    </p>
                  )}
                  <p className="font-mono text-[11px] text-ink-3">
                    {m.sender} → {m.recipient}
                  </p>
                  {m.status === "delivery_failed" && (
                    <button
                      type="button"
                      onClick={() => onRetry(m.message_id)}
                      disabled={retryingId === m.message_id}
                      className="inline-flex items-center justify-center rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {retryingId === m.message_id ? "Retrying…" : "Retry delivery"}
                    </button>
                  )}
                </div>
              );
            })}
        </div>
      )}
      {failedMsg && !open && (
        <div className="px-4 pb-3">
          <button
            type="button"
            onClick={() => onRetry(failedMsg.message_id)}
            disabled={retryingId === failedMsg.message_id}
            className="inline-flex items-center justify-center rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {retryingId === failedMsg.message_id
              ? "Retrying…"
              : "Retry delivery"}
          </button>
        </div>
      )}
    </li>
  );
}

type ToAnswer = {
  correlation_id: string;
  requester: string;
};

function ErrorState({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
    >
      {message}
    </p>
  );
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
      onClick={() => {
        const done = () => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 2000);
        };
        try {
          const result = navigator.clipboard?.writeText(text);
          if (result && typeof result.then === "function") {
            result.then(done, () => setCopied(false));
          } else {
            done();
          }
        } catch {
          setCopied(false);
        }
      }}
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-ink-3 hover:bg-bg-hover hover:text-ink-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      <svg
        aria-hidden="true"
        width="13"
        height="13"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <rect x="5" y="5" width="9" height="9" rx="2" />
        <path d="M11 5V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v5a2 2 0 0 0 2 2h1" />
      </svg>
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function formatTime(createdAt: string): string | null {
  if (!createdAt) return null;
  const ts = new Date(createdAt).getTime();
  if (Number.isNaN(ts)) return null;
  return new Date(ts).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

type StreamToolCard = {
  tool: string;
  status: "pending" | "completed";
  result?: string;
};

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
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
          className="rounded bg-bg-hover px-1 py-0.5 font-mono text-xs text-ink-2"
        >
          {token.slice(1, -1)}
        </code>
      );
    } else if (token.startsWith("**")) {
      nodes.push(
        <strong key={`${keyPrefix}-b${n++}`} className="font-semibold text-ink">
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
            className="text-accent underline decoration-accent/30 underline-offset-2 hover:text-accent-d"
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
  const segments = splitSegments(closeUnclosedFences(text));
  return (
    <>
      {segments.map((seg, i) =>
        seg.kind === "code" ? (
          <pre
            key={i}
            className="overflow-x-auto rounded-lg border border-line bg-bg-subtle p-3 font-mono text-xs leading-relaxed text-ink"
          >
            <code>{seg.text}</code>
          </pre>
        ) : (
          <span key={i} className="block space-y-2">
            {seg.text.split(/\n{2,}/).map((para, j) => (
              <p key={j} className="text-sm leading-relaxed text-ink">
                {renderInline(para, `${i}-${j}`)}
              </p>
            ))}
          </span>
        )
      )}
    </>
  );
}

function messageBody(msg: ChatMessage): string {
  const payload =
    (msg.envelope as { payload?: Record<string, unknown> })?.payload ?? {};
  for (const key of ["answer", "question", "text", "message", "reason"]) {
    const value = payload[key];
    if (typeof value === "string" && value.trim().length > 0) return value;
  }
  return "";
}

type ThreadTurn = {
  role: string;
  text: string;
  citations: string[];
  created_at: string;
};

const SUGGESTIONS = [
  "Research the latest news on a topic I follow",
  "Explain a complex idea in simple terms",
  "Draft a polite email for me",
  "Help me plan my day",
];

function EmptyThread({ onSuggest }: { onSuggest: (text: string) => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center">
      <p className="text-lg font-medium text-ink">What can I help with?</p>
      <div className="flex max-w-md flex-wrap items-center justify-center gap-2">
        {SUGGESTIONS.map((text) => (
          <button
            key={text}
            type="button"
            onClick={() => onSuggest(text)}
            className="inline-flex items-center justify-center rounded-full border border-line bg-bg px-4 py-2 text-sm text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {text}
          </button>
        ))}
        <Link
          href="/people"
          className="inline-flex items-center justify-center rounded-full border border-line bg-bg px-4 py-2 text-sm text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Pair a peer →
        </Link>
      </div>
    </div>
  );
}

function ListSkeleton() {
  return (
    <div aria-hidden="true" className="space-y-4">
      <div className="ml-auto h-10 w-2/3 animate-pulse rounded-2xl bg-bg-hover" />
      <div className="h-16 w-full animate-pulse rounded-2xl bg-bg-hover" />
      <div className="ml-auto h-10 w-1/2 animate-pulse rounded-2xl bg-bg-hover" />
    </div>
  );
}

function ChatInner() {
  const searchParams = useSearchParams();
  const threadParam = searchParams.get("t") ?? "";
  const [peers, setPeers] = useState<Peer[]>([]);
  const [peerId, setPeerId] = useState("");
  const [agentHandle, setAgentHandle] = useState(() => {
    try {
      return localStorage.getItem("nexus-chat-agent") || "default";
    } catch {
      return "default";
    }
  });
  const [agentOptions, setAgentOptions] = useState<string[]>(["default"]);

  const pickAgent = useCallback((name: string) => {
    setAgentHandle(name);
    try {
      localStorage.setItem("nexus-chat-agent", name);
    } catch {
      /* persistence is best-effort */
    }
  }, []);
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
  const [sessionId, setSessionId] = useState(
    () => threadParam || loadOrCreateSessionId()
  );
  const [live, setLive] = useState<"off" | "on" | "down">("off");
  const [historyTurns, setHistoryTurns] = useState<ThreadTurn[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const streamAccumRef = useRef<{
    tokens: string;
    doneText: string;
    citations: string[];
  }>({ tokens: "", doneText: "", citations: [] });
  const abortRef = useRef<AbortController | null>(null);
  const nearBottomRef = useRef(true);
  const [showLatest, setShowLatest] = useState(false);

  useEffect(() => {
    const onScroll = () => {
      const distance =
        document.documentElement.scrollHeight -
        (window.scrollY + window.innerHeight);
      nearBottomRef.current = distance < 200;
      setShowLatest(distance > 500);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Stick to the bottom while streaming when the user hasn't scrolled up.
  useEffect(() => {
    if (streaming && nearBottomRef.current) {
      window.scrollTo({
        top: document.documentElement.scrollHeight,
        behavior: "auto",
      });
    }
  }, [streamText, streaming]);

  const jumpToLatest = useCallback(() => {
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: "smooth",
    });
  }, []);

  const stopStream = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // The URL thread (?t=) is the source of truth: switching history in
  // the sidebar swaps sessions; bare /chat continues (or mints) the
  // stored one and writes it back into the URL.
  useEffect(() => {
    if (threadParam) {
      setSessionId(threadParam);
      setStreamText(null);
      setToolCards([]);
      setCitations([]);
      setStreamError(null);
    } else {
      const current = loadOrCreateSessionId();
      setSessionId(current);
      window.history.replaceState(null, "", `/chat?t=${encodeURIComponent(current)}`);
    }
  }, [threadParam]);

  useEffect(() => {
    let cancelled = false;
    if (!threadParam) {
      setHistoryTurns([]);
      setHistoryLoading(false);
      return () => {};
    }
    setHistoryLoading(true);
    api<{ turns: ThreadTurn[] }>(
      `/chat/threads/${encodeURIComponent(threadParam)}?agent=${encodeURIComponent(agentHandle)}`
    )
      .then((data) => {
        if (!cancelled) setHistoryTurns(data.turns);
      })
      .catch(() => {
        if (!cancelled) setHistoryTurns([]);
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [threadParam, agentHandle]);
  const [toAnswer, setToAnswer] = useState<ToAnswer[]>([]);
  const [answerText, setAnswerText] = useState<Record<string, string>>({});
  const [answering, setAnswering] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);

  const messagesById = new Map(messages.map((m) => [m.message_id, m]));
  const messagesByCorrelation = new Map(
    messages.map((m) => [m.correlation_id, m])
  );

  const refresh = useCallback(async () => {
    try {
      const [peerData, cardData, msgData, agentData] = await Promise.all([
        api<{ peers: Peer[] }>("/pairing/peers"),
        api<{ approvals: ApprovalCard[] }>("/ask/approvals"),
        api<{ messages: ChatMessage[] }>("/ask/messages"),
        api<{ agents: { id: string }[] }>("/agents").catch(() => ({
          agents: [],
        })),
      ]);
      setPeers(peerData.peers);
      setCards(cardData.approvals);
      setMessages(msgData.messages);
      if (!peerId && peerData.peers.length > 0) {
        setPeerId(peerData.peers[0].agent_id);
      }
      const names = agentData.agents.map((a) => a.id);
      if (names.length > 0) {
        setAgentOptions(names.includes("default") ? names : ["default", ...names]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load chat.");
    }
  }, [peerId]);

  useEffect(() => {
    void refresh().finally(() => setInitialLoading(false));
  }, [refresh]);

  useEffect(() => {
    let closed = false;
    let ws: WebSocket | null = null;
    // Browsers cannot send Authorization on a WS handshake: mint a
    // single-use ticket over authed HTTP first, then connect with it.
    void (async () => {
      let ticket = "";
      try {
        const data = await api<{ ticket: string }>("/ask/live-ticket", {
          method: "POST",
        });
        ticket = data.ticket;
      } catch {
        if (!closed) setLive("down");
        return;
      }
      if (closed) return;
      try {
        ws = new WebSocket(
          `${wsBase()}/ask/live?ticket=${encodeURIComponent(ticket)}`
        );
      } catch {
        if (!closed) setLive("down");
        return;
      }
      ws.onopen = () => {
        if (!closed) setLive("on");
      };
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data as string) as { type?: string };
          if (msg.type === "delivery") void refresh();
        } catch {
        }
      };
      const markDown = () => {
        if (!closed) setLive("down");
      };
      ws.onerror = markDown;
      ws.onclose = markDown;
    })();
    return () => {
      closed = true;
      try {
        ws?.close();
      } catch {
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
      if (isExpiredError(err)) {
        setError("This request expired — ask again");
      } else {
        setError(err instanceof Error ? err.message : "Could not send.");
      }
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
        if (isExpiredError(err)) {
          setError("This request expired — ask again");
        } else {
          setError(err instanceof Error ? err.message : "Decision failed.");
        }
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
    streamAccumRef.current = { tokens: "", doneText: "", citations: [] };
    const controller = new AbortController();
    abortRef.current = controller;
    let failed = false;
    try {
      const res = await fetch(
        `${API_BASE}/chat/stream?message=${encodeURIComponent(prompt)}&session_id=${encodeURIComponent(sessionId)}&agent=${encodeURIComponent(agentHandle)}`,
        {
          headers: { Accept: "text/event-stream", ...authHeaders() },
          signal: controller.signal,
        }
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
              streamAccumRef.current.tokens += evt.text as string;
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
                streamAccumRef.current.doneText = text;
              }
              streamAccumRef.current.citations = evt.citations ?? [];
              setCitations(evt.citations ?? []);
            } else if (evt.type === "error") {
              throw new Error(evt.message ?? "Stream failed.");
            }
          }
        }
      }
    } catch (err) {
      if (controller.signal.aborted) {
        // Stopped by the user: keep the partial text, no error banner.
        failed = false;
      } else {
        failed = true;
        setStreamError(err instanceof Error ? err.message : "Stream failed.");
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setStreaming(false);
      // Fold the finished turn into local history (the server persisted
      // the same rows): the thread reads as one conversation and the
      // stream block clears instead of duplicating.
      if (!failed) {
        const accum = streamAccumRef.current;
        const finalText = accum.doneText || accum.tokens;
        if (finalText) {
          const userText = prompt;
          const assistantCits = accum.citations;
          setHistoryTurns((prev) => [
            ...prev,
            { role: "user", text: userText, citations: [], created_at: "" },
            {
              role: "assistant",
              text: finalText,
              citations: assistantCits,
              created_at: "",
            },
          ]);
          setStreamText(null);
          setToolCards([]);
          setCitations([]);
        }
      }
    }
  }, [streamInput, streaming, sessionId, agentHandle]);

  const selectedPeer = peers.find((p) => p.agent_id === peerId) ?? null;
  const { groups: commGroups, leftovers: leftoverMessages } = useMemo(
    () => groupCompletedExchanges(messages),
    [messages]
  );
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
    historyTurns.length === 0 &&
    !hasStreamTurn &&
    !historyLoading &&
    !status &&
    !error;

  return (
    <div className="flex min-h-screen bg-bg text-ink">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-bg/95 px-4 py-3 backdrop-blur">
          <h1 className="text-base font-semibold tracking-tight text-ink">
            Chat
          </h1>
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <label
              htmlFor="agent-picker"
              className="text-xs font-medium text-ink-2"
            >
              Agent
            </label>
              <select
                id="agent-picker"
                value={agentHandle}
                onChange={(e) => pickAgent(e.target.value)}
              disabled={askBusy || streaming}
              aria-label="Agent"
              className="min-w-0 max-w-full rounded-lg border border-line bg-bg px-2 py-1.5 text-sm text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50 sm:max-w-[10rem]"
            >
              {agentOptions.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          {peers.length === 0 ? (
            <p className="text-xs text-ink-3">No paired peers yet.</p>
          ) : (
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1">
              <label
                htmlFor="peer-picker"
                className="text-xs font-medium text-ink-2"
              >
                Peer
              </label>
              <select
                id="peer-picker"
                value={peerId}
                onChange={(e) => setPeerId(e.target.value)}
                disabled={askBusy || streaming}
                aria-label="Peer"
                className="min-w-0 max-w-full flex-1 rounded-lg border border-line bg-bg px-2 py-1.5 text-sm text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50 sm:max-w-xs"
              >
                {peers.map((peer) => (
                  <option key={peer.agent_id} value={peer.agent_id}>
                    {peer.display_name} ({peer.fingerprint})
                  </option>
                ))}
              </select>
              <p aria-live="polite" className="min-w-0 truncate font-mono text-[11px] text-ink-3">
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
                ? "inline-flex items-center rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800"
                : "inline-flex items-center rounded-full bg-bg-subtle px-2.5 py-0.5 text-xs text-ink-3"
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
                ? "inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700"
                : "inline-flex items-center gap-1.5 rounded-full bg-bg-subtle px-2.5 py-0.5 text-xs text-ink-3"
            }
          >
            <span
              aria-hidden="true"
              className={`h-1.5 w-1.5 rounded-full ${
                live === "on" ? "bg-emerald-500" : "bg-ink-3"
              }`}
            />
            {live === "on" ? "live" : "live off"}
          </span>
        </header>

        <main className="flex flex-1 flex-col px-4 py-6 sm:px-6">
          <section
            aria-labelledby="messages-heading"
            className="mx-auto flex w-full max-w-3xl flex-1 flex-col"
          >
            <h2 id="messages-heading" className="sr-only">
              Conversation
            </h2>
            <LlmStatusLine />
            {threadEmpty ? (
              initialLoading || historyLoading ? (
                <div
                  aria-busy="true"
                  aria-live="polite"
                  className="flex flex-1 flex-col justify-center p-8"
                >
                  <ListSkeleton />
                </div>
              ) : (
                <EmptyThread
                  onSuggest={(text) => {
                    setStreamInput(text);
                    document.getElementById("stream-box")?.focus();
                  }}
                />
              )
            ) : (
              <ul className="flex-1 space-y-6">
            {status && (
              <li>
                <p
                  role="status"
                  aria-live="polite"
                  className="mx-auto max-w-md rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-center text-sm text-emerald-700"
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

            {historyLoading && historyTurns.length === 0 && (
              <li aria-busy="true" aria-live="polite">
                <ListSkeleton />
              </li>
            )}
            {historyTurns.map((turn, i) =>
              turn.role === "user" ? (
                <li key={`history-${i}`} className="flex justify-end">
                  <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-bg-subtle px-4 py-2.5">
                    <p className="text-sm text-ink">{turn.text}</p>
                  </div>
                </li>
              ) : (
                <li key={`history-${i}`} className="flex gap-3">
                  <div
                    aria-hidden="true"
                    className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-semibold text-white"
                  >
                    N
                  </div>
                  <div className="min-w-0 flex-1 space-y-2">
                    <article className="space-y-2">
                      <SafeMarkdown text={turn.text} />
                    </article>
                    <div className="flex items-center gap-2">
                      <CopyButton text={turn.text} label="Copy response" />
                      {formatTime(turn.created_at) && (
                        <time className="text-xs text-ink-3">
                          {formatTime(turn.created_at)}
                        </time>
                      )}
                    </div>
                    {turn.citations.length > 0 && (
                      <ul
                        aria-label="Sources"
                        className="space-y-1 border-t border-line pt-2"
                      >
                        {turn.citations.map((url) => (
                          <li key={url}>
                            <a
                              href={url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="block truncate text-xs text-accent hover:text-accent-d hover:underline"
                            >
                              {url}
                            </a>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </li>
              )
            )}

            {commGroups.map((group) => (
              <CommCard
                key={group.correlationId}
                group={group}
                peers={peers}
                pending={
                  (cardsByCorrelation.get(group.correlationId) ?? []).length > 0
                }
                retryingId={retrying}
                onRetry={(id) => void retryDelivery(id)}
              />
            ))}
            {leftoverMessages.map((msg) => {
              const isRequest = msg.message_type === "request";
              const body = messageBody(msg);
              const inlineCards =
                cardsByCorrelation.get(msg.correlation_id) ?? [];
              return (
                <Fragment key={msg.message_id}>
                  <li
                    className={
                      isRequest ? "flex justify-end" : "flex gap-3"
                    }
                  >
                    {!isRequest && (
                      <div
                        aria-hidden="true"
                        className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-semibold text-white"
                      >
                        N
                      </div>
                    )}
                    <div
                      className={
                        isRequest
                          ? "ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-bg-subtle px-4 py-2.5"
                          : "min-w-0 flex-1"
                      }
                    >
                      <p className="font-mono text-[11px] uppercase tracking-wide text-ink-3">
                        {typeLabel(msg.message_type)} ({deliveryLabel(msg.status)})
                      </p>
                      {body ? (
                        isRequest ? (
                          <p className="text-sm text-ink">{body}</p>
                        ) : (
                          <SafeMarkdown text={body} />
                        )
                      ) : (
                        <p className="text-sm text-ink-2">
                          {peerDisplayName(peers, msg.sender)} →{" "}
                          {peerDisplayName(peers, msg.recipient)}
                        </p>
                      )}
                      {body && (
                        <p className="mt-1 text-xs text-ink-3">
                          {peerDisplayName(peers, msg.sender)} →{" "}
                          {peerDisplayName(peers, msg.recipient)}
                        </p>
                      )}
                      {msg.status === "delivery_failed" && (
                        <button
                          type="button"
                          onClick={() => void retryDelivery(msg.message_id)}
                          disabled={retrying === msg.message_id}
                          aria-label={`Retry delivery of ${msg.message_id}`}
                          className="mt-2 inline-flex items-center justify-center rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/50 disabled:cursor-not-allowed disabled:opacity-50"
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
                        aria-label={`Approval ${card.action} for ${peerDisplayName(peers, card.requester)}`}
                        className="w-full space-y-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3"
                      >
                        <h3 className="text-sm font-semibold text-ink">
                          {card.action} for {peerDisplayName(peers, card.requester)}
                        </h3>
                        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                          <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                            Who
                          </dt>
                          <dd className="text-ink-2">
                            {peerDisplayName(peers, card.requester)}
                          </dd>
                          <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                            What
                          </dt>
                          <dd className="text-ink-2">
                            {card.question} ({card.data_category})
                          </dd>
                          <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                            Why
                          </dt>
                          <dd className="text-ink-2">
                            {card.purpose}
                          </dd>
                          <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                            Expiry
                          </dt>
                          <dd className="font-mono text-xs text-ink-3">
                            {card.expires_at}
                          </dd>
                        </dl>
                        <div className="flex flex-col gap-2 sm:flex-row">
                          <button
                            type="button"
                            onClick={() => decide(card.approval_id, "approve")}
                            disabled={deciding === card.approval_id}
                            className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            {deciding === card.approval_id
                              ? "Working…"
                              : "Approve"}
                          </button>
                          <button
                            type="button"
                            onClick={() => decide(card.approval_id, "reject")}
                            disabled={deciding === card.approval_id}
                            className="inline-flex items-center justify-center rounded-lg border border-red-200 bg-bg px-4 py-2 text-sm font-medium text-red-600 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
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
                  aria-label={`Approval ${card.action} for ${peerDisplayName(peers, card.requester)}`}
                  className="w-full space-y-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3"
                >
                  <h3 className="text-sm font-semibold text-ink">
                    {card.action} for {peerDisplayName(peers, card.requester)}
                  </h3>
                  <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                    <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                      Who
                    </dt>
                    <dd className="text-ink-2">{peerDisplayName(peers, card.requester)}</dd>
                    <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                      What
                    </dt>
                    <dd className="text-ink-2">
                      {card.question} ({card.data_category})
                    </dd>
                    <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                      Why
                    </dt>
                    <dd className="text-ink-2">{card.purpose}</dd>
                    <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                      Expiry
                    </dt>
                    <dd className="font-mono text-xs text-ink-3">
                      {card.expires_at}
                    </dd>
                  </dl>
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <button
                      type="button"
                      onClick={() => decide(card.approval_id, "approve")}
                      disabled={deciding === card.approval_id}
                      className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {deciding === card.approval_id ? "Working…" : "Approve"}
                    </button>
                    <button
                      type="button"
                      onClick={() => decide(card.approval_id, "reject")}
                      disabled={deciding === card.approval_id}
                      className="inline-flex items-center justify-center rounded-lg border border-red-200 bg-bg px-4 py-2 text-sm font-medium text-red-600 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {deciding === card.approval_id ? "Working…" : "Deny"}
                    </button>
                  </div>
                </div>
              </li>
            ))}
            {hasStreamTurn && (
              <li className="flex gap-3">
                <div
                  aria-hidden="true"
                  className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-semibold text-white"
                >
                  N
                </div>
                <div className="min-w-0 flex-1 space-y-2">
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
                              ? "inline-flex max-w-full items-center gap-2 rounded-full bg-amber-50 px-3 py-1 font-mono text-xs text-amber-700"
                              : "inline-flex max-w-full items-center gap-2 rounded-full bg-bg-subtle px-3 py-1 font-mono text-xs text-ink-2"
                          }
                        >
                          <span className="truncate">
                            {card.tool}:{" "}
                            {card.status === "pending"
                              ? "searching…"
                              : "done"}
                          </span>
                          {card.status === "completed" && card.result && (
                            <span className="max-w-[12rem] truncate text-ink-3">
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
                        className="animate-pulse text-sm text-ink-3"
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
                      className="space-y-1 border-t border-line pt-2"
                    >
                      {citations.map((url) => (
                        <li key={url}>
                          <a
                            href={url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="block truncate text-xs text-accent hover:text-accent-d hover:underline"
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
              className="mt-6 space-y-3 rounded-xl border border-line bg-bg-subtle p-4"
            >
              <h2
                id="answer-heading"
                className="text-xs font-semibold uppercase tracking-widest text-ink-3"
              >
                Approved — send an answer
              </h2>
              <ul className="space-y-3">
                {toAnswer.map((item) => (
                  <li
                    key={item.correlation_id}
                    className="space-y-2 rounded-lg border border-line bg-bg p-3"
                  >
                    <p className="font-mono text-[11px] text-ink-3">
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
                      className="w-full resize-none rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
                    />
                    <button
                      type="button"
                      onClick={() => void sendAnswer(item.correlation_id)}
                      disabled={
                        answering === item.correlation_id ||
                        (answerText[item.correlation_id] ?? "").trim().length === 0
                      }
                      className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
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

        <footer className="sticky bottom-0 z-20 border-t border-line bg-bg px-4 pb-4 pt-3 sm:px-6">
          <div className="mx-auto w-full max-w-3xl space-y-3">
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
                    className="w-full resize-none rounded-2xl border border-line bg-bg px-4 py-2.5 text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
                  />
                </div>
                <button
                  type="submit"
                  disabled={
                    (!streaming && askBusy) ||
                    (!streaming && streamInput.trim().length === 0)
                  }
                  aria-label={streaming ? "Stop generation" : "Ask live"}
                  onClick={(e) => {
                    if (streaming) {
                      e.preventDefault();
                      stopStream();
                    }
                  }}
                  className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-accent text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
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
                </button>
              </form>
            </section>

            <section
              aria-labelledby="ask-heading"
              className="rounded-2xl border border-line bg-bg p-2 shadow-sm focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/20"
            >
              <h2 id="ask-heading" className="sr-only">
                Ask
              </h2>
              {peers.length === 0 ? (
                <p className="px-3 py-2 text-sm text-ink-3">
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
                      className="w-full resize-none bg-transparent px-3 py-2 text-sm text-ink placeholder:text-ink-3 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
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
                    className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-accent text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
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
        {showLatest && (
          <button
            type="button"
            onClick={jumpToLatest}
            aria-label="Jump to latest messages"
            className="fixed bottom-40 right-4 z-30 inline-flex h-9 w-9 items-center justify-center rounded-full border border-line bg-bg text-ink-2 shadow-md hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
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
              <line x1="8" y1="2" x2="8" y2="14" />
              <polyline points="3 9 8 14 13 9" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}

export default function ChatPage() {
  return (
    <Suspense
      fallback={<div className="p-8 text-sm text-ink-3">Loading…</div>}
    >
      <ChatInner />
    </Suspense>
  );
}
