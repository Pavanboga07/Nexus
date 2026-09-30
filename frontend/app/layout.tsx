"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import "./globals.css";

const navLink =
  "rounded-md px-2 py-1 text-neutral-400 transition-colors hover:bg-neutral-900 hover:text-neutral-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-600";

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // The chat page brings its own full-height app shell (sidebar + thread
  // + composer), so it renders full-bleed. Every other page keeps the
  // existing centered shell unchanged.
  const pathname = usePathname();
  const isChat = pathname === "/chat" || pathname.startsWith("/chat/");
  return (
    <html lang="en">
      <body className="min-h-screen bg-neutral-950 text-neutral-200 antialiased">
        {isChat ? (
          <div className="flex min-h-screen w-full flex-col">{children}</div>
        ) : (
          <div className="mx-auto flex min-h-screen w-full max-w-3xl flex-col px-4 py-6 sm:px-6">
          <header className="mb-6 flex flex-wrap items-center justify-between gap-3 border-b border-neutral-800 pb-4">
            <span className="text-sm font-semibold tracking-tight text-neutral-50">
              Nexus
            </span>
            <nav aria-label="Primary" className="flex items-center gap-1 text-sm">
              <Link className={navLink} href="/chat">
                Chat
              </Link>
              <Link className={navLink} href="/people">
                People
              </Link>
              <Link className={navLink} href="/memory">
                Memory
              </Link>
            </nav>
          </header>
          <div className="flex-1">{children}</div>
          <footer className="mt-8 border-t border-neutral-800 pt-4 text-xs text-neutral-500">
            Local-first pair. Approvals stay on this machine.
          </footer>
          </div>
        )}
      </body>
    </html>
  );
}
