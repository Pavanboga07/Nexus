"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../components/api";

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
  trust_state?: string;
  last_seen_at?: string;
};

type DirectoryCard = {
  agent_id: string;
  display_name: string;
  endpoint: string;
  capabilities: unknown[];
};

export default function PeoplePage() {
  const [inviteCode, setInviteCode] = useState<string | null>(null);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [claimInput, setClaimInput] = useState("");
  const [claimBusy, setClaimBusy] = useState(false);
  const [trust, setTrust] = useState<Trust | null>(null);
  const [peers, setPeers] = useState<Peer[]>([]);
  const [peersLoading, setPeersLoading] = useState(true);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [trustBusy, setTrustBusy] = useState<string | null>(null);
  const [lookupInput, setLookupInput] = useState("");
  const [lookupBusy, setLookupBusy] = useState(false);
  const [lookupCard, setLookupCard] = useState<DirectoryCard | null>(null);
  const [lookupError, setLookupError] = useState<string | null>(null);

  const reloadPeers = useCallback(async () => {
    const data = await api<{ peers: Peer[] }>("/pairing/peers");
    setPeers(data.peers);
  }, []);

  const changeTrust = useCallback(
    async (agentId: string, state: string) => {
      setTrustBusy(agentId);
      setError(null);
      try {
        await api(`/pairing/peers/${encodeURIComponent(agentId)}/trust`, {
          method: "POST",
          body: JSON.stringify({ state }),
        });
        setStatus(
          state === "TRUSTED"
            ? "Peer trust restored."
            : state === "SUSPENDED"
              ? "Peer suspended — no new exchanges until resumed."
              : "Peer trust revoked — their grants no longer verify here."
        );
        await reloadPeers();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Trust change failed.");
      } finally {
        setTrustBusy(null);
      }
    },
    [reloadPeers]
  );

  const lookup = useCallback(async () => {
    const id = lookupInput.trim();
    if (!id || lookupBusy) return;
    setLookupBusy(true);
    setLookupError(null);
    setLookupCard(null);
    try {
      const data = await api<{ card: DirectoryCard }>(
        `/pairing/directory/lookup?agent_id=${encodeURIComponent(id)}`
      );
      setLookupCard(data.card);
    } catch (err) {
      setLookupError(
        err instanceof Error ? err.message : "Directory lookup failed."
      );
    } finally {
      setLookupBusy(false);
    }
  }, [lookupInput, lookupBusy]);

  useEffect(() => {
    let cancelled = false;
    api<{ peers: Peer[] }>("/pairing/peers")
      .then((data) => {
        if (!cancelled) setPeers(data.peers);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load peers.");
      })
      .finally(() => {
        if (!cancelled) setPeersLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const createInvite = useCallback(async () => {
    setInviteBusy(true);
    setError(null);
    setStatus(null);
    try {
      const data = await api<{ code: string }>("/pairing/invites", {
        method: "POST",
        body: JSON.stringify({}),
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
    if (claimBusy || claimInput.trim().length === 0) return;
    setClaimBusy(true);
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
    } finally {
      setClaimBusy(false);
    }
  }, [claimInput, claimBusy]);

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
    <main className="mx-auto w-full max-w-3xl space-y-8 px-4 py-8 sm:px-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight text-ink">
          People
        </h1>
        <p className="text-sm text-ink-2">
          Pair with an invite code. No QR codes in v1.
        </p>
      </div>

      {status && (
        <p
          role="status"
          aria-live="polite"
          className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700"
        >
          {status}
        </p>
      )}
      {error && (
        <p
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
        >
          {error}
        </p>
      )}

      <section
        aria-labelledby="create-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="create-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Create invite
        </h2>
        <button
          type="button"
          onClick={createInvite}
          disabled={inviteBusy}
          className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {inviteBusy ? "Creating…" : "Generate invite code"}
        </button>
        {inviteCode && (
          <p className="text-sm text-ink-2">
            Your code (expires in 15 minutes, single use):{" "}
            <code className="break-all rounded border border-line bg-bg px-1.5 py-0.5 font-mono text-xs text-ink">
              {inviteCode}
            </code>
          </p>
        )}
      </section>

      <section
        aria-labelledby="claim-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="claim-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Claim invite
        </h2>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1 space-y-1">
            <label
              htmlFor="claim-input"
              className="block text-xs font-medium text-ink-2"
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
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20"
            />
          </div>
          <button
            type="button"
            onClick={claimInvite}
            disabled={claimBusy || claimInput.trim().length === 0}
            aria-live="polite"
            className="inline-flex shrink-0 items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {claimBusy ? "Looking up…" : "Look up"}
          </button>
        </div>
        {trust && (
          <div className="space-y-2 rounded-xl border border-amber-200 bg-amber-50 p-4">
            <h3 className="text-xs font-semibold uppercase tracking-widest text-amber-700">
              Trust check
            </h3>
            <p className="text-sm text-ink-2">
              Fingerprint (from the verified card — call the inviter and
              compare):{" "}
              <code className="block max-w-full truncate rounded border border-line bg-bg px-1.5 py-0.5 font-mono text-xs text-ink">
                {trust.fingerprint}
              </code>
            </p>
            <p className="text-sm text-ink-2">
              {trust.display_name} ({trust.agent_id})
            </p>
            <button
              type="button"
              onClick={approvePeer}
              className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Approve and pin
            </button>
          </div>
        )}
      </section>

      <section
        aria-labelledby="peers-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="peers-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Paired peers
        </h2>
        {peersLoading ? (
          <p aria-live="polite" className="text-sm text-ink-3">
            Loading peers…
          </p>
        ) : peers.length === 0 ? (
          <p className="text-sm text-ink-3">No peers yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {peers.map((peer) => {
              const trust = peer.trust_state || "TRUSTED";
              return (
                <li
                  key={peer.agent_id}
                  className="flex flex-col gap-3 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium text-ink">
                        {peer.display_name}
                      </span>
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ${
                          trust === "TRUSTED"
                            ? "bg-emerald-50 text-emerald-700"
                            : trust === "SUSPENDED"
                              ? "bg-amber-50 text-amber-700"
                              : "bg-red-50 text-red-600"
                        }`}
                      >
                        {trust.toLowerCase()}
                      </span>
                    </div>
                    <code className="block max-w-full truncate font-mono text-xs text-ink-3">
                      {peer.fingerprint}
                    </code>
                    {peer.last_seen_at && (
                      <p className="text-[11px] text-ink-3">
                        Last seen {peer.last_seen_at}
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 flex-wrap gap-2">
                    {trust === "TRUSTED" ? (
                      <button
                        type="button"
                        onClick={() => void changeTrust(peer.agent_id, "SUSPENDED")}
                        disabled={trustBusy === peer.agent_id}
                        className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Suspend
                      </button>
                    ) : trust === "SUSPENDED" ? (
                      <button
                        type="button"
                        onClick={() => void changeTrust(peer.agent_id, "TRUSTED")}
                        disabled={trustBusy === peer.agent_id}
                        className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Resume
                      </button>
                    ) : null}
                    {trust !== "REVOKED" && (
                      <button
                        type="button"
                        onClick={() => void changeTrust(peer.agent_id, "REVOKED")}
                        disabled={trustBusy === peer.agent_id}
                        className="inline-flex items-center justify-center rounded-lg border border-red-200 bg-bg px-3 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Revoke
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => unpair(peer.agent_id)}
                      className="inline-flex items-center justify-center rounded-lg border border-red-200 bg-bg px-3 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Unpair
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section
        aria-labelledby="directory-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="directory-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Network directory
        </h2>
        <p className="text-sm text-ink-2">
          Look up any agent on the shared gateway. Cards shown here are
          cryptographically verified — but verification is not trust.
          Pairing still needs the invite ceremony.
        </p>
        <div className="flex flex-col gap-2 sm:flex-row">
          <label htmlFor="directory-lookup" className="sr-only">
            Agent ID to look up
          </label>
          <input
            id="directory-lookup"
            type="text"
            value={lookupInput}
            onChange={(e) => setLookupInput(e.target.value)}
            placeholder="nexus:ed25519:…"
            autoComplete="off"
            spellCheck={false}
            disabled={lookupBusy}
            className="w-full flex-1 rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
          />
          <button
            type="button"
            onClick={lookup}
            disabled={lookupBusy || lookupInput.trim().length === 0}
            className="inline-flex shrink-0 items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {lookupBusy ? "Looking up…" : "Look up"}
          </button>
        </div>
        {lookupError && (
          <p
            role="alert"
            className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          >
            {lookupError}
          </p>
        )}
        {lookupCard && (
          <div className="space-y-1 rounded-lg border border-line bg-bg p-3">
            <p className="text-sm font-medium text-ink">
              {lookupCard.display_name}
            </p>
            <code className="block max-w-full truncate font-mono text-xs text-ink-3">
              {lookupCard.agent_id}
            </code>
            <p className="text-xs text-ink-2">
              {(lookupCard.capabilities ?? []).length} advertised
              capabilities · {lookupCard.endpoint}
            </p>
          </div>
        )}
      </section>
    </main>
  );
}
