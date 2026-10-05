"use client";

import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { API_BASE } from "./api";
import { getOperatorToken, setOperatorToken } from "./operator";

export default function OperatorBanner() {
  const pathname = usePathname();
  const [locked, setLocked] = useState(false);
  const [input, setInput] = useState("");
  const [checking, setChecking] = useState(true);

  const probe = useCallback(async () => {
    setChecking(true);
    try {
      const token = getOperatorToken();
      const res = await fetch(`${API_BASE}/settings/llm-status`, {
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      });
      setLocked(res.status === 401);
    } catch {
      setLocked(false);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    void probe();
  }, [probe, pathname]);

  if (checking || !locked) return null;

  return (
    <div
      role="alert"
      className="border-b border-amber-200 bg-amber-50 px-4 py-3"
    >
      <p className="text-sm font-medium text-amber-800">
        This app needs your operator token.
      </p>
      <p className="mt-0.5 text-xs text-amber-700">
        Find it with{" "}
        <code className="rounded bg-amber-100 px-1 font-mono">
          cat /data/operator_token
        </code>{" "}
        inside the backend container (or your data dir), paste it once —
        it stays in this browser.
      </p>
      <form
        className="mt-2 flex flex-col gap-2 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (input.trim().length === 0) return;
          setOperatorToken(input.trim());
          setInput("");
          void probe();
        }}
      >
        <label htmlFor="operator-token-input" className="sr-only">
          Operator token
        </label>
        <input
          id="operator-token-input"
          type="password"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Paste operator token…"
          autoComplete="off"
          className="w-full rounded-lg border border-amber-300 bg-white px-3 py-1.5 font-mono text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 sm:max-w-sm"
        />
        <button
          type="submit"
          disabled={input.trim().length === 0}
          className="inline-flex shrink-0 items-center justify-center rounded-lg bg-accent px-4 py-1.5 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Unlock
        </button>
      </form>
    </div>
  );
}
