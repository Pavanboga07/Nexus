"use client";

import { authHeaders } from "./operator";

export const API_BASE =
  process.env.NEXT_PUBLIC_NEXUS_API ?? "/api";

/** Backend host for the live-update WebSocket (rewrites don't proxy WS). */
export function wsBase(): string {
  if (typeof window !== "undefined" && API_BASE.startsWith("/")) {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${window.location.hostname}:8001`;
  }
  return API_BASE.replace(/^http/, "ws");
}

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** Shared fetch wrapper: auth headers, JSON, typed errors.
 *
 * Network failures (backend down, CORS, DNS) surface as TypeError from
 * fetch — translated here into actionable messages instead of the raw
 * "Failed to fetch". HTTP errors keep the backend's detail/code.
 */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
        ...(init?.headers ?? {}),
      },
    });
  } catch (err) {
    throw new ApiError(
      API_BASE.startsWith("/")
        ? "Cannot reach the Nexus backend through the app server. " +
          "Make sure the backend is running on port 8001, then retry."
        : `Cannot connect to the Nexus backend at ${API_BASE}. ` +
          "Make sure it is running, then retry.",
      0,
      "CONNECTION_FAILED"
    );
  }
  const body = (await res.json().catch(() => ({}))) as T & {
    detail?: string;
    code?: string;
  };
  if (!res.ok) {
    throw new ApiError(
      friendlyMessage(res.status, body.detail, body.code),
      res.status,
      body.code
    );
  }
  return body;
}

function friendlyMessage(status: number, detail?: string, code?: string): string {
  if (status === 401) {
    return (
      "Your session has expired or the operator token is missing. " +
      "Reconnect in Settings, then retry."
    );
  }
  if (status === 403) return detail ?? "Not allowed.";
  if (status === 404) return detail ?? "Not found.";
  if (status === 409) return detail ?? "Already exists.";
  if (status === 413) {
    return "Request too large — the backend refused it. Trim the payload and retry.";
  }
  if (status === 429) {
    return "Too many requests — wait a moment, then retry.";
  }
  if (status === 503 && code === "MISSING_KEY") {
    return (
      "Your model provider isn't configured yet. " +
      "Add a model key in Settings, then retry."
    );
  }
  if (status >= 500) {
    return (
      detail ?? `Backend error (${status}). The server logged details — retry shortly.`
    );
  }
  return detail ?? `Request failed (${status}).`;
}

export function isExpiredError(err: unknown): boolean {
  return (
    err instanceof ApiError && (err.code === "EXPIRED" || err.status === 410)
  );
}
