"use client";

export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col items-center gap-3 px-4 py-16 text-center">
      <h1 className="text-lg font-semibold text-ink">
        Something went wrong
      </h1>
      <p className="max-w-md text-sm text-ink-2">
        This page hit an unexpected error. Your data is safe — try again,
        or go back to the dashboard.
      </p>
      {error?.message && (
        <details className="w-full rounded-lg border border-line bg-bg-subtle p-3 text-left">
          <summary className="cursor-pointer text-xs font-medium text-ink-2">
            Technical details
          </summary>
          <p className="mt-2 break-words font-mono text-xs text-ink-3">
            {error.message}
          </p>
        </details>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={reset}
          className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-d focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
        >
          Try again
        </button>
        <a
          href="/chat"
          className="inline-flex items-center justify-center rounded-lg border border-line bg-bg px-4 py-2 text-sm font-medium text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-3/50"
        >
          Back to chat
        </a>
      </div>
    </main>
  );
}
