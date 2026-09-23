"use client";

import { useCallback, useEffect, useState } from "react";

const API_BASE =
  process.env.NEXT_PUBLIC_NEXUS_API ?? "http://127.0.0.1:8001";

type Memory = {
  id: string;
  text: string;
  session_id: string;
  created_at: string;
};

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

export default function MemoryPage() {
  const [query, setQuery] = useState("");
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const load = useCallback(async (q?: string) => {
    setLoading(true);
    setError(null);
    try {
      const path = q ? `/memory?q=${encodeURIComponent(q)}` : "/memory";
      const data = await api<{ memories: Memory[] }>(path);
      setMemories(data.memories);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load memories.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const forget = useCallback(
    async (id: string) => {
      setForgetting(id);
      setError(null);
      setStatus(null);
      try {
        await api(`/memory/${encodeURIComponent(id)}`, { method: "DELETE" });
        setMemories((prev) => prev.filter((m) => m.id !== id));
        setStatus("Forgotten. It no longer appears in recall.");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not forget.");
      } finally {
        setForgetting(null);
      }
    },
    []
  );

  return (
    <main className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-50">
          Memory
        </h1>
        <p className="text-sm text-neutral-400">
          Long-term facts this laptop remembers. Forgetting deletes one for
          good.
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
        aria-labelledby="search-heading"
        className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-900 p-4 sm:p-5"
      >
        <h2
          id="search-heading"
          className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
        >
          Recall
        </h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void load(query.trim() || undefined);
          }}
          className="flex flex-col gap-2 sm:flex-row sm:items-end"
        >
          <div className="flex-1 space-y-1">
            <label
              htmlFor="memory-search"
              className="block text-xs font-medium uppercase tracking-wide text-neutral-500"
            >
              Search memories
            </label>
            <input
              id="memory-search"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="dog, standup, …"
              disabled={loading}
              className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 placeholder:text-neutral-600 focus:border-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-600/40 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={loading}
              className="inline-flex items-center justify-center rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? "Searching…" : "Search"}
            </button>
            <button
              type="button"
              disabled={loading || query.length === 0}
              onClick={() => {
                setQuery("");
                void load();
              }}
              className="inline-flex items-center justify-center rounded-md border border-neutral-700 bg-neutral-950 px-4 py-2 text-sm font-medium text-neutral-300 hover:bg-neutral-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-neutral-500/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Clear
            </button>
          </div>
        </form>
      </section>

      <section
        aria-labelledby="list-heading"
        className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-900 p-4 sm:p-5"
      >
        <h2
          id="list-heading"
          className="text-xs font-semibold uppercase tracking-widest text-neutral-500"
        >
          Stored facts
        </h2>
        {loading ? (
          <p aria-live="polite" className="text-sm text-neutral-500">
            Loading…
          </p>
        ) : memories.length === 0 ? (
          <p className="text-sm text-neutral-500">
            Nothing remembered yet. Chat turns store facts here.
          </p>
        ) : (
          <ul className="space-y-2">
            {memories.map((m) => (
              <li
                key={m.id}
                className="flex flex-col gap-2 rounded-lg border border-neutral-800 bg-neutral-950 p-3 sm:flex-row sm:items-start sm:justify-between"
              >
                <div className="min-w-0 flex-1 space-y-1">
                  <p className="text-sm leading-relaxed text-neutral-200">
                    {m.text}
                  </p>
                  <p className="font-mono text-[11px] text-neutral-500">
                    {m.created_at || "undated"}
                    {m.session_id ? ` · ${m.session_id}` : ""}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => forget(m.id)}
                  disabled={forgetting === m.id}
                  aria-label={`Forget: ${m.text.slice(0, 60)}`}
                  className="inline-flex shrink-0 items-center justify-center rounded-md border border-red-800 bg-red-900/60 px-3 py-1.5 text-sm font-medium text-red-100 hover:bg-red-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {forgetting === m.id ? "Forgetting…" : "Forget"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
