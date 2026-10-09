"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import OperatorBanner from "./components/operator-banner";
import ThreadsNav from "./components/threads-nav";
import { ToastProvider } from "./components/ui";
import "./globals.css";

const navLink =
  "rounded-lg px-3 py-2 text-sm text-ink-2 transition-colors hover:bg-bg-hover hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent";

const NAV_SECTIONS = [
  {
    title: "Workspace",
    items: [
      { href: "/chat", label: "Chat" },
      { href: "/agents", label: "Agents" },
      { href: "/people", label: "Network" },
    ],
  },
  {
    title: "Automate",
    items: [
      { href: "/tasks", label: "Tasks" },
      { href: "/workflows", label: "Workflows" },
      { href: "/autonomy", label: "Autonomy" },
    ],
  },
  {
    title: "Library",
    items: [{ href: "/memory", label: "Memory" }],
  },
];

const SETTINGS_ITEM = { href: "/settings", label: "Settings" };

function useActive() {
  const pathname = usePathname();
  return (href: string) =>
    pathname === href || (href !== "/chat" && pathname.startsWith(href))
      ? "bg-accent-soft text-accent-ink font-medium"
      : "";
}

function SectionNav({
  onNavigate,
  collapsed,
}: {
  onNavigate?: () => void;
  collapsed: boolean;
}) {
  const active = useActive();
  return (
    <div className="flex flex-col gap-3">
      {NAV_SECTIONS.map((section) => (
        <div key={section.title}>
          {!collapsed && (
            <p className="px-4 pb-1 text-[11px] font-semibold uppercase tracking-widest text-ink-3">
              {section.title}
            </p>
          )}
          <nav
            aria-label={section.title}
            className={`flex flex-col gap-1 text-sm ${collapsed ? "items-center px-2" : "px-3"}`}
          >
            {section.items.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                onClick={onNavigate}
                title={collapsed ? item.label : undefined}
                aria-label={item.label}
                className={
                  collapsed
                    ? `flex h-9 w-9 items-center justify-center rounded-lg text-sm font-semibold text-ink-2 transition-colors hover:bg-bg-hover hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${active(item.href)}`
                    : `${navLink} ${active(item.href)}`
                }
              >
                {collapsed ? (
                  <span aria-hidden="true">{item.label.charAt(0)}</span>
                ) : (
                  item.label
                )}
              </Link>
            ))}
          </nav>
        </div>
      ))}
    </div>
  );
}

function SettingsLink({
  onNavigate,
  collapsed,
}: {
  onNavigate?: () => void;
  collapsed: boolean;
}) {
  const active = useActive();
  return (
    <Link
      href={SETTINGS_ITEM.href}
      onClick={onNavigate}
      title={collapsed ? SETTINGS_ITEM.label : undefined}
      aria-label={SETTINGS_ITEM.label}
      className={
        collapsed
          ? `flex h-9 w-9 items-center justify-center rounded-lg text-sm font-semibold text-ink-2 transition-colors hover:bg-bg-hover hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${active(SETTINGS_ITEM.href)}`
          : `${navLink} ${active(SETTINGS_ITEM.href)}`
      }
    >
      {collapsed ? (
        <span aria-hidden="true">{SETTINGS_ITEM.label.charAt(0)}</span>
      ) : (
        SETTINGS_ITEM.label
      )}
    </Link>
  );
}

