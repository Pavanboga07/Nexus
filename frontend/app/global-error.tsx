"use client";

export default function GlobalError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg text-ink antialiased">
        <main className="mx-auto flex w-full max-w-2xl flex-col items-center gap-3 px-4 py-16 text-center">
          <h1 className="text-lg font-semibold">Something went wrong</h1>
          <p className="max-w-md text-sm text-ink-2">
            The app hit an unexpected error. Reload to continue — your data
            is stored on the server.
          </p>
          <button
            type="button"
            onClick={reset}
            className="inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white"
          >
            Reload
          </button>
        </main>
      </body>
    </html>
  );
}
