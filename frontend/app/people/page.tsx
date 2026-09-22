"use client";

import { useCallback, useState } from "react";

const API_BASE =
  process.env.NEXT_PUBLIC_NEXUS_API ?? "http://127.0.0.1:8001";

type Trust = {
  agent_id: string;
  display_name: string;
  fingerprint: string;
  card: Record<string, unknown>;
};

type Peer = {
  agent_id: string;
  display_name: string;
  fingerprint: string;
  paired_at: string;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = (await res.json().catch(() => ({}))) as T & {
    detail?: string;
    code?: string;
  };
  if (!res.ok) {
    throw new Error(body.detail ?? `request failed (${res.status})`);
  }
  return body;
}

export default function PeoplePage() {
  const [inviteCode, setInviteCode] = useState<string | null>(null);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [claimInput, setClaimInput] = useState("");
  const [trust, setTrust] = useState<Trust | null>(null);
  const [peers, setPeers] = useState<Peer[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const createInvite = useCallback(async () => {
    setInviteBusy(true);
    setError(null);
    setStatus(null);
    try {
      const data = await api<{ code: string }>("/pairing/invites", {
        method: "POST",
        body: JSON.stringify({ card: {} }),
      });
      setInviteCode(data.code);
      setStatus("Invite created. Share the six words out of band.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create invite.");
    } finally {
      setInviteBusy(false);
    }
  }, []);

  const claimInvite = useCallback(async () => {
    setError(null);
    setStatus(null);
    setTrust(null);
    try {
      const data = await api<Trust>("/pairing/claim", {
        method: "POST",
        body: JSON.stringify({ code: claimInput }),
      });
      setTrust(data);
      setStatus("Card verified. Compare the fingerprint, then approve.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Claim failed.");
    }
  }, [claimInput]);

  const approvePeer = useCallback(async () => {
    if (!trust) return;
    setError(null);
    try {
      await api("/pairing/approve", {
        method: "POST",
        body: JSON.stringify({ card: trust.card }),
      });
      setTrust(null);
      setClaimInput("");
      setStatus("Peer pinned.");
      const data = await api<{ peers: Peer[] }>("/pairing/peers");
      setPeers(data.peers);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approve failed.");
    }
  }, [trust]);

  const unpair = useCallback(async (agentId: string) => {
    setError(null);
    try {
      await api(`/pairing/peers/${encodeURIComponent(agentId)}`, {
        method: "DELETE",
      });
      setPeers((prev) => prev.filter((p) => p.agent_id !== agentId));
      setStatus("Peer removed locally.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unpair failed.");
    }
  }, []);

  return (
    <main>
      <h1>People</h1>
      <p>Pair with an invite code. No QR codes in v1.</p>

      {status && (
        <p role="status" aria-live="polite">
          {status}
        </p>
      )}
      {error && (
        <p role="alert">
          {error}
        </p>
      )}

      <section aria-labelledby="create-heading">
        <h2 id="create-heading">Create invite</h2>
        <button type="button" onClick={createInvite} disabled={inviteBusy}>
          {inviteBusy ? "Creating…" : "Generate invite code"}
        </button>
        {inviteCode && (
          <p>
            Your code (expires in 15 minutes, single use):{" "}
            <code>{inviteCode}</code>
          </p>
        )}
      </section>

      <section aria-labelledby="claim-heading">
        <h2 id="claim-heading">Claim invite</h2>
        <label htmlFor="claim-input">Six-word code</label>
        <input
          id="claim-input"
          type="text"
          value={claimInput}
          onChange={(e) => setClaimInput(e.target.value)}
          placeholder="word-word-word-word-word-word"
          autoComplete="off"
        />
        <button
          type="button"
          onClick={claimInvite}
          disabled={claimInput.trim().length === 0}
        >
          Look up
        </button>
        {trust && (
          <div>
            <h3>Trust check</h3>
            <p>
              Fingerprint (from the verified card — call the inviter and
              compare): <code>{trust.fingerprint}</code>
            </p>
            <p>
              {trust.display_name} ({trust.agent_id})
            </p>
            <button type="button" onClick={approvePeer}>
              Approve and pin
            </button>
          </div>
        )}
      </section>

      <section aria-labelledby="peers-heading">
        <h2 id="peers-heading">Paired peers</h2>
        {peers.length === 0 ? (
          <p>No peers yet.</p>
        ) : (
          <ul>
            {peers.map((peer) => (
              <li key={peer.agent_id}>
                {peer.display_name} — <code>{peer.fingerprint}</code>{" "}
                <button type="button" onClick={() => unpair(peer.agent_id)}>
                  Unpair
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
