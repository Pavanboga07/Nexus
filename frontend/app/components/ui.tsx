"use client";

import Link from "next/link";
import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

/* ── headings ───────────────────────────────────────────────────────── */

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
        <h1 className="text-2xl font-semibold tracking-tight text-ink">
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

/* ── alerts ─────────────────────────────────────────────────────────── */

export function ErrorAlert({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="rounded-lg border border-danger-border bg-danger-bg px-3 py-2 text-sm text-danger-text"
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
      className="rounded-lg border border-success-border bg-success-bg px-3 py-2 text-sm text-success-text"
    >
      {message}
    </p>
  );
}

/** Error surface with a retry affordance — use on every failure view. */
export function ErrorRetry({
  message,
  onRetry,
  label = "Retry",
}: {
  message: string;
  onRetry: () => void;
  label?: string;
}) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
      <div className="min-w-0 flex-1">
        <ErrorAlert message={message} />
      </div>
      <button
        type="button"
        onClick={onRetry}
        className={secondaryButtonClass}
      >
        {label}
      </button>
    </div>
  );
}

/* ── loading ────────────────────────────────────────────────────────── */

export function LoadingSkeleton({ label = "Loading…" }: { label?: string }) {
  return (
    <div aria-busy="true" aria-live="polite" className="space-y-3">
      <span className="sr-only">{label}</span>
      <div aria-hidden="true" className="space-y-3">
        <div className="skeleton h-10 w-2/3 rounded-xl" />
        <div className="skeleton h-16 w-full rounded-xl" />
        <div className="skeleton h-10 w-1/2 rounded-xl" />
      </div>
    </div>
  );
}

/** Tighter skeleton for list rows. */
export function RowSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div aria-hidden="true" className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-12 w-full rounded-xl" />
      ))}
    </div>
  );
}

/* ── empty states ───────────────────────────────────────────────────── */

export function EmptyState({
  title,
  hint,
  actionLabel,
  actionHref,
  onAction,
}: {
  title: string;
  hint?: string;
  actionLabel?: string;
  actionHref?: string;
  onAction?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-line bg-bg-subtle px-6 py-10 text-center">
      <span
        aria-hidden="true"
        className="mb-1 flex h-11 w-11 items-center justify-center rounded-2xl bg-accent-soft text-accent-ink"
      >
        <svg
          width="20"
          height="20"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M10 2.5 12 8l5.5 2-5.5 2-2 5.5L8 12 2.5 10 8 8l2-5.5Z" />
        </svg>
      </span>
      <p className="text-sm font-medium text-ink">{title}</p>
      {hint && <p className="max-w-sm text-sm text-ink-2">{hint}</p>}
      {actionLabel && (actionHref || onAction) && (
        <span className="mt-2">
          {actionHref ? (
            <Link href={actionHref} className={primaryButtonClass}>
              {actionLabel}
            </Link>
          ) : (
            <button
              type="button"
              onClick={onAction}
              className={primaryButtonClass}
            >
              {actionLabel}
            </button>
          )}
        </span>
      )}
    </div>
  );
}

/* ── status badges ──────────────────────────────────────────────────── */

const STATUS_STYLES: Record<string, string> = {
  completed: "bg-success-bg text-success-text border-success-border",
  done: "bg-success-bg text-success-text border-success-border",
  enabled: "bg-success-bg text-success-text border-success-border",
  trusted: "bg-success-bg text-success-text border-success-border",
  active: "bg-success-bg text-success-text border-success-border",
  running: "bg-info-bg text-info-text border-info-border",
  dispatched: "bg-info-bg text-info-text border-info-border",
  resolving: "bg-info-bg text-info-text border-info-border",
  authorized: "bg-info-bg text-info-text border-info-border",
  limited: "bg-info-bg text-info-text border-info-border",
  pending: "bg-warning-bg text-warning-text border-warning-border",
  waiting_approval: "bg-warning-bg text-warning-text border-warning-border",
  paused: "bg-warning-bg text-warning-text border-warning-border",
  approval_required: "bg-warning-bg text-warning-text border-warning-border",
  failed: "bg-danger-bg text-danger-text border-danger-border",
  delivery_failed: "bg-danger-bg text-danger-text border-danger-border",
  cancelled: "bg-bg-hover text-ink-3 border-line",
  denied: "bg-danger-bg text-danger-text border-danger-border",
  revoked: "bg-danger-bg text-danger-text border-danger-border",
  disabled: "bg-bg-hover text-ink-3 border-line",
  suspended: "bg-bg-hover text-ink-3 border-line",
};

