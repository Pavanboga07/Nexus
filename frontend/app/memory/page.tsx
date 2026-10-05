"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, api } from "../components/api";
import { authHeaders } from "../components/operator";

type Memory = {
  id: string;
  text: string;
  session_id: string;
  created_at: string;
};

type AuditEntry = {
  memory_id: string;
  action: string;
  at: string;
};

export default function MemoryPage() {
  const [query, setQuery] = useState("");
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [audit, setAudit] = useState<AuditEntry[] | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async (q?: string) => {
    setLoading(true);
    setError(null);
    try {
      const path = q ? `/memory?q=${encodeURIComponent(q)}` : "/memory";
      const data = await api<{ memories: Memory[] }>(path);
      setMemories(data.memories);
      setSelected([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load memories.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const forget = useCallback(async (id: string) => {
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
  }, []);

  const saveEdit = useCallback(async () => {
    if (!editingId || saving || draft.trim().length === 0) return;
    setSaving(true);
    setError(null);
    try {
      await api(`/memory/${encodeURIComponent(editingId)}`, {
        method: "PUT",
        body: JSON.stringify({ text: draft.trim() }),
      });
      setMemories((prev) =>
        prev.map((m) => (m.id === editingId ? { ...m, text: draft.trim() } : m))
      );
      setEditingId(null);
      setDraft("");
      setStatus("Memory updated.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save.");
    } finally {
      setSaving(false);
    }
  }, [editingId, draft, saving]);

  const bulkDelete = useCallback(async () => {
    if (selected.length === 0 || bulkBusy) return;
    setBulkBusy(true);
    setError(null);
    try {
      const data = await api<{ forgotten: number; missing: string[] }>(
        "/memory/bulk-delete",
        { method: "POST", body: JSON.stringify({ ids: selected }) }
      );
      setMemories((prev) => prev.filter((m) => !selected.includes(m.id)));
      setSelected([]);
      setStatus(
        data.missing.length > 0
          ? `Forgot ${data.forgotten}; ${data.missing.length} already gone.`
          : `Forgot ${data.forgotten}.`
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Bulk delete failed.");
    } finally {
      setBulkBusy(false);
    }
  }, [selected, bulkBusy]);

  const exportAll = useCallback(async () => {
    setExporting(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/memory/export`, {
        headers: { ...authHeaders() },
      });
      if (!res.ok) throw new Error(`export failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "nexus-memory.json";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setStatus("Exported memories to nexus-memory.json.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not export.");
    } finally {
      setExporting(false);
    }
  }, []);

  const importFile = useCallback(
    async (file: File) => {
      setImporting(true);
      setImportError(null);
      setError(null);
      try {
        let parsed: unknown;
        try {
          parsed = JSON.parse(await file.text());
        } catch {
          setImportError(
            "Could not read that file — choose a JSON file shaped {memories: [{text}]}."
          );
          return;
        }
        const list = Array.isArray(parsed)
          ? parsed
          : (parsed as { memories?: unknown }).memories;
        if (!Array.isArray(list)) {
          setImportError(
            "Import failed — file must be {memories: [{text}]}."
          );
          return;
        }
        if (list.length === 0) {
          setStatus("No memories found in that file.");
          return;
        }
        const data = await api<{ imported: number }>("/memory/import", {
          method: "POST",
          body: JSON.stringify({ memories: list }),
        });
        setStatus(`Imported ${data.imported} memories.`);
        await load(query.trim() || undefined);
      } catch (err) {
        setImportError(
          err instanceof Error ? err.message : "Import failed."
        );
      } finally {
        setImporting(false);
        if (fileRef.current) fileRef.current.value = "";
      }
    },
    [load, query]
  );

  const loadAudit = useCallback(async () => {
    try {
      const data = await api<{ audit: AuditEntry[] }>("/memory/audit");
      setAudit(data.audit);
    } catch {
      setAudit([]);
    }
  }, []);

  const toggleSelect = useCallback((id: string) => {
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }, []);

  return (
    <main className="mx-auto w-full max-w-3xl space-y-8 px-4 py-8 sm:px-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight text-ink">
          Memory
        </h1>
        <p className="text-sm text-ink-2">
          Long-term facts this laptop remembers. Forgetting deletes one for
          good.
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
        aria-labelledby="search-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="search-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
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
              className="block text-xs font-medium text-ink-2"
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
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={loading}
              className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
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
              className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-4 py-2 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Clear
            </button>
          </div>
        </form>
      </section>

      <section
        aria-labelledby="list-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2
            id="list-heading"
            className="text-xs font-semibold uppercase tracking-widest text-ink-3"
          >
            Stored facts
          </h2>
          {selected.length > 0 && (
            <button
              type="button"
              onClick={bulkDelete}
              disabled={bulkBusy}
              className="inline-flex items-center justify-center rounded-lg border border-red-200 bg-bg px-3 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {bulkBusy ? "Forgetting…" : `Forget ${selected.length} selected`}
            </button>
          )}
        </div>
        {loading ? (
          <p aria-live="polite" className="text-sm text-ink-3">
            Loading…
          </p>
        ) : memories.length === 0 ? (
          <p className="text-sm text-ink-3">
            Nothing remembered yet. Chat turns store facts here.
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {memories.map((m) => (
              <li
                key={m.id}
                className="flex flex-col gap-3 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-start sm:justify-between"
              >
                <div className="flex min-w-0 flex-1 items-start gap-2">
                  <input
                    type="checkbox"
                    checked={selected.includes(m.id)}
                    onChange={() => toggleSelect(m.id)}
                    aria-label={`Select: ${m.text.slice(0, 60)}`}
                    className="mt-1 h-4 w-4 shrink-0 accent-[#10a37f]"
                  />
                  <div className="min-w-0 flex-1 space-y-1">
                    {editingId === m.id ? (
                      <div className="space-y-2">
                        <label htmlFor={`edit-${m.id}`} className="sr-only">
                          Edit memory
                        </label>
                        <textarea
                          id={`edit-${m.id}`}
                          value={draft}
                          onChange={(e) => setDraft(e.target.value)}
                          rows={2}
                          disabled={saving}
                          className="w-full resize-none rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
                        />
                        <div className="flex gap-2">
                          <button
                            type="button"
                            onClick={saveEdit}
                            disabled={saving || draft.trim().length === 0}
                            className="inline-flex items-center justify-center rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            {saving ? "Saving…" : "Save"}
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setEditingId(null);
                              setDraft("");
                            }}
                            disabled={saving}
                            className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <p className="text-sm leading-relaxed text-ink">
                          {m.text}
                        </p>
                        <p className="font-mono text-[11px] text-ink-3">
                          {m.created_at || "undated"}
                          {m.session_id ? ` · ${m.session_id}` : ""}
                        </p>
                      </>
                    )}
                  </div>
                </div>
                {editingId !== m.id && (
                  <div className="flex shrink-0 gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setEditingId(m.id);
                        setDraft(m.text);
                      }}
                      className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-3 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => forget(m.id)}
                      disabled={forgetting === m.id}
                      aria-label={`Forget: ${m.text.slice(0, 60)}`}
                      className="inline-flex items-center justify-center rounded-lg border border-red-200 bg-bg px-3 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {forgetting === m.id ? "Forgetting…" : "Forget"}
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section
        aria-labelledby="library-heading"
        className="space-y-3 rounded-xl border border-line bg-bg-subtle p-5"
      >
        <h2
          id="library-heading"
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          Library
        </h2>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <button
            type="button"
            onClick={exportAll}
            disabled={exporting}
            className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-4 py-2 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {exporting ? "Exporting…" : "Export memories"}
          </button>
          <div className="flex items-center gap-2">
            <label
              htmlFor="memory-import"
              className="inline-flex cursor-pointer items-center justify-center rounded-lg border border-line bg-bg px-4 py-2 text-sm font-medium text-ink-2 hover:bg-bg-hover focus-within:ring-2 focus-within:ring-ink-3/50"
            >
              {importing ? "Importing…" : "Import JSON…"}
            </label>
            <input
              ref={fileRef}
              id="memory-import"
              type="file"
              accept="application/json"
              disabled={importing}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void importFile(file);
              }}
              className="sr-only"
            />
          </div>
        </div>
        {importError && (
          <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {importError}
          </p>
        )}
        <details
          className="rounded-lg border border-line bg-bg px-3 py-2"
          onToggle={(e) => {
            if ((e.target as HTMLDetailsElement).open && audit === null) {
              void loadAudit();
            }
          }}
        >
          <summary className="cursor-pointer text-sm font-medium text-ink-2">
            Forget history
          </summary>
          {audit === null ? (
            <p className="py-2 text-sm text-ink-3">Open to load.</p>
          ) : audit.length === 0 ? (
            <p className="py-2 text-sm text-ink-3">Nothing forgotten yet.</p>
          ) : (
            <ul className="space-y-1 py-2">
              {audit.map((entry, i) => (
                <li
                  key={`${entry.memory_id}-${i}`}
                  className="font-mono text-[11px] text-ink-3"
                >
                  {entry.action} · {entry.memory_id}
                  {entry.at ? ` · ${entry.at}` : ""}
                </li>
              ))}
            </ul>
          )}
        </details>
      </section>
    </main>
  );
}
