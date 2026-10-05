"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../components/api";

type Task = {
  task_id: string;
  requesting_agent_id: string;
  target_agent_id: string;
  capability_id: string;
  purpose: string;
  status: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  error_code: string;
  error_detail: string;
  correlation_id: string;
  parent_task_id: string;
  retry_count: number;
  delegation_id: string;
  approval_id: string;
  origin?: string;
  depth?: number;
  root_task_id?: string;
  trigger_id?: string;
  remote_task_id?: string;
  children?: Task[];
  events?: { event: string; detail: string; created_at: string }[];
};

type Workflow = {
  id: string;
  name: string;
  status: string;
  steps: {
    step_id: string;
    step_key: string;
    target_agent: string;
    capability: string;
    status: string;
    task_id: string;
    error: string;
  }[];
};

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

export default function TasksPage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [target, setTarget] = useState("");
  const [capability, setCapability] = useState("");
  const [question, setQuestion] = useState("");
  const [agentOptions, setAgentOptions] = useState<string[]>([]);
  const [capOptions, setCapOptions] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    api<{ agents: { id: string }[] }>("/agents")
      .then((data) => {
        if (cancelled) return;
        const names = data.agents.map((a) => a.id);
        setAgentOptions(names);
        if (!target && names.length > 0) setTarget(names[0]);
      })
      .catch(() => {
        if (!cancelled) setAgentOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!target) {
      setCapOptions([]);
      setCapability("");
      return;
    }
    setCapability("");
    let cancelled = false;
    api<{ capabilities: { id: string }[] }>(
      `/agents/${encodeURIComponent(target)}/capabilities`
    )
      .then((data) => {
        if (!cancelled) setCapOptions(data.capabilities.map((c) => c.id));
      })
      .catch(() => {
        if (!cancelled) setCapOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [target]);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<Task | null>(null);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [selectedFlow, setSelectedFlow] = useState<Workflow | null>(null);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [schedName, setSchedName] = useState("");
  const [schedAgent, setSchedAgent] = useState("default");
  const [schedTrigger, setSchedTrigger] = useState("0 8 * * *");
  const [schedCap, setSchedCap] = useState("");

  const loadFlows = useCallback(async () => {
    try {
      const data = await api<{ workflows: Workflow[] }>("/workflows");
      setWorkflows(data.workflows);
    } catch {
      /* workflows section is best-effort */
    }
  }, []);

  const loadSchedules = useCallback(async () => {
    try {
      const data = await api<{ schedules: Schedule[] }>(
        "/autonomy/schedules"
      );
      setSchedules(data.schedules);
    } catch {
      /* schedules section is best-effort */
    }
  }, []);

  useEffect(() => {
    void loadFlows();
    void loadSchedules();
  }, [loadFlows, loadSchedules]);

  const openFlow = useCallback(async (flowId: string) => {
    try {
      setSelectedFlow(
        await api<Workflow>(`/workflows/${encodeURIComponent(flowId)}`)
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open flow.");
    }
  }, []);

  const flowAct = useCallback(
    async (verb: "pause" | "cancel", flowId: string) => {
      setBusy(true);
      setError(null);
      try {
        const updated = await api<Workflow>(
          `/workflows/${encodeURIComponent(flowId)}/${verb}`,
          { method: "POST" }
        );
        setSelectedFlow(updated);
        await loadFlows();
      } catch (err) {
        setError(err instanceof Error ? err.message : `${verb} failed.`);
      } finally {
        setBusy(false);
      }
    },
    [loadFlows]
  );

  const createSchedule = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await api("/autonomy/schedules", {
        method: "POST",
        body: JSON.stringify({
          agent_id: schedAgent.trim() || "default",
          name: schedName.trim(),
          kind: "cron",
          trigger: schedTrigger.trim(),
          target_agent: schedAgent.trim() || "default",
          capability: schedCap.trim(),
        }),
      });
      setSchedName("");
      setSchedCap("");
      await loadSchedules();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Schedule failed.");
    } finally {
      setBusy(false);
    }
  }, [schedAgent, schedName, schedTrigger, schedCap, loadSchedules]);

  const toggleSchedule = useCallback(
    async (schedule: Schedule) => {
      setBusy(true);
      try {
        await api(
          `/autonomy/schedules/${encodeURIComponent(schedule.id)}/${
            schedule.enabled ? "disable" : "enable"
          }`,
          { method: "POST" }
        );
        await loadSchedules();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Toggle failed.");
      } finally {
        setBusy(false);
      }
    },
    [loadSchedules]
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api<{ tasks: Task[] }>("/tasks?limit=50");
      setTasks(data.tasks);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load tasks.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const open = useCallback(async (taskId: string) => {
    try {
      setSelected(await api<Task>(`/tasks/${encodeURIComponent(taskId)}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open task.");
    }
  }, []);

  const create = useCallback(async () => {
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      const created = await api<Task>("/tasks", {
        method: "POST",
        body: JSON.stringify({
          target_agent: target.trim(),
          capability: capability.trim(),
          input: question.trim() ? { question: question.trim() } : {},
        }),
      });
      setTarget("");
      setCapability("");
      setQuestion("");
      setStatus(`Task ${created.status.toLowerCase()}: ${created.task_id}`);
      await load();
      await open(created.task_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Create failed.");
    } finally {
      setBusy(false);
    }
  }, [target, capability, question, load, open]);

  const act = useCallback(
    async (verb: "cancel" | "retry" | "advance", taskId: string) => {
      setBusy(true);
      setError(null);
      try {
        const updated = await api<Task>(
          `/tasks/${encodeURIComponent(taskId)}/${verb}`,
          { method: "POST" }
        );
        await load();
        setSelected(updated);
      } catch (err) {
        setError(err instanceof Error ? err.message : `${verb} failed.`);
      } finally {
        setBusy(false);
      }
    },
    [load]
  );

  const pill = (state: string) => {
    const cls =
      state === "COMPLETED"
        ? "bg-emerald-50 text-emerald-700"
        : state === "FAILED"
          ? "bg-red-50 text-red-600"
          : state === "WAITING_APPROVAL"
            ? "bg-amber-50 text-amber-700"
            : "bg-bg-hover text-ink-2";
    return (
      <span
        className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ${cls}`}
      >
        {state.replace(/_/g, " ")}
      </span>
    );
  };

  return (
    <main className="mx-auto w-full max-w-3xl space-y-8 px-4 py-8 sm:px-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight text-ink">
          Tasks
        </h1>
        <p className="text-sm text-ink-2">
          Orchestrated work across agents — routed, authorized, tracked.
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
        aria-labelledby="new-task-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="new-task-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          New task
        </h2>
        <div className="grid gap-2 sm:grid-cols-2">
          <div className="space-y-1">
            <label
              htmlFor="task-target"
              className="block text-xs font-medium text-ink-2"
            >
              Agent
            </label>
            <select
              id="task-target"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              disabled={busy || agentOptions.length === 0}
              aria-label="Agent"
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {agentOptions.length === 0 ? (
                <option value="">No agents yet — create one first</option>
              ) : (
                agentOptions.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))
              )}
            </select>
          </div>
          <div className="space-y-1">
            <label
              htmlFor="task-capability"
              className="block text-xs font-medium text-ink-2"
            >
              Capability (optional)
            </label>
            <select
              id="task-capability"
              value={capability}
              onChange={(e) => setCapability(e.target.value)}
              disabled={busy}
              aria-label="Capability"
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <option value="">None — let the agent decide</option>
              {capOptions.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="space-y-1">
          <label
            htmlFor="task-question"
            className="block text-xs font-medium text-ink-2"
          >
              Task — what should the agent do?
          </label>
          <input
            id="task-question"
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="What should the agent do?"
            disabled={busy}
            className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
          />
        </div>
        <button
          type="button"
          onClick={create}
          disabled={busy}
          className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Running…" : "Run task"}
        </button>
      </section>

      <section
        aria-labelledby="tasks-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="tasks-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Recent tasks
        </h2>
        {loading ? (
          <p aria-live="polite" className="text-sm text-ink-3">
            Loading…
          </p>
        ) : tasks.length === 0 ? (
          <p className="text-sm text-ink-3">No tasks yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {tasks.map((task) => (
              <li key={task.task_id} className="py-2 first:pt-0 last:pb-0">
                <button
                  type="button"
                  onClick={() => void open(task.task_id)}
                  className="flex w-full flex-wrap items-center gap-2 rounded-lg px-1 py-1 text-left hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {pill(task.status)}
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-3">
                    {task.task_id}
                  </span>
                  <span className="max-w-full truncate text-xs text-ink-2">
                    {task.capability_id || task.target_agent_id || "—"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {selected && (
        <section
          aria-labelledby="task-detail-heading"
          className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2
              id="task-detail-heading"
              className="text-xs font-semibold uppercase tracking-widest text-ink-3"
            >
              Task detail
            </h2>
            {pill(selected.status)}
          </div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
              Target
            </dt>
            <dd className="font-mono text-xs text-ink-2">
              {selected.target_agent_id || "—"}
            </dd>
            <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
              Capability
            </dt>
            <dd className="font-mono text-xs text-ink-2">
              {selected.capability_id || "—"}
            </dd>
            <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
              Correlation
            </dt>
            <dd className="font-mono text-xs text-ink-2">
              {selected.correlation_id || "—"}
            </dd>
            <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
              Lineage
            </dt>
            <dd className="font-mono text-xs text-ink-2">
              origin {selected.origin || "user"} · depth{" "}
              {selected.depth ?? 0}
              {selected.parent_task_id
                ? ` · parent ${selected.parent_task_id.slice(-6)}`
                : ""}
              {selected.remote_task_id
                ? ` · remote ${selected.remote_task_id}`
                : ""}
            </dd>
            {selected.error_code && (
              <>
                <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                  Error
                </dt>
                <dd className="text-sm text-red-600">
                  {selected.error_code} — {selected.error_detail}
                </dd>
              </>
            )}
            {selected.output && Object.keys(selected.output).length > 0 && (
              <>
                <dt className="text-xs font-medium uppercase tracking-wide text-ink-3">
                  Result
                </dt>
                <dd className="min-w-0 break-words font-mono text-xs text-ink-2">
                  {JSON.stringify(selected.output)}
                </dd>
              </>
            )}
          </dl>
          <div className="flex flex-wrap gap-2">
            {(selected.status === "WAITING_APPROVAL" ||
              selected.status === "AUTHORIZED") && (
              <button
                type="button"
                onClick={() => void act("advance", selected.task_id)}
                disabled={busy}
                className="inline-flex items-center justify-center rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Advance
              </button>
            )}
            {!["COMPLETED", "FAILED", "CANCELLED"].includes(
              selected.status
            ) && (
              <button
                type="button"
                onClick={() => void act("cancel", selected.task_id)}
                disabled={busy}
                className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Cancel
              </button>
            )}
            {selected.status === "FAILED" && (
              <button
                type="button"
                onClick={() => void act("retry", selected.task_id)}
                disabled={busy}
                className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Retry
              </button>
            )}
          </div>
          {(selected.children ?? []).length > 0 && (
            <div className="space-y-1">
              <p className="text-xs font-medium uppercase tracking-wide text-ink-3">
                Children
              </p>
              <ul className="space-y-1">
                {(selected.children ?? []).map((child) => (
                  <li key={child.task_id}>
                    <button
                      type="button"
                      onClick={() => void open(child.task_id)}
                      className="flex w-full items-center gap-2 rounded-lg px-1 py-1 text-left hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      {pill(child.status)}
                      <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-3">
                        {child.task_id}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(selected.events ?? []).length > 0 && (
            <div className="space-y-1">
              <p className="text-xs font-medium uppercase tracking-wide text-ink-3">
                Timeline
              </p>
              <ul className="space-y-1">
                {(selected.events ?? []).map((event, i) => (
                  <li
                    key={`${event.event}-${i}`}
                    className="font-mono text-[11px] text-ink-3"
                  >
                    {event.event}
                    {event.detail ? ` — ${event.detail}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      <section
        aria-labelledby="workflows-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="workflows-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Workflows
        </h2>
        {workflows.length === 0 ? (
          <p className="text-sm text-ink-3">No workflows yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {workflows.map((flow) => (
              <li key={flow.id} className="py-2 first:pt-0 last:pb-0">
                <button
                  type="button"
                  onClick={() => void openFlow(flow.id)}
                  className="flex w-full flex-wrap items-center gap-2 rounded-lg px-1 py-1 text-left hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {pill(flow.status)}
                  <span className="min-w-0 flex-1 truncate text-sm text-ink">
                    {flow.name || flow.id}
                  </span>
                  <span className="font-mono text-[11px] text-ink-3">
                    {flow.steps.length} steps
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {selectedFlow && (
          <div className="space-y-2 rounded-lg border border-line bg-bg p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm font-medium text-ink">
                {selectedFlow.name || selectedFlow.id}
              </p>
              {pill(selectedFlow.status)}
            </div>
            <ul className="space-y-1">
              {selectedFlow.steps.map((step) => (
                <li
                  key={step.step_id}
                  className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-ink-2"
                >
                  <span className="text-ink">{step.step_key}</span>
                  {pill(step.status)}
                  <span className="truncate">
                    {step.target_agent} · {step.capability}
                  </span>
                  {step.error && (
                    <span className="text-red-600">{step.error}</span>
                  )}
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap gap-2">
              {selectedFlow.status === "RUNNING" && (
                <button
                  type="button"
                  onClick={() => void flowAct("pause", selectedFlow.id)}
                  disabled={busy}
                  className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Pause
                </button>
              )}
              {!["COMPLETED", "FAILED", "CANCELLED"].includes(
                selectedFlow.status
              ) && (
                <button
                  type="button"
                  onClick={() => void flowAct("cancel", selectedFlow.id)}
                  disabled={busy}
                  className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Cancel
                </button>
              )}
            </div>
          </div>
        )}
      </section>

      <section
        aria-labelledby="schedules-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="schedules-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Scheduled work
        </h2>
        <div className="grid gap-2 sm:grid-cols-2">
          <div className="space-y-1">
            <label
              htmlFor="sched-name"
              className="block text-xs font-medium text-ink-2"
            >
              Name
            </label>
            <input
              id="sched-name"
              type="text"
              value={schedName}
              onChange={(e) => setSchedName(e.target.value)}
              placeholder="Morning brief"
              disabled={busy}
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <div className="space-y-1">
            <label
              htmlFor="sched-agent"
              className="block text-xs font-medium text-ink-2"
            >
              Agent
            </label>
            <input
              id="sched-agent"
              type="text"
              value={schedAgent}
              onChange={(e) => setSchedAgent(e.target.value)}
              placeholder="default"
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <div className="space-y-1">
            <label
              htmlFor="sched-trigger"
              className="block text-xs font-medium text-ink-2"
            >
              Cron (min hr day mon dow)
            </label>
            <input
              id="sched-trigger"
              type="text"
              value={schedTrigger}
              onChange={(e) => setSchedTrigger(e.target.value)}
              placeholder="0 8 * * *"
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <div className="space-y-1">
            <label
              htmlFor="sched-cap"
              className="block text-xs font-medium text-ink-2"
            >
              Capability (optional)
            </label>
            <input
              id="sched-cap"
              type="text"
              value={schedCap}
              onChange={(e) => setSchedCap(e.target.value)}
              placeholder="research.web_search@v1"
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
        </div>
        <button
          type="button"
          onClick={createSchedule}
          disabled={busy}
          className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save schedule"}
        </button>
        {schedules.length === 0 ? (
          <p className="text-sm text-ink-3">No schedules yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {schedules.map((schedule) => (
              <li
                key={schedule.id}
                className="flex flex-col gap-2 py-2 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0 space-y-0.5">
                  <p className="text-sm font-medium text-ink">
                    {schedule.name || schedule.id}
                  </p>
                  <p className="font-mono text-[11px] text-ink-3">
                    {schedule.trigger} · next{" "}
                    {schedule.next_run_at || "—"}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void toggleSchedule(schedule)}
                  disabled={busy}
                  className="inline-flex shrink-0 items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {schedule.enabled ? "Disable" : "Enable"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
