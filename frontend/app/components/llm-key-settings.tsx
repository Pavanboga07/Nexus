"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { ErrorAlert, StatusNote } from "./ui";

export type LlmStatus = {
  configured: boolean;
  provider_hint: string;
  base_url?: string;
  model?: string;
};

const PRESETS: Record<string, { label: string; base_url: string; model: string }> = {
  openai: {
    label: "OpenAI",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
  },
  gemini: {
    label: "Google Gemini",
    base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
    model: "gemini-3.8-flash",
  },
  custom: { label: "Custom (OpenAI-compatible)", base_url: "", model: "" },
};

function presetFor(baseUrl: string): string {
  const lower = (baseUrl || "").toLowerCase();
  if (lower.includes("generativelanguage")) return "gemini";
  if (lower.includes("openai.com")) return "openai";
  return "custom";
}

/** Full provider/key form — lives on the Settings page. */
export function LlmKeySettings() {
  const [status, setStatus] = useState<LlmStatus | null>(null);
  const [keyInput, setKeyInput] = useState("");
  const [preset, setPreset] = useState("openai");
  const [baseUrl, setBaseUrl] = useState(PRESETS.openai.base_url);
  const [model, setModel] = useState(PRESETS.openai.model);
  const [models, setModels] = useState<string[] | null>(null);
  const [modelsBusy, setModelsBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [keyError, setKeyError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const next = await api<LlmStatus>("/settings/llm-status");
      setStatus(next);
      if (next.base_url) {
        setBaseUrl(next.base_url);
        setPreset(presetFor(next.base_url));
      }
      if (next.model) setModel(next.model);
    } catch {
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const pickPreset = useCallback((name: string) => {
    setPreset(name);
    const entry = PRESETS[name];
    if (entry && name !== "custom") {
      setBaseUrl(entry.base_url);
      setModel(entry.model);
    }
    setModels(null);
  }, []);

  const loadModels = useCallback(async () => {
    setModelsBusy(true);
    setKeyError(null);
    try {
      const data = await api<{ models: string[] }>("/settings/llm-models");
      setModels(data.models);
    } catch (err) {
      setKeyError(err instanceof Error ? err.message : "Could not list models.");
    } finally {
      setModelsBusy(false);
    }
  }, []);

  const saveKey = useCallback(async () => {
    const key = keyInput.trim();
    const base = baseUrl.trim().replace(/\/+$/, "");
    const chosen = model.trim();
    if (saving) return;
    setSaving(true);
    setKeyError(null);
    setSaved(null);
    try {
      await api("/settings/llm-key", {
        method: "POST",
        body: JSON.stringify({ key, base_url: base, model: chosen }),
      });
      setKeyInput("");
      setSaved(
        key.length > 0
          ? "Model key saved — chat is ready."
          : "Provider saved — paste a key to verify."
      );
      await loadStatus();
    } catch (err) {
      setKeyError(err instanceof Error ? err.message : "Could not save key.");
    } finally {
      setSaving(false);
    }
  }, [keyInput, baseUrl, model, saving, loadStatus]);

  return (
    <section
      aria-labelledby="llm-key-heading"
      className="rounded-xl border border-line bg-bg-subtle p-4"
    >
      <h2
        id="llm-key-heading"
        className="text-xs font-semibold uppercase tracking-widest text-ink-3"
      >
        Model provider
      </h2>
      <p aria-live="polite" className="mt-1 text-sm text-ink-2">
        {status
          ? status.configured
            ? `Configured (${status.provider_hint})`
            : `No model key yet — pick a provider and paste your key (${status.provider_hint})`
          : "Checking model key…"}
      </p>
      {saved && (
        <div className="mt-2">
          <StatusNote message={saved} />
        </div>
      )}
      {keyError && (
        <div className="mt-2">
          <ErrorAlert message={keyError} />
        </div>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void saveKey();
        }}
        className="mt-3 space-y-2"
      >
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="flex-1 space-y-1">
            <label
              htmlFor="llm-preset"
              className="block text-xs font-medium text-ink-2"
            >
              Provider
            </label>
            <select
              id="llm-preset"
              value={preset}
              onChange={(e) => pickPreset(e.target.value)}
              disabled={saving}
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {Object.entries(PRESETS).map(([name, entry]) => (
                <option key={name} value={name}>
                  {entry.label}
                </option>
              ))}
            </select>
          </div>
          <div className="flex-1 space-y-1">
            <label
              htmlFor="llm-model"
              className="block text-xs font-medium text-ink-2"
            >
              Model
            </label>
            <input
              id="llm-model"
              type="text"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="gemini-3.8-flash"
              autoComplete="off"
              spellCheck={false}
              disabled={saving}
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
        </div>
        <div className="space-y-1">
          <label
            htmlFor="llm-base-url"
            className="block text-xs font-medium text-ink-2"
          >
            Base URL (OpenAI-compatible)
          </label>
          <input
            id="llm-base-url"
            type="text"
            value={baseUrl}
            onChange={(e) => {
              setBaseUrl(e.target.value);
              setPreset(presetFor(e.target.value));
            }}
            placeholder="https://…/v1"
            autoComplete="off"
            spellCheck={false}
            disabled={saving}
            className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
          />
        </div>
        <div className="space-y-1">
          <label
            htmlFor="llm-key-input"
            className="block text-xs font-medium text-ink-2"
          >
            {status?.configured ? "Rotate key (paste new)" : "API key"}
          </label>
          <input
            id="llm-key-input"
            type="password"
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            placeholder="Paste key…"
            autoComplete="off"
            disabled={saving}
            className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="submit"
            disabled={saving}
            className="inline-flex shrink-0 items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving
              ? "Verifying…"
              : status?.configured
                ? "Save changes"
                : "Save key"}
          </button>
          <button
            type="button"
            onClick={loadModels}
            disabled={modelsBusy}
            className="inline-flex shrink-0 items-center justify-center rounded-lg border border-line bg-bg px-4 py-2 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {modelsBusy ? "Loading…" : "Load models"}
          </button>
        </div>
        {models && (
          <div className="space-y-1">
            <p className="text-xs text-ink-3">
              {models.length === 0
                ? "No models returned."
                : "Pick a model:"}
            </p>
            {models.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {models.map((id) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setModel(id)}
                    aria-pressed={id === model.trim()}
                    className={`inline-flex items-center rounded-full border px-2.5 py-1 font-mono text-xs focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                      id === model.trim()
                        ? "border-accent bg-accent-soft text-accent-ink"
                        : "border-line bg-bg text-ink-2 hover:bg-bg-hover"
                    }`}
                  >
                    {id}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </form>
      <p className="mt-2 text-xs text-ink-3">
        Verified live before it replaces the old one — a bad key is rejected
        and never stored.
      </p>
    </section>
  );
}

/** Slim one-line model status for the Chat page — links to Settings. */
export function LlmStatusLine() {
  const [status, setStatus] = useState<LlmStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    api<LlmStatus>("/settings/llm-status")
      .then((next) => {
        if (!cancelled) setStatus(next);
      })
      .catch(() => {
        if (!cancelled) setStatus(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <p
      aria-live="polite"
      className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl border border-line bg-bg-subtle px-4 py-2.5 text-sm text-ink-2"
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 rounded-full ${
          status?.configured ? "bg-success-bg0" : "bg-warning-bg0"
        }`}
      />
      {status
        ? status.configured
          ? `Model ready (${status.provider_hint})`
          : "No model key yet — chat needs one to reply."
        : "Checking model…"}
      <Link
        href="/settings"
        className="font-medium text-accent hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        {status?.configured ? "Manage in Settings" : "Add key in Settings →"}
      </Link>
    </p>
  );
}
