"use client";

import { useCallback, useEffect, useState } from "react";

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
