"use client";

/**
 * Same scheme allowlist as the inline markdown renderer: only
 * `http(s)` and `mailto` citations become links. Anything else —
 * notably a `javascript:` URL from hostile search content — renders as
 * plain text so it can never execute in this origin (the operator
 * token lives in this origin's localStorage).
 */
export function isSafeCitationUrl(url: string): boolean {
  return /^(https?:|mailto:)/i.test(url.trim());
}

export function CitationList({ urls }: { urls: string[] }) {
  if (urls.length === 0) return null;
  return (
    <ul aria-label="Sources" className="space-y-1 border-t border-line pt-2">
      {urls.map((url) => (
        <li key={url}>
          {isSafeCitationUrl(url) ? (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="block truncate text-xs text-accent hover:text-accent-d hover:underline"
            >
              {url}
            </a>
          ) : (
            <span className="block truncate text-xs text-ink-3">{url}</span>
          )}
        </li>
      ))}
    </ul>
  );
}
