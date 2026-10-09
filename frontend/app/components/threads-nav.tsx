"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { RelativeTime } from "./ui";

type Thread = {
  thread_id: string;
  title: string;
  updated_at: string;
};

const SESSION_KEY = "nexus-session-id";

function mintSessionId(): string {
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export default function ThreadsNav() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeThread = searchParams.get("t") ?? "";

  const [threads, setThreads] = useState<Thread[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      let agent = "";
      try {
        agent = localStorage.getItem("nexus-chat-agent") || "";
      } catch {
        /* persistence is best-effort */
      }
      const suffix = agent ? `?agent=${encodeURIComponent(agent)}` : "";
      const data = await api<{ threads: Thread[] }>(
        `/chat/threads${suffix}`
      );
      setThreads(data.threads);
    } catch {
      /* sidebar history is best-effort */
    } finally {
      setLoading(false);
    }
  }, []);

  const newChat = useCallback(() => {
    const fresh = mintSessionId();
    try {
      localStorage.setItem(SESSION_KEY, fresh);
    } catch {
      /* persistence is best-effort */
    }
    router.push(`/chat?t=${encodeURIComponent(fresh)}`);
  }, [router]);

  useEffect(() => {
    if (pathname.startsWith("/chat")) void load();
  }, [pathname, load, activeThread]);

  const remove = useCallback(
    async (threadId: string) => {
      setDeleting(threadId);
      try {
        await api(`/chat/threads/${encodeURIComponent(threadId)}`, {
          method: "DELETE",
        });
        setThreads((prev) => prev.filter((t) => t.thread_id !== threadId));
      } catch {
        /* best-effort */
      } finally {
        setDeleting(null);
      }
    },
    []
  );

  if (!pathname.startsWith("/chat")) return null;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1 px-3">
      <button
        type="button"
        onClick={newChat}
        className="inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-br from-accent to-accent-d px-3 py-2 text-sm font-medium text-white shadow-glow transition-all hover:brightness-110 active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
      >
        <svg
          aria-hidden="true"
          width="14"
          height="14"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        >
          <line x1="8" y1="2" x2="8" y2="14" />
          <line x1="2" y1="8" x2="14" y2="8" />
        </svg>
        New chat
      </button>
      <p className="px-1 pb-1 pt-3 text-xs font-medium uppercase tracking-wide text-ink-3">
        History
      </p>
      <nav aria-label="Chat history" className="min-h-0 flex-1 overflow-y-auto">
        {loading ? (
          <div aria-live="polite" className="space-y-2 px-1 py-1">
            <span className="sr-only">Loading chats…</span>
            <div aria-hidden="true" className="skeleton h-8 rounded-lg" />
            <div aria-hidden="true" className="skeleton h-8 rounded-lg" />
            <div aria-hidden="true" className="skeleton h-8 w-3/4 rounded-lg" />
          </div>
        ) : threads.length === 0 ? (
          <p className="px-1 py-1 text-sm text-ink-3">
            No chats yet — start one above.
          </p>
        ) : (
          <ul className="space-y-0.5">
            {threads.map((thread) => {
              const active = thread.thread_id === activeThread;
              return (
                <li
                  key={thread.thread_id}
                  className={`group flex items-center gap-1 rounded-lg border border-transparent transition-colors ${
                    active
                      ? "border-accent/30 bg-accent-soft"
                      : "hover:bg-bg-hover"
                  }`}
                >
                  <Link
                    href={`/chat?t=${encodeURIComponent(thread.thread_id)}`}
                    title={`${thread.title || "Untitled"}${thread.updated_at ? ` · ${thread.updated_at}` : ""}`}
                    aria-current={active ? "page" : undefined}
                    className={`min-w-0 flex-1 truncate px-2.5 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                      active ? "font-medium text-accent-ink" : "text-ink-2"
                    }`}
                  >
                    <span className="block truncate">
                      {thread.title || "Untitled"}
                    </span>
                    {thread.updated_at && (
                      <span className="block text-[11px] text-ink-3">
                        <RelativeTime
                          iso={thread.updated_at}
                          className="text-[11px] text-ink-3"
                        />
                      </span>
                    )}
                  </Link>
                  <button
                    type="button"
                    onClick={() => void remove(thread.thread_id)}
                    disabled={deleting === thread.thread_id}
                    aria-label={`Delete chat: ${thread.title || "Untitled"}`}
                    className="mr-1 shrink-0 rounded-md px-1.5 py-1 text-xs text-ink-3 opacity-0 transition-all hover:bg-danger-bg hover:text-danger-text focus:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent group-hover:opacity-100 disabled:opacity-50"
                  >
                    {deleting === thread.thread_id ? "…" : "✕"}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </nav>
    </div>
  );
}
