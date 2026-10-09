"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../components/api";
import {
  EmptyState,
  ErrorRetry,
  LoadingSkeleton,
  PageHeader,
  StatusBadge,
  StatusNote,
  inputClass,
  primaryButtonClass,
  secondaryButtonClass,
  useToast,
} from "../components/ui";

type Step = {
  step_id: string;
  step_key: string;
  target_agent: string;
  capability: string;
  status: string;
  task_id: string;
  error: string;
};

type Workflow = {
  id: string;
  name: string;
  status: string;
  steps: Step[];
};

export default function WorkflowsPage() {
  const notify = useToast();
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [agent, setAgent] = useState("default");
  const [capability, setCapability] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<Workflow | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api<{ workflows: Workflow[] }>("/workflows");
      setWorkflows(data.workflows);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load workflows.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = useCallback(async () => {
    if (!name.trim() || !capability.trim() || busy) return;
    setBusy("create");
    setError(null);
    setStatus(null);
    try {
      const flow = await api<Workflow>("/workflows/run", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          steps: [
            {
              key: "step-1",
              target_agent: agent.trim() || "default",
              capability: capability.trim(),
              input: {},
            },
          ],
        }),
      });
      setName("");
      setCapability("");
      setStatus(`Workflow “${flow.name}” started (${flow.status}).`);
      notify("Workflow started", "success");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start workflow.");
    } finally {
      setBusy(null);
    }
  }, [name, agent, capability, busy, load]);

  const act = useCallback(
    async (id: string, action: "pause" | "cancel") => {
      setBusy(id + action);
      setError(null);
      try {
        await api(`/workflows/${encodeURIComponent(id)}/${action}`, {
          method: "POST",
        });
        setStatus(`Workflow ${action}d.`);
        notify(`Workflow ${action}d`, "info");
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : `Could not ${action}.`);
      } finally {
        setBusy(null);
      }
    },
    [load]
  );

  const inspect = useCallback(async (id: string) => {
    setError(null);
    try {
      const flow = await api<Workflow>(
        `/workflows/${encodeURIComponent(id)}`
      );
      setSelected(flow);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load workflow.");
    }
  }, []);

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-6 sm:px-6">
      <PageHeader
        title="Workflows"
        subtitle="Multi-step jobs: what each workflow does, step by step, and whether it finished."
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
        aria-labelledby="new-workflow-heading"
        className="mb-6 rounded-xl border border-line bg-bg-subtle p-4"
      >
        <h2
          id="new-workflow-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Start a workflow
        </h2>
        <form
          className="mt-3 space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <div>
            <label htmlFor="wf-name" className="block text-xs font-medium text-ink-2">
              Workflow name
            </label>
            <input
              id="wf-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. morning research"
              className={inputClass}
            />
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <div className="flex-1">
              <label htmlFor="wf-agent" className="block text-xs font-medium text-ink-2">
                Agent
              </label>
              <input
                id="wf-agent"
                value={agent}
                onChange={(e) => setAgent(e.target.value)}
                placeholder="default"
                className={inputClass}
              />
            </div>
            <div className="flex-1">
              <label htmlFor="wf-cap" className="block text-xs font-medium text-ink-2">
                Capability
              </label>
              <input
                id="wf-cap"
                value={capability}
                onChange={(e) => setCapability(e.target.value)}
                placeholder="agent.capability@v1"
                className={inputClass}
              />
            </div>
          </div>
          <button type="submit" disabled={busy === "create"} className={primaryButtonClass}>
            {busy === "create" ? "Starting…" : "Start workflow"}
          </button>
        </form>
      </section>

      <section aria-labelledby="workflows-heading">
        <h2
          id="workflows-heading"
          className="mb-2 text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          All workflows
        </h2>
        {loading ? (
          <LoadingSkeleton label="Loading workflows" />
        ) : workflows.length === 0 ? (
          <EmptyState
            title="No workflows yet."
            hint="Start your first workflow above — a named sequence of agent steps."
          />
        ) : (
          <ul className="space-y-2">
            {workflows.map((w) => (
              <li
                key={w.id}
                className="rounded-xl border border-line bg-bg px-4 py-3"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-ink">
                      {w.name || w.id}
                    </p>
                    <p className="truncate font-mono text-xs text-ink-3">
                      {w.id} · {w.steps?.length ?? 0} step(s)
                    </p>
                  </div>
                  <StatusBadge status={w.status} />
                </div>
                {Array.isArray(w.steps) && w.steps.length > 0 && (
                  <ol className="mt-2 space-y-1 border-t border-line pt-2">
                    {w.steps.map((s) => (
                      <li
                        key={s.step_id || s.step_key}
                        className="flex items-center justify-between gap-2 text-xs"
                      >
                        <span className="min-w-0 truncate text-ink-2">
                          {s.step_key}: {s.target_agent} → {s.capability}
                        </span>
                        <StatusBadge status={s.status} />
                      </li>
                    ))}
                  </ol>
                )}
                <div className="mt-2 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => void inspect(w.id)}
                    className={secondaryButtonClass}
                  >
                    Details
                  </button>
                  <button
                    type="button"
                    onClick={() => void act(w.id, "pause")}
                    disabled={busy === w.id + "pause"}
                    aria-label={`Pause workflow ${w.id}`}
                    className={secondaryButtonClass}
                  >
                    {busy === w.id + "pause" ? "Pausing…" : "Pause"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void act(w.id, "cancel")}
                    disabled={busy === w.id + "cancel"}
                    aria-label={`Cancel workflow ${w.id}`}
                    className={secondaryButtonClass}
                  >
                    {busy === w.id + "cancel" ? "Cancelling…" : "Cancel"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {selected && (
        <section
          aria-labelledby="workflow-detail-heading"
          className="mt-6 rounded-xl border border-line bg-bg-subtle p-4"
        >
          <h2
            id="workflow-detail-heading"
            className="text-xs font-semibold uppercase tracking-widest text-ink-3"
          >
            Workflow details
          </h2>
          <p className="mt-1 text-sm font-medium text-ink">
            {selected.name} ({selected.id})
          </p>
          <div className="mt-1">
            <StatusBadge status={selected.status} />
          </div>
          <ol className="mt-3 space-y-2">
            {(selected.steps ?? []).map((s) => (
              <li
                key={s.step_id || s.step_key}
                className="rounded-lg border border-line bg-bg p-3 text-sm"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-ink">{s.step_key}</span>
                  <StatusBadge status={s.status} />
                </div>
                <p className="mt-1 text-xs text-ink-2">
                  {s.target_agent} → {s.capability}
                </p>
                {s.task_id && (
                  <p className="mt-1 font-mono text-xs text-ink-3">
                    task {s.task_id}
                  </p>
                )}
                {s.error && (
                  <p className="mt-1 text-xs text-danger-text">{s.error}</p>
                )}
              </li>
            ))}
          </ol>
          <button
            type="button"
            onClick={() => setSelected(null)}
            className={`mt-3 ${secondaryButtonClass}`}
          >
            Close details
          </button>
        </section>
      )}
    </main>
  );
}
