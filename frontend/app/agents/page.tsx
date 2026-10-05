"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../components/api";

type Agent = {
  id: string;
  owner_id: string;
  agent_id: string;
  display_name: string;
  status: string;
  autonomy?: string;
  fingerprint: string;
  key_version: number;
};

const AUTONOMY_LEVELS = ["disabled", "approval_required", "limited", "enabled"];

type Capability = {
  id: string;
  version: number;
  description: string;
  tool: string | null;
  status: string;
};

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [caps, setCaps] = useState<Record<string, Capability[]>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api<{ agents: Agent[] }>("/agents");
      setAgents(data.agents);
      const entries = await Promise.all(
        data.agents.map(async (agent) => {
          try {
            const list = await api<{ capabilities: Capability[] }>(
              `/agents/${encodeURIComponent(agent.id)}/capabilities`
            );
            return [agent.id, list.capabilities] as const;
          } catch {
            return [agent.id, []] as const;
          }
        })
      );
      setCaps(Object.fromEntries(entries));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load agents.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const mutate = useCallback(
    async (label: string, path: string, init?: RequestInit) => {
      setBusy(label);
      setError(null);
      setStatus(null);
      try {
        await api(path, { method: "POST", ...(init ?? {}) });
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : `${label} failed.`);
      } finally {
        setBusy(null);
      }
    },
    [load]
  );

  const create = useCallback(async () => {
    if (name.trim().length === 0) return;
    setBusy("create");
    setError(null);
    try {
      await api("/agents", {
        method: "POST",
        body: JSON.stringify({ name: name.trim() }),
      });
      setName("");
      setStatus("Agent created with its own keypair.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Create failed.");
    } finally {
      setBusy(null);
    }
  }, [name, load]);

  return (
    <main className="mx-auto w-full max-w-3xl space-y-8 px-4 py-8 sm:px-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight text-ink">
          Agents
        </h1>
        <p className="text-sm text-ink-2">
          Independently identifiable agents under this owner. Each holds
          its own keys, memory partition, policies, and capabilities.
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
        aria-labelledby="create-agent-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="create-agent-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Create agent
        </h2>
        <div className="flex flex-col gap-2 sm:flex-row">
          <label htmlFor="agent-name" className="sr-only">
            Agent name
          </label>
          <input
            id="agent-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="research"
            autoComplete="off"
            spellCheck={false}
            disabled={busy !== null}
            className="w-full flex-1 rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
          />
          <button
            type="button"
            onClick={create}
            disabled={busy !== null || name.trim().length === 0}
            className="inline-flex shrink-0 items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy === "create" ? "Creating…" : "Create"}
          </button>
        </div>
      </section>

      <section
        aria-labelledby="agents-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="agents-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          My agents
        </h2>
        {loading ? (
          <p aria-live="polite" className="text-sm text-ink-3">
            Loading…
          </p>
        ) : agents.length === 0 ? (
          <p className="text-sm text-ink-3">No agents yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {agents.map((agent) => (
              <li key={agent.id} className="space-y-2 py-3 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-ink">
                    {agent.display_name || agent.id}
                  </span>
                  <span
                    className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ${
                      agent.status === "active"
                        ? "bg-emerald-50 text-emerald-700"
                        : agent.status === "disabled"
                          ? "bg-bg-hover text-ink-2"
                          : "bg-red-50 text-red-600"
                    }`}
                  >
                    {agent.status}
                  </span>
                  <span className="text-xs text-ink-3">
                    {caps[agent.id]?.length ?? 0} capabilities · key v
                    {agent.key_version} · autonomy{" "}
                    {agent.autonomy || "limited"}
                  </span>
                </div>
                <code className="block max-w-full truncate font-mono text-xs text-ink-3">
                  {agent.agent_id} · {agent.fingerprint}
                </code>
                {(caps[agent.id] ?? []).length > 0 && (
                  <ul className="flex flex-wrap gap-1.5">
                    {(caps[agent.id] ?? []).map((cap) => (
                      <li
                        key={cap.id}
                        title={cap.description}
                        className="inline-flex items-center rounded-full bg-bg-hover px-2.5 py-0.5 font-mono text-xs text-ink-2"
                      >
                        {cap.id}
                      </li>
                    ))}
                  </ul>
                )}
                <div className="flex flex-wrap gap-2">
                  {agent.status === "active" ? (
                    <button
                      type="button"
                      onClick={() =>
                        void mutate("disable", `/agents/${agent.id}/disable`)
                      }
                      disabled={busy !== null}
                      className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Disable
                    </button>
                  ) : agent.status === "disabled" ? (
                    <button
                      type="button"
                      onClick={() =>
                        void mutate("enable", `/agents/${agent.id}/enable`)
                      }
                      disabled={busy !== null}
                      className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Enable
                    </button>
                  ) : null}
                  <button
                    type="button"
                    onClick={() =>
                      void mutate("rotate", `/agents/${agent.id}/rotate`)
                    }
                    disabled={busy !== null}
                    className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Rotate key
                  </button>
                  <label className="inline-flex items-center gap-1 text-xs text-ink-2">
                    Autonomy
                    <select
                      aria-label={`Autonomy for ${agent.id}`}
                      value={agent.autonomy || "limited"}
                      disabled={busy !== null}
                      onChange={(e) => {
                        const level = e.target.value;
                        setBusy(`autonomy-${agent.id}`);
                        setError(null);
                        api(`/agents/${agent.id}/autonomy`, {
                          method: "POST",
                          body: JSON.stringify({ level }),
                        })
                          .then(() => load())
                          .catch((err: unknown) =>
                            setError(
                              err instanceof Error
                                ? err.message
                                : "Autonomy change failed."
                            )
                          )
                          .finally(() => setBusy(null));
                      }}
                      className="rounded-lg border border-line bg-bg px-2 py-1.5 text-sm text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {AUTONOMY_LEVELS.map((level) => (
                        <option key={level} value={level}>
                          {level.replace(/_/g, " ")}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
