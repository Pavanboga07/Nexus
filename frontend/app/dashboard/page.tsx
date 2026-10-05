"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "../components/api";
import {
  EmptyState,
  ErrorAlert,
  LoadingSkeleton,
  PageHeader,
  StatusBadge,
} from "../components/ui";

type Agent = { id: string; display_name: string; status: string };
type Task = {
  task_id: string;
  target_agent_id: string;
  capability_id: string;
  status: string;
  purpose: string;
};
type Workflow = { id: string; name: string; status: string };
type Schedule = {
  id: string;
  agent_id: string;
  name: string;
  enabled: boolean;
  next_run_at: string;
};

const STEPS = [
  { href: "/agents", label: "Create an agent", hint: "Give it a name and capabilities" },
  { href: "/chat", label: "Chat with it", hint: "Pick the agent, send a message" },
  { href: "/tasks", label: "Run a task", hint: "Assign work, watch it finish" },
  { href: "/autonomy", label: "Automate it", hint: "Schedules and triggers" },
  { href: "/people", label: "Connect a peer", hint: "Pair and trust agents" },
];

export default function DashboardPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [a, t, w, s] = await Promise.all([
        api<{ agents: Agent[] }>("/agents").catch(() => ({ agents: [] })),
        api<{ tasks: Task[] }>("/tasks?limit=50").catch(() => ({ tasks: [] })),
        api<{ workflows: Workflow[] }>("/workflows").catch(() => ({
          workflows: [],
        })),
        api<{ schedules: Schedule[] }>("/autonomy/schedules").catch(() => ({
          schedules: [],
        })),
      ]);
      setAgents(a.agents);
      setTasks(t.tasks);
      setWorkflows(w.workflows);
      setSchedules(s.schedules);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load dashboard.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const running = tasks.filter((t) =>
    ["pending", "running", "dispatched", "resolving", "authorized"].includes(
      (t.status || "").toLowerCase()
    )
  );
  const needsAttention = tasks.filter((t) =>
    ["failed", "delivery_failed"].includes((t.status || "").toLowerCase())
  );

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 px-4 py-6 sm:px-6">
      <PageHeader
        title="Dashboard"
        subtitle="What your agents are doing, what needs attention, and what to do next."
        action={
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:opacity-50"
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        }
      />
      {error && (
        <div className="mb-4">
          <ErrorAlert message={error} />
        </div>
      )}
      {loading ? (
        <LoadingSkeleton label="Loading dashboard" />
      ) : agents.length === 0 ? (
        <EmptyState
          title="No agents yet."
          hint="Create your first agent to start chatting, assigning tasks, and automating work."
          actionLabel="Create your first agent"
          actionHref="/agents"
        />
      ) : (
        <div className="space-y-6">
          <section aria-labelledby="overview-heading">
            <h2
              id="overview-heading"
              className="mb-2 text-xs font-semibold uppercase tracking-widest text-ink-3"
            >
              Overview
            </h2>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {[
                { label: "Agents", value: agents.length, href: "/agents" },
                { label: "Running tasks", value: running.length, href: "/tasks" },
                { label: "Workflows", value: workflows.length, href: "/workflows" },
                {
                  label: "Schedules",
                  value: schedules.filter((s) => s.enabled).length,
                  href: "/autonomy",
                },
              ].map((card) => (
                <Link
                  key={card.label}
                  href={card.href}
                  className="rounded-xl border border-line bg-bg-subtle p-4 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  <p className="text-2xl font-semibold text-ink">{card.value}</p>
                  <p className="mt-1 text-xs text-ink-2">{card.label}</p>
                </Link>
              ))}
            </div>
          </section>

          {needsAttention.length > 0 && (
            <section aria-labelledby="attention-heading">
              <h2
                id="attention-heading"
                className="mb-2 text-xs font-semibold uppercase tracking-widest text-ink-3"
              >
                Needs attention
              </h2>
              <ul className="space-y-2">
                {needsAttention.slice(0, 5).map((t) => (
                  <li
                    key={t.task_id}
                    className="flex items-center justify-between gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-2.5"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-ink">
                        {t.capability_id || t.purpose || t.task_id}
                      </p>
                      <p className="truncate text-xs text-ink-2">
                        {t.target_agent_id}
                      </p>
                    </div>
                    <StatusBadge status={t.status} />
                  </li>
                ))}
              </ul>
              <Link
                href="/tasks"
                className="mt-2 inline-block text-sm text-accent hover:underline"
              >
                View all tasks →
              </Link>
            </section>
          )}

          <section aria-labelledby="agents-heading">
            <h2
              id="agents-heading"
              className="mb-2 text-xs font-semibold uppercase tracking-widest text-ink-3"
            >
              Your agents
            </h2>
            <ul className="space-y-2">
              {agents.map((a) => (
                <li
                  key={a.id}
                  className="flex items-center justify-between gap-3 rounded-xl border border-line bg-bg px-4 py-2.5"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-ink">
                      {a.display_name || a.id}
                    </p>
                    <p className="truncate font-mono text-xs text-ink-3">
                      {a.id}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <StatusBadge status={a.status} />
                    <Link
                      href={`/chat?agent=${encodeURIComponent(a.id)}`}
                      aria-label={`Chat with ${a.id}`}
                      className="rounded-lg border border-line px-3 py-1.5 text-xs font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      Chat
                    </Link>
                  </div>
                </li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="next-heading">
            <h2
              id="next-heading"
              className="mb-2 text-xs font-semibold uppercase tracking-widest text-ink-3"
            >
              What to do next
            </h2>
            <ol className="space-y-2">
              {STEPS.map((s, i) => (
                <li key={s.href}>
                  <Link
                    href={s.href}
                    className="flex items-center gap-3 rounded-xl border border-line bg-bg px-4 py-2.5 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <span
                      aria-hidden="true"
                      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-bg-hover text-xs font-semibold text-ink-2"
                    >
                      {i + 1}
                    </span>
                    <span>
                      <span className="block text-sm font-medium text-ink">
                        {s.label}
                      </span>
                      <span className="block text-xs text-ink-2">{s.hint}</span>
                    </span>
                  </Link>
                </li>
              ))}
            </ol>
          </section>
        </div>
      )}
    </main>
  );
}