export function StatusBadge({ status }: { status: string }) {
  const key = (status || "").toLowerCase();
  const style = STATUS_STYLES[key] ?? "bg-bg-hover text-ink-2 border-line";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${style}`}
    >
      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current" />
      {status || "unknown"}
    </span>
  );
}

/* ── cards ──────────────────────────────────────────────────────────── */

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`surface-card ${className}`}>{children}</div>
  );
}

export function SectionCard({
  id,
  title,
  action,
  children,
  className = "",
}: {
  id: string;
  title: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      aria-labelledby={id}
      className={`rounded-xl border border-line bg-bg-subtle p-5 ${className}`}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2
          id={id}
          className="text-xs font-semibold uppercase tracking-widest text-ink-3"
        >
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

/* ── relative time ──────────────────────────────────────────────────── */

export function formatRelative(iso: string): string | null {
  if (!iso) return null;
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return null;
  const diff = Date.now() - ts;
  if (diff < 0) return "in the future";
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 5) return `${weeks}w ago`;
  return new Date(ts).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function RelativeTime({
  iso,
  className = "text-xs text-ink-3",
}: {
  iso: string;
  className?: string;
}) {
  const label = formatRelative(iso);
  if (!label) return null;
  const ts = new Date(iso);
  const absolute = Number.isNaN(ts.getTime())
    ? iso
    : ts.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
  return (
    <time dateTime={iso} title={absolute} className={className}>
      {label}
    </time>
  );
}

/* ── copy button + code block ───────────────────────────────────────── */

export function CopyButton({
  text,
  label = "Copy",
}: {
  text: string;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
      onClick={() => {
        const done = () => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 2000);
        };
        try {
          const result = navigator.clipboard?.writeText(text);
          if (result && typeof result.then === "function") {
            result.then(done, () => setCopied(false));
          } else {
            done();
          }
        } catch {
          setCopied(false);
        }
      }}
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-ink-3 transition-colors hover:bg-bg-hover hover:text-ink-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      <svg
        aria-hidden="true"
        width="13"
        height="13"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <rect x="5" y="5" width="9" height="9" rx="2" />
        <path d="M11 5V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v5a2 2 0 0 0 2 2h1" />
      </svg>
      {copied ? "Copied" : label}
    </button>
  );
}

export function CodeBlock({
  lang,
  text,
}: {
  lang: string;
  text: string;
}) {
  const label = lang || "code";
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-[#0a0a10]">
      <div className="flex items-center justify-between gap-2 border-b border-line bg-bg-raise/60 px-3 py-1.5">
        <span className="font-mono text-[11px] uppercase tracking-wider text-ink-3">
          {label}
        </span>
        <CopyButton text={text} label="Copy code" />
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed text-ink-2">
        <code>{text.replace(/\n$/, "")}</code>
      </pre>
    </div>
  );
}

/* ── typing indicator ───────────────────────────────────────────────── */

export function TypingIndicator({ label = "Thinking" }: { label?: string }) {
  return (
    <p aria-live="polite" className="flex items-center gap-2 py-1">
      <span className="sr-only">{label}…</span>
      <span aria-hidden="true" className="flex items-center gap-1.5">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
      </span>
      <span aria-hidden="true" className="text-sm text-ink-3">
        {label}…
      </span>
    </p>
  );
}

/* ── toasts ─────────────────────────────────────────────────────────── */

type ToastKind = "success" | "info" | "warning" | "danger";

type Toast = { id: number; message: string; kind: ToastKind };

const ToastContext = createContext<(message: string, kind?: ToastKind) => void>(
  () => {}
);

export function useToast() {
  return useContext(ToastContext);
}

const TOAST_STYLES: Record<ToastKind, string> = {
  success: "border-success-border bg-bg-overlay text-success-text",
  info: "border-info-border bg-bg-overlay text-info-text",
  warning: "border-warning-border bg-bg-overlay text-warning-text",
  danger: "border-danger-border bg-bg-overlay text-danger-text",
};

const TOAST_DOT: Record<ToastKind, string> = {
  success: "bg-success",
  info: "bg-info",
  warning: "bg-warning",
  danger: "bg-danger",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const notify = useCallback(
    (message: string, kind: ToastKind = "info") => {
      const id = nextId.current++;
      setToasts((prev) => [...prev.slice(-3), { id, message, kind }]);
      window.setTimeout(() => dismiss(id), 4500);
    },
    [dismiss]
  );

  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div
        aria-live="polite"
        className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-2"
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role="status"
            className={`animate-toast-in pointer-events-auto flex items-start gap-2.5 rounded-xl border px-3.5 py-3 shadow-pop backdrop-blur ${TOAST_STYLES[toast.kind]}`}
          >
            <span
              aria-hidden="true"
              className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${TOAST_DOT[toast.kind]}`}
            />
            <p className="min-w-0 flex-1 text-sm text-ink">{toast.message}</p>
            <button
              type="button"
              onClick={() => dismiss(toast.id)}
              aria-label="Dismiss notification"
              className="shrink-0 rounded p-0.5 text-ink-3 transition-colors hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <span aria-hidden="true">✕</span>
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/* ── shared control classes ─────────────────────────────────────────── */

export const inputClass =
  "w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm text-ink placeholder:text-ink-3 transition-colors focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50";

export const primaryButtonClass =
  "inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white shadow-glow transition-all hover:bg-accent-d active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-50 disabled:shadow-none";

export const secondaryButtonClass =
  "inline-flex items-center justify-center rounded-lg border border-line bg-bg px-4 py-2 text-sm font-medium text-ink-2 transition-colors hover:bg-bg-hover hover:text-ink active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50";

export const ghostButtonClass =
  "inline-flex items-center justify-center rounded-lg px-3 py-1.5 text-sm font-medium text-ink-2 transition-colors hover:bg-bg-hover hover:text-ink active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50";

export const dangerButtonClass =
  "inline-flex items-center justify-center rounded-lg border border-danger-border bg-bg px-4 py-2 text-sm font-medium text-danger-text transition-colors hover:bg-danger-bg active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-danger/50 disabled:cursor-not-allowed disabled:opacity-50";
