"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../components/api";
import {
  EmptyState,
  ErrorRetry,
  LoadingSkeleton,
  PageHeader,
  RelativeTime,
  StatusBadge,
  StatusNote,
  inputClass,
  primaryButtonClass,
  secondaryButtonClass,
  useToast,
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
  const notify = useToast();
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [triggers, setTriggers] = useState<Trigger[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [agent, setAgent] = useState("default");
  const [cron, setCron] = useState("0 8 * * *");
  const [capability, setCapability] = useState("");
  const [trigEvent, setTrigEvent] = useState("");
  const [trigAgent, setTrigAgent] = useState("default");
  const [trigCapability, setTrigCapability] = useState("");
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
      notify("Schedule created", "success");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create schedule.");
    } finally {
      setBusy(null);
    }
  }, [name, agent, cron, capability, busy, load]);

  const createTrigger = useCallback(async () => {
    if (!trigEvent.trim() || !trigCapability.trim() || busy) return;
    setBusy("create-trigger");
    setError(null);
    setStatus(null);
    try {
      await api("/autonomy/triggers", {
        method: "POST",
        body: JSON.stringify({
          agent_id: trigAgent.trim() || "default",
          event_type: trigEvent.trim(),
          target_agent: trigAgent.trim() || "default",
          target_capability: trigCapability.trim(),
        }),
      });
      setTrigEvent("");
      setTrigCapability("");
      setStatus("Trigger created — it will fire on matching events.");
      notify("Trigger created", "success");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create trigger.");
    } finally {
      setBusy(null);
    }
  }, [trigEvent, trigAgent, trigCapability, busy, load]);

  const toggleTrigger = useCallback(
    async (id: string, enabled: boolean) => {
      setBusy(id);
      setError(null);
      try {
        await api(
          `/autonomy/triggers/${encodeURIComponent(id)}/${enabled ? "disable" : "enable"}`,
          { method: "POST" }
        );
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not update trigger.");
      } finally {
        setBusy(null);
      }
    },
    [load]
  );

  const removeTrigger = useCallback(
    async (id: string) => {
      setBusy(id);
      setError(null);
      try {
        await api(`/autonomy/triggers/${encodeURIComponent(id)}`, {
          method: "DELETE",
        });
        setStatus("Trigger deleted.");
        notify("Trigger deleted", "warning");
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not delete trigger.");
      } finally {
        setBusy(null);
      }
    },
    [load]
  );

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
        notify("Schedule deleted", "warning");
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
          <ErrorRetry message={error} onRetry={() => void load()} />
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
                    <p className="mt-0.5 flex flex-wrap items-center gap-x-1 text-xs text-ink-3">
                      {s.last_run_at ? (
                        <>
                          Last run{" "}
                          <RelativeTime
                            iso={s.last_run_at}
                            className="text-xs text-ink-3"
                          />
                        </>
                      ) : (
                        "Never run yet"
                      )}
                      {s.next_run_at && (
                        <>
                          {" "}· next{" "}
                          <RelativeTime
                            iso={s.next_run_at}
                            className="text-xs text-ink-3"
                          />
                        </>
                      )}
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
          New trigger
        </h2>
        <form
          className="mb-4 space-y-2 rounded-xl border border-line bg-bg-subtle p-4"
          onSubmit={(e) => {
            e.preventDefault();
            void createTrigger();
          }}
        >
          <div className="flex flex-col gap-2 sm:flex-row">
            <div className="flex-1">
              <label htmlFor="trig-event" className="block text-xs font-medium text-ink-2">
                Event type
              </label>
              <input
                id="trig-event"
                value={trigEvent}
                onChange={(e) => setTrigEvent(e.target.value)}
                placeholder="e.g. task.completed"
                className={inputClass}
              />
            </div>
            <div className="flex-1">
              <label htmlFor="trig-agent" className="block text-xs font-medium text-ink-2">
                Agent
              </label>
              <input
                id="trig-agent"
                value={trigAgent}
                onChange={(e) => setTrigAgent(e.target.value)}
                placeholder="default"
                className={inputClass}
              />
            </div>
            <div className="flex-1">
              <label htmlFor="trig-cap" className="block text-xs font-medium text-ink-2">
                Capability
              </label>
              <input
                id="trig-cap"
                value={trigCapability}
                onChange={(e) => setTrigCapability(e.target.value)}
                placeholder="agent.capability@v1"
                className={inputClass}
              />
            </div>
          </div>
          <button type="submit" disabled={busy === "create-trigger"} className={primaryButtonClass}>
            {busy === "create-trigger" ? "Creating…" : "Create trigger"}
          </button>
        </form>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-widest text-ink-3">
          Triggers
        </h3>
        {loading ? (
          <LoadingSkeleton label="Loading triggers" />
        ) : triggers.length === 0 ? (
          <EmptyState
            title="No triggers yet."
            hint="Create one above — pick an event, an agent, and what it should run."
          />
        ) : (
          <ul className="space-y-2">
            {triggers.map((t) => (
              <li
                key={t.id}
                className="rounded-xl border border-line bg-bg px-4 py-3"
              >
                <div className="flex items-center justify-between gap-3">
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
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => void toggleTrigger(t.id, t.enabled)}
                    disabled={busy === t.id}
                    aria-label={`${t.enabled ? "Disable" : "Enable"} trigger ${t.id}`}
                    className={secondaryButtonClass}
                  >
                    {busy === t.id
                      ? "Working…"
                      : t.enabled
                        ? "Disable"
                        : "Enable"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void removeTrigger(t.id)}
                    disabled={busy === t.id}
                    aria-label={`Delete trigger ${t.id}`}
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
    </main>
  );
}
