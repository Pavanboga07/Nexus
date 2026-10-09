"use client";

const TOKEN_KEY = "nexus-operator-token";

export function getOperatorToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setOperatorToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* persistence is best-effort */
  }
}

/** Authorization header for API calls; empty object when no token saved. */
export function authHeaders(): Record<string, string> {
  const token = getOperatorToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
