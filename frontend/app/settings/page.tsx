"use client";

import { useCallback, useEffect, useState } from "react";
import { API_BASE } from "../components/api";
import { LlmKeySettings } from "../components/llm-key-settings";
import { getOperatorToken, setOperatorToken } from "../components/operator";
import {
  ErrorAlert,
  PageHeader,
  StatusNote,
  inputClass,
  primaryButtonClass,
} from "../components/ui";

export default function SettingsPage() {
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [backupBusy, setBackupBusy] = useState(false);

  useEffect(() => {
    setToken(getOperatorToken() ?? "");
  }, []);

  const saveToken = useCallback(() => {
    setOperatorToken(token.trim());
    setStatus(
      token.trim()
        ? "Operator token saved on this device."
        : "Operator token cleared on this device."
    );
  }, [token]);

  const exportBackup = useCallback(async () => {
    setBackupBusy(true);
    setError(null);
    setStatus(null);
    try {
      const res = await fetch(`${API_BASE}/backup`, {
        headers: {
          ...(getOperatorToken()
            ? { Authorization: `Bearer ${getOperatorToken()}` }
            : {}),
        },
      });
      if (!res.ok) throw new Error(`backup failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `nexus-backup-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus("Backup downloaded — memories, pairings, policy, and identity.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Backup failed.");
    } finally {
      setBackupBusy(false);
    }
  }, []);

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-6 sm:px-6">
      <PageHeader
        title="Settings"
        subtitle="Your connection to Nexus, your model key status, and your backup."
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
      <div className="space-y-6">
          <section
            aria-labelledby="operator-heading"
            className="rounded-xl border border-line bg-bg-subtle p-4"
          >
            <h2
              id="operator-heading"
              className="text-xs font-semibold uppercase tracking-widest text-ink-3"
            >
              Operator token
            </h2>
            <p className="mt-1 text-sm text-ink-2">
              The token that unlocks this Nexus on this device. It stays in
              your browser — never sent anywhere else.
            </p>
            <form
              className="mt-3 flex flex-col gap-2 sm:flex-row"
              onSubmit={(e) => {
                e.preventDefault();
                saveToken();
              }}
            >
              <div className="flex-1">
                <label htmlFor="op-token" className="sr-only">
                  Operator token
                </label>
                <input
                  id="op-token"
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder="Paste operator token…"
                  autoComplete="off"
                  className={inputClass}
                />
              </div>
              <button type="submit" className={primaryButtonClass}>
                Save
              </button>
            </form>
          </section>

          <LlmKeySettings />

          <section
            aria-labelledby="backup-heading"
            className="rounded-xl border border-line bg-bg-subtle p-4"
          >
            <h2
              id="backup-heading"
              className="text-xs font-semibold uppercase tracking-widest text-ink-3"
            >
              Backup
            </h2>
            <p className="mt-1 text-sm text-ink-2">
              Download everything Nexus knows about you: memories, pairings,
              policy rules, and identity. Scoped to your account only.
            </p>
            <button
              type="button"
              onClick={() => void exportBackup()}
              disabled={backupBusy}
              className={`mt-3 ${primaryButtonClass}`}
            >
              {backupBusy ? "Exporting…" : "Download backup"}
            </button>
          </section>
        </div>
    </main>
  );
}
