"use client";

import Link from "next/link";
import type { ReactNode } from "react";

export function PageHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          {title}
        </h1>
        {subtitle && (
          <p className="mt-1 max-w-xl text-sm text-ink-2">{subtitle}</p>
        )}
      </div>
      {action}
    </div>
  );
}

export function ErrorAlert({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
    >
      {message}
    </p>
  );
}

export function StatusNote({ message }: { message: string }) {
  return (
    <p
      role="status"
      aria-live="polite"
      className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700"
    >
      {message}
    </p>
  );
}

export function LoadingSkeleton({ label = "Loading…" }: { label?: string }) {
  return (
    <div aria-busy="true" aria-live="polite" className="space-y-3">
      <span className="sr-only">{label}</span>
      <div aria-hidden="true" className="space-y-3">
        <div className="h-10 w-2/3 animate-pulse rounded-xl bg-bg-hover" />
        <div className="h-16 w-full animate-pulse rounded-xl bg-bg-hover" />
        <div className="h-10 w-1/2 animate-pulse rounded-xl bg-bg-hover" />
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  actionLabel,
  actionHref,
}: {
  title: string;
  hint?: string;
  actionLabel?: string;
  actionHref?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-line bg-bg-subtle px-6 py-10 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {hint && <p className="max-w-sm text-sm text-ink-2">{hint}</p>}
      {actionLabel && actionHref && (
        <Link
          href={actionHref}
          className="mt-2 inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
        >
          {actionLabel}
        </Link>
      )}
    </div>
  );
}

const STATUS_STYLES: Record<string, string> = {
  completed: "bg-emerald-50 text-emerald-700 border-emerald-200",
  done: "bg-emerald-50 text-emerald-700 border-emerald-200",
  running: "bg-blue-50 text-blue-700 border-blue-200",
  dispatched: "bg-blue-50 text-blue-700 border-blue-200",
  pending: "bg-bg-subtle text-ink-2 border-line",
  paused: "bg-amber-50 text-amber-800 border-amber-200",
  failed: "bg-red-50 text-red-700 border-red-200",
  cancelled: "bg-bg-subtle text-ink-3 border-line",
  denied: "bg-red-50 text-red-700 border-red-200",
  enabled: "bg-emerald-50 text-emerald-700 border-emerald-200",
  disabled: "bg-bg-subtle text-ink-3 border-line",
};

export function StatusBadge({ status }: { status: string }) {
  const key = (status || "").toLowerCase();
  const style = STATUS_STYLES[key] ?? STATUS_STYLES.pending;
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${style}`}
    >
      {status || "unknown"}
    </span>
  );
}

export const inputClass =
  "w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50";

export const primaryButtonClass =
  "inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50";

export const secondaryButtonClass =
  "inline-flex items-center justify-center rounded-lg border border-line bg-bg px-4 py-2 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50 disabled:cursor-not-allowed disabled:opacity-50";
