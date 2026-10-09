"use client";

import { useCallback, useEffect, useState } from "react";
import { API_BASE, api } from "../components/api";
import { LlmKeySettings } from "../components/llm-key-settings";
import { getOperatorToken, setOperatorToken } from "../components/operator";
import {
  ErrorAlert,
  PageHeader,
  StatusNote,
  inputClass,
  primaryButtonClass,
  useToast,
} from "../components/ui";

export default function SettingsPage() {
  const notify = useToast();
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [backupBusy, setBackupBusy] = useState(false);
  const [rotating, setRotating] = useState(false);

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
    notify(
      token.trim() ? "Operator token saved" : "Operator token cleared",
      "success"
    );
  }, [token, notify]);

  const rotateToken = useCallback(async () => {
    setRotating(true);
    setError(null);
    setStatus(null);
    try {
      const data = await api<{ token: string; rotated: boolean }>(
        "/settings/operator-token/rotate",
        { method: "POST" }
      );
      if (data.token) {
        setOperatorToken(data.token);
        setToken(data.token);
        setStatus(
          "Token rotated — the new token is saved on this device. " +
            "Paste it into any other browser sessions."
        );
        notify("Token rotated", "success");
      } else {
        setError("Rotation did not return a new token.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not rotate token.");
    } finally {
      setRotating(false);
    }
  }, []);

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
      notify("Backup downloaded", "success");
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
              <button
                type="button"
                onClick={() => void rotateToken()}
                disabled={rotating}
                className="inline-flex shrink-0 items-center justify-center rounded-lg border border-line bg-bg px-4 py-1.5 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {rotating ? "Rotating…" : "Rotate token"}
              </button>
            </form>
            <p className="mt-2 text-xs text-ink-3">
              Rotating replaces the token everywhere — re-paste it on any
              other device or browser you use.
            </p>
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
