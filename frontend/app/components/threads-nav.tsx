"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

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
        className="inline-flex items-center justify-center gap-2 rounded-lg border border-line bg-bg px-3 py-2 text-sm font-medium text-ink hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <span aria-hidden="true" className="text-base leading-none">
          +
        </span>
        New chat
      </button>
      <p className="px-1 pb-1 pt-3 text-xs font-medium uppercase tracking-wide text-ink-3">
        History
      </p>
      <nav aria-label="Chat history" className="min-h-0 flex-1 overflow-y-auto">
        {loading ? (
          <p aria-live="polite" className="px-1 py-1 text-sm text-ink-3">
            Loading…
          </p>
        ) : threads.length === 0 ? (
          <p className="px-1 py-1 text-sm text-ink-3">No chats yet.</p>
        ) : (
          <ul className="space-y-0.5">
            {threads.map((thread) => {
              const active = thread.thread_id === activeThread;
              return (
                <li
                  key={thread.thread_id}
                  className={`group flex items-center gap-1 rounded-lg ${
                    active ? "bg-bg-hover" : "hover:bg-bg-hover"
                  }`}
                >
                  <Link
                    href={`/chat?t=${encodeURIComponent(thread.thread_id)}`}
                    title={thread.title}
                    aria-current={active ? "page" : undefined}
                    className={`min-w-0 flex-1 truncate px-2 py-1.5 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                      active ? "font-medium text-ink" : "text-ink-2"
                    }`}
                  >
                    {thread.title || "Untitled"}
                  </Link>
                  <button
                    type="button"
                    onClick={() => void remove(thread.thread_id)}
                    disabled={deleting === thread.thread_id}
                    aria-label={`Delete chat: ${thread.title || "Untitled"}`}
                    className="shrink-0 rounded px-1.5 py-1 text-xs text-ink-3 opacity-0 hover:text-danger focus:opacity-100 focus:outline-none group-hover:opacity-100 disabled:opacity-50"
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
