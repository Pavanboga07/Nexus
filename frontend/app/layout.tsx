"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense } from "react";
import OperatorBanner from "./components/operator-banner";
import ThreadsNav from "./components/threads-nav";
import "./globals.css";

const navLink =
  "rounded-lg px-3 py-2 text-sm text-ink-2 transition-colors hover:bg-bg-hover hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent";

const NAV_ITEMS = [
  { href: "/", label: "Dashboard" },
  { href: "/agents", label: "Agents" },
  { href: "/chat", label: "Chat" },
  { href: "/tasks", label: "Tasks" },
  { href: "/workflows", label: "Workflows" },
  { href: "/autonomy", label: "Autonomy" },
  { href: "/memory", label: "Memory" },
  { href: "/people", label: "People" },
  { href: "/settings", label: "Settings" },
];

function PrimaryNav({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const active = (href: string) =>
    href === "/"
      ? pathname === "/"
        ? "bg-bg-hover text-ink font-medium"
        : ""
      : pathname.startsWith(href)
        ? "bg-bg-hover text-ink font-medium"
        : "";
  return (
    <nav aria-label="Primary" className="flex flex-col gap-1 px-3 text-sm">
      {NAV_ITEMS.map((item) => (
        <Link
          key={item.href}
          className={`${navLink} ${active(item.href)}`}
          href={item.href}
          onClick={onNavigate}
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg text-ink antialiased">
        <div className="flex min-h-screen">
          <aside className="fixed inset-y-0 left-0 z-40 hidden w-60 shrink-0 flex-col gap-2 border-r border-line bg-bg-subtle py-4 md:flex">
            <div className="flex items-center gap-2 px-4 pb-2">
              <span className="text-base font-semibold tracking-tight text-ink">
                Nexus
              </span>
            </div>
            <Suspense>
              <ThreadsNav />
            </Suspense>
            <div className="border-t border-line pt-2">
              <PrimaryNav />
            </div>
            <div className="mt-auto border-t border-line px-4 pt-3">
              <p className="text-xs text-ink-3">
                Local-first pair. Approvals stay on this machine.
              </p>
            </div>
          </aside>
          <div className="flex min-w-0 flex-1 flex-col md:ml-60">
            <header className="flex items-center justify-between gap-2 border-b border-line bg-bg px-4 py-2 md:hidden">
              <span className="text-sm font-semibold text-ink">Nexus</span>
              <nav aria-label="Primary" className="flex flex-wrap items-center gap-1 text-sm">
                {NAV_ITEMS.map((item) => (
                  <Link key={item.href} className={navLink} href={item.href}>
                    {item.label}
                  </Link>
                ))}
              </nav>
            </header>
            <OperatorBanner />
            {children}
          </div>
        </div>
      </body>
    </html>
  );
}
