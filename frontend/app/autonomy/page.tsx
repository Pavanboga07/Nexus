"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../components/api";
import {
  EmptyState,
  ErrorAlert,
  LoadingSkeleton,
  PageHeader,
  StatusBadge,
  StatusNote,
  inputClass,
  primaryButtonClass,
  secondaryButtonClass,
} from "../components/ui";

type Schedule = {
  id: string;
  agent_id: string;
  name: string;
  kind: string;
  trigger: string;
  timezone: string;
  target_agent: string;
  capability: string;
  enabled: boolean;
  next_run_at: string;
  last_run_at: string;
};

type Trigger = {
  id: string;
  agent_id: string;
  event_type: string;
  target_agent: string;
  target_capability: string;
  enabled: boolean;
};

export default function AutonomyPage() {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [triggers, setTriggers] = useState<Trigger[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [agent, setAgent] = useState("default");
  const [cron, setCron] = useState("0 8 * * *");
  const [capability, setCapability] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, t] = await Promise.all([
        api<{ schedules: Schedule[] }>("/autonomy/schedules"),
        api<{ triggers: Trigger[] }>("/autonomy/triggers").catch(() => ({
          triggers: [],
        })),
      ]);
      setSchedules(s.schedules);
      setTriggers(t.triggers);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load autonomy.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const createSchedule = useCallback(async () => {
    if (!name.trim() || busy) return;
    setBusy("create");
    setError(null);
    setStatus(null);
    try {
      await api("/autonomy/schedules", {
        method: "POST",
        body: JSON.stringify({
          agent_id: agent.trim() || "default",
          name: name.trim(),
          kind: "cron",
          trigger: cron.trim(),
          timezone: "UTC",
          target_agent: agent.trim() || "default",
          capability: capability.trim(),
        }),
      });
      setName("");
      setCapability("");
      setStatus("Schedule created — it will run automatically.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create schedule.");
    } finally {
      setBusy(null);
    }
  }, [name, agent, cron, capability, busy, load]);

  const toggle = useCallback(
    async (id: string, enabled: boolean) => {
      setBusy(id);
      setError(null);
      try {
        await api(
          `/autonomy/schedules/${encodeURIComponent(id)}/${enabled ? "disable" : "enable"}`,
          { method: "POST" }
        );
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not update schedule.");
      } finally {
        setBusy(null);
      }
    },
    [load]
  );

  const remove = useCallback(
    async (id: string) => {
      setBusy(id);
      setError(null);
      try {
        await api(`/autonomy/schedules/${encodeURIComponent(id)}`, {
          method: "DELETE",
        });
        setStatus("Schedule deleted.");
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not delete schedule.");
      } finally {
        setBusy(null);
      }
    },
    [load]
  );

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-6 sm:px-6">
      <PageHeader
        title="Autonomy"
        subtitle="What your agents do automatically: when they run, what they do, and what happened last."
      />
      {error && (
        <div className="mb-4">
          <ErrorAlert message={error} />
        </div>
      )}
      {status && (
        <div className="mb-4">
          <StatusNote message={status} />
        </div>
      )}

      <section
        aria-labelledby="new-schedule-heading"
        className="mb-6 rounded-xl border border-line bg-bg-subtle p-4"
      >
        <h2
          id="new-schedule-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          New schedule
        </h2>
        <form
          className="mt-3 space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            void createSchedule();
          }}
        >
          <div className="flex flex-col gap-2 sm:flex-row">
            <div className="flex-1">
              <label htmlFor="sched-name" className="block text-xs font-medium text-ink-2">
                Name
              </label>
              <input
                id="sched-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. morning briefing"
                className={inputClass}
              />
            </div>
            <div className="flex-1">
              <label htmlFor="sched-agent" className="block text-xs font-medium text-ink-2">
                Agent
              </label>
              <input
                id="sched-agent"
                value={agent}
                onChange={(e) => setAgent(e.target.value)}
                placeholder="default"
                className={inputClass}
              />
            </div>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <div className="flex-1">
              <label htmlFor="sched-cron" className="block text-xs font-medium text-ink-2">
                Schedule (cron)
              </label>
              <input
                id="sched-cron"
                value={cron}
                onChange={(e) => setCron(e.target.value)}
                placeholder="0 8 * * *"
                className={inputClass}
              />
            </div>
            <div className="flex-1">
              <label htmlFor="sched-cap" className="block text-xs font-medium text-ink-2">
                Capability (optional)
              </label>
              <input
                id="sched-cap"
                value={capability}
                onChange={(e) => setCapability(e.target.value)}
                placeholder="agent.capability@v1"
                className={inputClass}
              />
            </div>
          </div>
          <button type="submit" disabled={busy === "create"} className={primaryButtonClass}>
            {busy === "create" ? "Creating…" : "Create schedule"}
          </button>
        </form>
      </section>

      <section aria-labelledby="schedules-heading">
        <h2
          id="schedules-heading"
          className="mb-2 text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Schedules
        </h2>
        {loading ? (
          <LoadingSkeleton label="Loading schedules" />
        ) : schedules.length === 0 ? (
          <EmptyState
            title="No schedules yet."
            hint="Create one above — pick an agent, a time, and what it should do."
          />
        ) : (
          <ul className="space-y-2">
            {schedules.map((s) => (
              <li
                key={s.id}
                className="rounded-xl border border-line bg-bg px-4 py-3"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-ink">
                      {s.name || s.id}
                    </p>
                    <p className="truncate text-xs text-ink-2">
                      {s.agent_id} · {s.trigger}
                      {s.capability ? ` → ${s.capability}` : ""}
                    </p>
                    <p className="mt-0.5 text-xs text-ink-3">
                      {s.last_run_at
                        ? `Last run ${s.last_run_at}`
                        : "Never run yet"}
                      {s.next_run_at ? ` · next ${s.next_run_at}` : ""}
                    </p>
                  </div>
                  <StatusBadge status={s.enabled ? "enabled" : "disabled"} />
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => void toggle(s.id, s.enabled)}
                    disabled={busy === s.id}
                    aria-label={`${s.enabled ? "Disable" : "Enable"} schedule ${s.id}`}
                    className={secondaryButtonClass}
                  >
                    {busy === s.id
                      ? "Working…"
                      : s.enabled
                        ? "Disable"
                        : "Enable"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void remove(s.id)}
                    disabled={busy === s.id}
                    aria-label={`Delete schedule ${s.id}`}
                    className={secondaryButtonClass}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="triggers-heading" className="mt-6">
        <h2
          id="triggers-heading"
          className="mb-2 text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Triggers
        </h2>
        {loading ? (
          <LoadingSkeleton label="Loading triggers" />
        ) : triggers.length === 0 ? (
          <EmptyState
            title="No triggers yet."
            hint="Triggers run a workflow or capability when something happens."
          />
        ) : (
          <ul className="space-y-2">
            {triggers.map((t) => (
              <li
                key={t.id}
                className="flex items-center justify-between gap-3 rounded-xl border border-line bg-bg px-4 py-3"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-ink">
                    {t.event_type}
                  </p>
                  <p className="truncate text-xs text-ink-2">
                    {t.agent_id}
                    {t.target_capability ? ` → ${t.target_capability}` : ""}
                  </p>
                </div>
                <StatusBadge status={t.enabled ? "enabled" : "disabled"} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