const COLLAPSE_KEY = "nexus-sidebar-collapsed";

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    try {
      setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1");
    } catch {
    }
  }, []);

  const toggleCollapsed = () => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
      } catch {
      }
      return next;
    });
  };

  return (
    <html lang="en">
      <body className="min-h-screen bg-bg-deep text-ink antialiased">
        <div className="flex min-h-screen">
          <aside
            className={`fixed inset-y-0 left-0 z-40 hidden shrink-0 flex-col gap-2 border-r border-line bg-bg py-4 shadow-card transition-[width] duration-200 md:flex ${
              collapsed ? "w-16" : "w-60"
            }`}
          >
            <div
              className={`flex items-center gap-2.5 px-4 pb-2 ${
                collapsed ? "justify-center px-0" : "justify-between"
              }`}
            >
              {!collapsed && (
                <span className="flex items-center gap-2.5">
                  <span
                    aria-hidden="true"
                    className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-accent-d text-sm font-bold text-white shadow-glow"
                  >
                    N
                  </span>
                  <span className="text-base font-semibold tracking-tight text-ink">
                    Nexus
                  </span>
                </span>
              )}
              <button
                type="button"
                onClick={toggleCollapsed}
                aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
                aria-expanded={!collapsed}
                title={collapsed ? "Expand" : "Collapse"}
                className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-ink-3 hover:bg-bg-hover hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
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
                  strokeLinejoin="round"
                >
                  {collapsed ? (
                    <polyline points="6 3 11 8 6 13" />
                  ) : (
                    <polyline points="10 3 5 8 10 13" />
                  )}
                </svg>
              </button>
            </div>
            {!collapsed && (
              <Suspense>
                <ThreadsNav />
              </Suspense>
            )}
            <div
              className={`min-h-0 flex-1 overflow-y-auto pt-2 ${
                collapsed ? "" : "border-t border-line"
              }`}
            >
              <SectionNav collapsed={collapsed} />
            </div>
            <div
              className={`border-t border-line pt-2 ${
                collapsed ? "flex justify-center px-0" : "px-3"
              }`}
            >
              <SettingsLink collapsed={collapsed} />
            </div>
            {!collapsed && (
              <div className="border-t border-line px-4 pt-3">
                <p className="text-xs text-ink-3">
                  Local-first pair. Approvals stay on this machine.
                </p>
              </div>
            )}
          </aside>

          <div
            className={`flex min-w-0 flex-1 flex-col ${
              collapsed ? "md:ml-16" : "md:ml-60"
            }`}
          >
            <header className="sticky top-0 z-30 flex items-center gap-2 border-b border-line bg-bg-deep/90 px-4 py-2 backdrop-blur md:hidden">
              <button
                type="button"
                onClick={() => setDrawer(true)}
                aria-label="Open navigation"
                className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                <svg
                  aria-hidden="true"
                  width="18"
                  height="18"
                  viewBox="0 0 16 16"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                >
                  <line x1="2" y1="4" x2="14" y2="4" />
                  <line x1="2" y1="8" x2="14" y2="8" />
                  <line x1="2" y1="12" x2="14" y2="12" />
                </svg>
              </button>
              <span className="text-sm font-semibold text-ink">Nexus</span>
            </header>
            <ToastProvider>
              <OperatorBanner />
              {children}
            </ToastProvider>
          </div>
        </div>

        {drawer && (
          <div
            className="fixed inset-0 z-50 md:hidden"
            role="dialog"
            aria-modal="true"
            aria-label="Navigation"
          >
            <div
              className="animate-fade-in absolute inset-0 bg-black/60"
              onClick={() => setDrawer(false)}
              aria-hidden="true"
            />
            <div className="animate-drawer-in absolute inset-y-0 left-0 flex w-72 flex-col gap-2 overflow-y-auto border-r border-line bg-bg py-4 shadow-pop">
              <div className="flex items-center justify-between px-4 pb-2">
                <span className="flex items-center gap-2.5">
                  <span
                    aria-hidden="true"
                    className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-accent-d text-sm font-bold text-white shadow-glow"
                  >
                    N
                  </span>
                  <span className="text-base font-semibold tracking-tight text-ink">
                    Nexus
                  </span>
                </span>
                <button
                  type="button"
                  onClick={() => setDrawer(false)}
                  aria-label="Close navigation"
                  className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-ink-2 hover:bg-bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  <span aria-hidden="true">✕</span>
                </button>
              </div>
              <Suspense>
                <ThreadsNav />
              </Suspense>
              <div className="border-t border-line pt-2">
                <SectionNav
                  collapsed={false}
                  onNavigate={() => setDrawer(false)}
                />
              </div>
              <div className="border-t border-line px-3 pt-2">
                <SettingsLink
                  collapsed={false}
                  onNavigate={() => setDrawer(false)}
                />
              </div>
            </div>
          </div>
        )}
      </body>
    </html>
  );
}
