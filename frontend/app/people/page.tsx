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
    <main className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-50">
          People
        </h1>
        <p className="text-sm text-neutral-400">
          Pair with an invite code. No QR codes in v1.
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
      {error && (
        <p
          role="alert"
          className="rounded-lg border border-red-900 bg-red-950 px-3 py-2 text-sm text-red-200"
        >
          {error}
        </p>
      )}

      <section
        aria-labelledby="create-heading"
        className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-900 p-4 sm:p-5"
      >
        <h2
          id="create-heading"
          className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
        >
          Create invite
        </h2>
        <button
          type="button"
          onClick={createInvite}
          disabled={inviteBusy}
          className="inline-flex items-center justify-center rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {inviteBusy ? "Creating…" : "Generate invite code"}
        </button>
        {inviteCode && (
          <p className="text-sm text-neutral-400">
            Your code (expires in 15 minutes, single use):{" "}
            <code className="break-all rounded border border-neutral-800 bg-neutral-950 px-1.5 py-0.5 font-mono text-xs text-emerald-200">
              {inviteCode}
            </code>
          </p>
        )}
      </section>

      <section
        aria-labelledby="claim-heading"
        className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-900 p-4 sm:p-5"
      >
        <h2
          id="claim-heading"
          className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
        >
          Claim invite
        </h2>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1 space-y-1">
            <label
              htmlFor="claim-input"
              className="block text-xs font-medium uppercase tracking-wide text-neutral-500"
            >
              Six-word code
            </label>
            <input
              id="claim-input"
              type="text"
              value={claimInput}
              onChange={(e) => setClaimInput(e.target.value)}
              placeholder="word-word-word-word-word-word"
              autoComplete="off"
              className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm text-neutral-200 placeholder:text-neutral-600 focus:border-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-600/40"
            />
          </div>
          <button
            type="button"
            onClick={claimInvite}
            disabled={claimInput.trim().length === 0}
            className="inline-flex shrink-0 items-center justify-center rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Look up
          </button>
        </div>
        {trust && (
          <div className="space-y-2 rounded-lg border border-amber-900 bg-amber-950/30 p-4">
            <h3 className="text-xs font-semibold uppercase tracking-widest text-amber-200/80">
              Trust check
            </h3>
            <p className="text-sm text-neutral-300">
              Fingerprint (from the verified card — call the inviter and
              compare):{" "}
              <code className="block max-w-full truncate rounded border border-neutral-800 bg-neutral-950 px-1.5 py-0.5 font-mono text-xs text-emerald-200">
                {trust.fingerprint}
              </code>
            </p>
            <p className="text-sm text-neutral-300">
              {trust.display_name} ({trust.agent_id})
            </p>
            <button
              type="button"
              onClick={approvePeer}
              className="inline-flex items-center justify-center rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Approve and pin
            </button>
          </div>
        )}
      </section>

      <section
        aria-labelledby="peers-heading"
        className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-900 p-4 sm:p-5"
      >
        <h2
          id="peers-heading"
          className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
        >
          Paired peers
        </h2>
        {peers.length === 0 ? (
          <p className="text-sm text-neutral-500">No peers yet.</p>
        ) : (
          <ul className="space-y-2">
            {peers.map((peer) => (
              <li
                key={peer.agent_id}
                className="flex flex-col gap-2 rounded-lg border border-neutral-800 bg-neutral-950 p-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-neutral-100">
                      {peer.display_name}
                    </span>
                    <span className="inline-flex items-center rounded-full border border-emerald-900 bg-emerald-950 px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide text-emerald-300">
                      paired
                    </span>
                  </div>
                  <code className="block max-w-full truncate font-mono text-xs text-neutral-400">
                    {peer.fingerprint}
                  </code>
                </div>
                <button
                  type="button"
                  onClick={() => unpair(peer.agent_id)}
                  className="inline-flex shrink-0 items-center justify-center rounded-md border border-red-800 bg-red-900/60 px-3 py-1.5 text-sm font-medium text-red-100 hover:bg-red-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                >
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
