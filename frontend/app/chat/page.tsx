"use client";

import { Fragment, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { closeUnclosedFences, splitSegments } from "./markdown";
import { api, isExpiredError, wsBase } from "../components/api";
import { LlmStatusLine } from "../components/llm-key-settings";
import {
  CodeBlock,
  CopyButton,
  RelativeTime,
  TypingIndicator,
  secondaryButtonClass,
} from "../components/ui";
import { AskPanel } from "./ask-panel";
import { CitationList } from "./citations";
import { StreamPanel } from "./stream-panel";
import { ThreadTurn, useChatStream } from "./use-chat-stream";

const SESSION_KEY = "nexus-session-id";

function mintSessionId(): string {
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Stable React key for a history turn: content hash + index fallback. */
function turnKey(turn: ThreadTurn, index: number): string {
  const s = `${turn.role}|${turn.created_at}|${turn.text}|${turn.citations.join(",")}`;
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
  }
  return `history-${h.toString(36)}-${index}`;
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
    <li className="animate-message-in rounded-xl border border-line bg-bg-raise shadow-card">
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
            <span aria-hidden="true" className="text-success-text">✓</span>{" "}
            {pending ? "Waiting for approval" : "Approved"}
          </li>
          <li>
            <span aria-hidden="true" className="text-success-text">✓</span>{" "}
            {delivery}
          </li>
          {answer && (
            <li>
              <span aria-hidden="true" className="text-success-text">✓</span>{" "}
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
                      className="inline-flex items-center justify-center rounded-lg border border-warning-border bg-warning-bg px-3 py-1.5 text-xs font-medium text-warning-text hover:bg-warning-bg focus:outline-none focus-visible:ring-2 focus-visible:ring-warning/50 disabled:cursor-not-allowed disabled:opacity-50"
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
            className="inline-flex items-center justify-center rounded-lg border border-warning-border bg-warning-bg px-3 py-1.5 text-xs font-medium text-warning-text hover:bg-warning-bg focus:outline-none focus-visible:ring-2 focus-visible:ring-warning/50 disabled:cursor-not-allowed disabled:opacity-50"
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
      className="rounded-lg border border-danger-border bg-danger-bg px-3 py-2 text-sm text-danger-text"
    >
      {message}
    </p>
  );
}


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
          <CodeBlock key={i} lang={seg.lang} text={seg.text} />
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

const SUGGESTIONS = [
  "Research the latest news on a topic I follow",
  "Explain a complex idea in simple terms",
  "Draft a polite email for me",
  "Help me plan my day",
];

function EmptyThread({ onSuggest }: { onSuggest: (text: string) => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-5 p-8 text-center">
      <span
        aria-hidden="true"
        className="flex h-16 w-16 items-center justify-center rounded-3xl bg-gradient-to-br from-accent to-accent-d text-2xl font-bold text-white shadow-glow"
      >
        N
      </span>
      <div className="space-y-1">
        <p className="text-xl font-semibold tracking-tight text-ink">
          What can I help with?
        </p>
        <p className="text-sm text-ink-3">
          Ask live for web answers with sources — or ask a paired peer.
        </p>
      </div>
      <div className="flex max-w-lg flex-wrap items-center justify-center gap-2">
        {SUGGESTIONS.map((text) => (
          <button
            key={text}
            type="button"
            onClick={() => onSuggest(text)}
            className="inline-flex items-center justify-center rounded-full border border-line bg-bg-raise px-4 py-2 text-sm text-ink-2 shadow-sm transition-all hover:-translate-y-px hover:border-accent/60 hover:text-ink hover:shadow-glow focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {text}
          </button>
        ))}
        <Link
          href="/people"
          className="inline-flex items-center justify-center rounded-full border border-line bg-bg-raise px-4 py-2 text-sm text-ink-2 shadow-sm transition-all hover:-translate-y-px hover:border-accent/60 hover:text-ink hover:shadow-glow focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
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
    // ThreadsNav reads the picker through localStorage, which has no
    // same-tab change event: notify it so the sidebar reloads the
    // newly selected agent's threads.
    window.dispatchEvent(new Event("nexus-agent-change"));
  }, []);

  // Deep link from the dashboard's "Chat" button (?agent=<id>): select
  // the named agent once the /agents list has loaded and validated it.
  const agentParam = searchParams.get("agent") ?? "";
  const agentParamAppliedRef = useRef(false);
  useEffect(() => {
    if (!agentParam || agentParamAppliedRef.current) return;
    if (agentOptions.includes(agentParam)) {
      agentParamAppliedRef.current = true;
      pickAgent(agentParam);
    }
  }, [agentParam, agentOptions, pickAgent]);
  const [question, setQuestion] = useState("");
  const [cards, setCards] = useState<ApprovalCard[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [askBusy, setAskBusy] = useState(false);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState(
    () => threadParam || loadOrCreateSessionId()
  );
  const [live, setLive] = useState<"off" | "on" | "down">("off");
  const [relayDown, setRelayDown] = useState(false);
  const [historyTurns, setHistoryTurns] = useState<ThreadTurn[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const nearBottomRef = useRef(true);
  const [showLatest, setShowLatest] = useState(false);

  const foldTurns = useCallback((turns: ThreadTurn[]) => {
    setHistoryTurns((prev) => [...prev, ...turns]);
  }, []);

  const {
    streamInput,
    setStreamInput,
    streamText,
    toolCards,
    citations,
    streaming,
    streamError,
    hasStreamTurn,
    runStream,
    stopStream,
    resetStream,
  } = useChatStream({ sessionId, agentHandle, nearBottomRef, onTurnsFolded: foldTurns });

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

  const jumpToLatest = useCallback(() => {
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: "smooth",
    });
  }, []);

  // The URL thread (?t=) is the source of truth: switching history in
  // the sidebar swaps sessions; bare /chat continues (or mints) the
  // stored one and writes it back into the URL.
  useEffect(() => {
    if (threadParam) {
      setSessionId(threadParam);
      resetStream();
    } else {
      const current = loadOrCreateSessionId();
      setSessionId(current);
      window.history.replaceState(null, "", `/chat?t=${encodeURIComponent(current)}`);
    }
  }, [threadParam, resetStream]);

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

  // Each endpoint resolves independently: one failing call must not
  // wipe out the sections that loaded fine (the dashboard page already
  // isolates this way).
  const refresh = useCallback(async () => {
    const [peersRes, cardsRes, msgRes, agentRes] = await Promise.allSettled([
      api<{ peers: Peer[] }>("/pairing/peers"),
      api<{ approvals: ApprovalCard[] }>("/ask/approvals"),
      api<{ messages: ChatMessage[] }>("/ask/messages"),
      api<{ agents: { id: string }[] }>("/agents"),
    ]);
    let failures = 0;
    if (peersRes.status === "fulfilled") {
      const list = peersRes.value.peers;
      setPeers(list);
      if (list.length > 0) {
        // Functional update: refresh must not depend on peerId, or the
        // mount effect and the WebSocket effect below re-run (double
        // fetch + socket teardown) on every peer change.
        setPeerId((prev) => prev || list[0].agent_id);
      }
    } else {
      failures += 1;
    }
    if (cardsRes.status === "fulfilled") {
      setCards(cardsRes.value.approvals);
    } else {
      failures += 1;
    }
    if (msgRes.status === "fulfilled") {
      setMessages(msgRes.value.messages);
    } else {
      failures += 1;
    }
    // The agents call was already best-effort before (`.catch` → empty),
    // so a rejection here is not a user-facing failure.
    if (agentRes.status === "fulfilled") {
      const names = agentRes.value.agents.map((a) => a.id);
      if (names.length > 0) {
        setAgentOptions(names.includes("default") ? names : ["default", ...names]);
      }
    }
    if (failures > 0) {
      setError("One or more chat sections failed to refresh.");
    }
  }, []);

  useEffect(() => {
    void refresh().finally(() => setInitialLoading(false));
  }, [refresh]);

  useEffect(() => {
    let closed = false;
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    const MAX_DELAY_MS = 30000;

    const scheduleReconnect = () => {
      if (closed) return;
      attempts += 1;
      const delay = Math.min(1000 * 2 ** Math.min(attempts, 5), MAX_DELAY_MS);
      retryTimer = setTimeout(connect, delay);
    };

    // Browsers cannot send Authorization on a WS handshake: mint a
    // single-use ticket over authed HTTP first, then connect with it.
    // Drops reconnect with exponential backoff (1s → 30s cap); the
    // header chip shows "live unavailable" while down.
    const connect = () => {
      if (closed) return;
      void (async () => {
        let ticket = "";
        try {
          const data = await api<{ ticket: string }>("/ask/live-ticket", {
            method: "POST",
          });
          ticket = data.ticket;
        } catch {
          if (!closed) setLive("down");
          scheduleReconnect();
          return;
        }
        if (closed) return;
        let socket: WebSocket;
        try {
          // Ticket travels as the WS subprotocol (Sec-WebSocket-Protocol),
          // not the query string — query strings land in access logs.
          socket = new WebSocket(`${wsBase()}/ask/live`, [ticket]);
        } catch {
          if (!closed) setLive("down");
          scheduleReconnect();
          return;
        }
        ws = socket;
        socket.onopen = () => {
          if (closed) return;
          attempts = 0;
          setLive("on");
          setRelayDown(false);
        };
        socket.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data as string) as { type?: string };
            if (msg.type === "delivery") void refresh();
            else if (msg.type === "relay_down") {
              // Backend lost the relay and closed right after this frame;
              // flag it until a reconnect succeeds.
              if (!closed) setRelayDown(true);
            }
          } catch {
            // Malformed frames are ignored; the socket stays up.
          }
        };
        const markDown = () => {
          // onerror fires just before onclose: only the first counts,
          // else every drop schedules two reconnects.
          if (closed || ws !== socket) return;
          ws = null;
          setLive("down");
          scheduleReconnect();
        };
        socket.onerror = markDown;
        socket.onclose = markDown;
      })();
    };

    connect();
    return () => {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      try {
        ws?.close();
      } catch {
        // best-effort teardown
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
  const threadEmpty =
    messages.length === 0 &&
    cards.length === 0 &&
    historyTurns.length === 0 &&
    !hasStreamTurn &&
    !historyLoading &&
    !status &&
    !error;

  return (
    <div className="flex min-h-screen bg-bg-deep text-ink">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-bg-deep/85 px-4 py-3 backdrop-blur-md">
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
                ? "inline-flex items-center rounded-full bg-warning-bg px-2.5 py-0.5 text-xs font-medium text-warning-text"
                : "inline-flex items-center rounded-full bg-bg-subtle px-2.5 py-0.5 text-xs text-ink-3"
            }
          >
            {pendingCount} pending
          </span>
          <span
            aria-label={
              live === "on"
                ? "Live relay updates on"
                : live === "down"
                  ? "Live updates unavailable — retrying automatically"
                  : "Live relay updates off"
            }
            title={
              live === "on"
                ? "Relay deliveries refresh this view live"
                : live === "down"
                  ? "Live updates unavailable — retrying automatically; approvals refresh on send/decide"
                  : "Live updates unavailable — approvals refresh on send/decide"
            }
            className={
              live === "on"
                ? "inline-flex items-center gap-1.5 rounded-full bg-success-bg px-2.5 py-0.5 text-xs font-medium text-success-text"
                : live === "down"
                  ? "inline-flex items-center gap-1.5 rounded-full bg-warning-bg px-2.5 py-0.5 text-xs font-medium text-warning-text"
                  : "inline-flex items-center gap-1.5 rounded-full bg-bg-subtle px-2.5 py-0.5 text-xs text-ink-3"
            }
          >
            <span
              aria-hidden="true"
              className={`h-1.5 w-1.5 rounded-full ${
                live === "on"
                  ? "bg-success"
                  : live === "down"
                    ? "bg-warning"
                    : "bg-ink-3"
              }`}
            />
            {live === "on" ? "live" : live === "down" ? "live unavailable" : "live off"}
          </span>
          {relayDown && (
            <span
              role="status"
              title="The backend lost its relay connection — relayed deliveries are paused. Reconnects automatically."
              className="inline-flex items-center gap-1.5 rounded-full bg-warning-bg px-2.5 py-0.5 text-xs font-medium text-warning-text"
            >
              <span
                aria-hidden="true"
                className="h-1.5 w-1.5 rounded-full bg-warning"
              />
              relay down
            </span>
          )}
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
                  className="mx-auto max-w-md rounded-lg border border-success-border bg-success-bg px-3 py-2 text-center text-sm text-success-text"
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
                <li key={turnKey(turn, i)} className="flex justify-end">
                  <div className="animate-message-in ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-gradient-to-br from-accent to-accent-d px-4 py-2.5 text-white shadow-glow">
                    <p className="text-sm leading-relaxed text-white">{turn.text}</p>
                  </div>
                </li>
              ) : (
                <li key={turnKey(turn, i)} className="animate-message-in flex gap-3">
                  <div
                    aria-hidden="true"
                    className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent-d text-xs font-bold text-white shadow-glow"
                  >
                    N
                  </div>
                  <div className="min-w-0 flex-1 space-y-2">
                    <article className="space-y-2">
                      <SafeMarkdown text={turn.text} />
                    </article>
                    <div className="flex items-center gap-2">
                      <CopyButton text={turn.text} label="Copy response" />
                      <RelativeTime iso={turn.created_at} />
                    </div>
                    <CitationList urls={turn.citations} />
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
                      isRequest
                        ? "flex justify-end"
                        : "animate-message-in flex gap-3"
                    }
                  >
                    {!isRequest && (
                      <div
                        aria-hidden="true"
                        className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent-d text-xs font-bold text-white shadow-glow"
                      >
                        N
                      </div>
                    )}
                    <div
                      className={
                        isRequest
                          ? "animate-message-in ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-gradient-to-br from-accent to-accent-d px-4 py-2.5 text-white shadow-glow"
                          : "min-w-0 flex-1"
                      }
                    >
                      <p className="font-mono text-[11px] uppercase tracking-wide text-ink-3">
                        {typeLabel(msg.message_type)} ({deliveryLabel(msg.status)})
                      </p>
                      {body ? (
                        isRequest ? (
                          <p className="text-sm leading-relaxed text-white">{body}</p>
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
                          className="mt-2 inline-flex items-center justify-center rounded-lg border border-warning-border bg-warning-bg px-3 py-1.5 text-xs font-medium text-warning-text hover:bg-warning-bg focus:outline-none focus-visible:ring-2 focus-visible:ring-warning/50 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {retrying === msg.message_id
                            ? "Retrying…"
                            : "Retry delivery"}
                        </button>
                      )}
                    </div>
                  </li>
                  {inlineCards.map((card) => (
                    <li key={card.approval_id} className="animate-message-in flex justify-start">
                      <div
                        aria-label={`Approval ${card.action} for ${peerDisplayName(peers, card.requester)}`}
                        className="w-full space-y-3 rounded-xl border border-warning-border bg-warning-bg px-4 py-3"
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
                            className="inline-flex items-center justify-center rounded-lg border border-danger-border bg-bg px-4 py-2 text-sm font-medium text-danger-text hover:bg-danger-bg focus:outline-none focus-visible:ring-2 focus-visible:ring-danger/50 disabled:cursor-not-allowed disabled:opacity-50"
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
              <li key={card.approval_id} className="animate-message-in flex justify-start">
                <div
                  aria-label={`Approval ${card.action} for ${peerDisplayName(peers, card.requester)}`}
                  className="w-full space-y-3 rounded-xl border border-warning-border bg-warning-bg px-4 py-3"
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
                      className="inline-flex items-center justify-center rounded-lg border border-danger-border bg-bg px-4 py-2 text-sm font-medium text-danger-text hover:bg-danger-bg focus:outline-none focus-visible:ring-2 focus-visible:ring-danger/50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {deciding === card.approval_id ? "Working…" : "Deny"}
                    </button>
                  </div>
                </div>
              </li>
            ))}
            {hasStreamTurn && (
              <li className="animate-message-in flex gap-3">
                <div
                  aria-hidden="true"
                  className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent-d text-xs font-bold text-white shadow-glow"
                >
                  N
                </div>
                <div className="min-w-0 flex-1 space-y-2">
                  {streamError && (
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                      <div className="min-w-0 flex-1">
                        <ErrorState message={streamError} />
                      </div>
                      <button
                        type="button"
                        onClick={() => void runStream()}
                        className={secondaryButtonClass}
                      >
                        Retry
                      </button>
                    </div>
                  )}
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
                              ? "inline-flex max-w-full items-center gap-2 rounded-full bg-warning-bg px-3 py-1 font-mono text-xs text-warning-text"
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
                    toolCards.length === 0 && <TypingIndicator />}
                  {streamText !== null && streamText !== "" && (
                    <article
                      aria-live="polite"
                      className={`space-y-2 ${streaming ? "stream-caret" : ""}`}
                    >
                      <SafeMarkdown text={streamText} />
                    </article>
                  )}
                  <CitationList urls={citations} />
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
                    className="space-y-2 rounded-lg border border-line bg-bg-raise p-3"
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

        <footer className="sticky bottom-0 z-20 border-t border-line bg-bg-deep/90 px-4 pb-4 pt-3 backdrop-blur-md sm:px-6">
          <div className="mx-auto w-full max-w-3xl space-y-3">
            <StreamPanel
              streamInput={streamInput}
              setStreamInput={setStreamInput}
              streaming={streaming}
              askBusy={askBusy}
              runStream={() => void runStream()}
              stopStream={stopStream}
            />

            <AskPanel
              peers={peers}
              question={question}
              setQuestion={setQuestion}
              peerId={peerId}
              sendAsk={() => void sendAsk()}
              askBusy={askBusy}
              streaming={streaming}
            />
          </div>
        </footer>
        {showLatest && (
          <button
            type="button"
            onClick={jumpToLatest}
            aria-label="Jump to latest messages"
            className="animate-fade-in fixed bottom-40 right-4 z-30 inline-flex h-10 w-10 items-center justify-center rounded-full border border-accent/40 bg-bg-raise text-accent-ink shadow-pop transition-all hover:border-accent hover:shadow-glow focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
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
