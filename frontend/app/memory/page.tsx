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
    <main>
      <h1>Memory</h1>
      <p>Long-term facts this laptop remembers. Forgetting deletes one for good.</p>

      {status && (
        <p role="status" aria-live="polite">
          {status}
        </p>
      )}
      {error && <p role="alert">{error}</p>}

      <section aria-labelledby="search-heading">
        <h2 id="search-heading">Recall</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void load(query.trim() || undefined);
          }}
        >
          <label htmlFor="memory-search">Search memories</label>
          <input
            id="memory-search"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="dog, standup, …"
            disabled={loading}
          />
          <button type="submit" disabled={loading}>
            {loading ? "Searching…" : "Search"}
          </button>
          <button
            type="button"
            disabled={loading || query.length === 0}
            onClick={() => {
              setQuery("");
              void load();
            }}
          >
            Clear
          </button>
        </form>
      </section>

      <section aria-labelledby="list-heading">
        <h2 id="list-heading">Stored facts</h2>
        {loading ? (
          <p aria-live="polite">Loading…</p>
        ) : memories.length === 0 ? (
          <p>Nothing remembered yet. Chat turns store facts here.</p>
        ) : (
          <ul>
            {memories.map((m) => (
              <li key={m.id}>
                <p>{m.text}</p>
                <p>
                  <small>
                    {m.created_at || "undated"}
                    {m.session_id ? ` · ${m.session_id}` : ""}
                  </small>
                </p>
                <button
                  type="button"
                  onClick={() => forget(m.id)}
                  disabled={forgetting === m.id}
                  aria-label={`Forget: ${m.text.slice(0, 60)}`}
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
