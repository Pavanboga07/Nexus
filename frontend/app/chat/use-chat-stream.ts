"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE } from "../components/api";
import { authHeaders } from "../components/operator";

export type StreamToolCard = {
  tool: string;
  status: "pending" | "completed";
  result?: string;
};

export type ThreadTurn = {
  role: string;
  text: string;
  citations: string[];
  created_at: string;
};

/**
 * Owns all LLM-stream state and behavior for the chat page: the
 * streaming text, tool cards, citations, error state, the SSE pump
 * (`runStream`), and the auto-scroll-while-streaming effect.
 *
 * Finished turns are folded into the thread through `onTurnsFolded`;
 * everything else stays inside this hook so `ChatInner` keeps the
 * orchestration (peers, approvals, answers, live socket) only.
 */
export function useChatStream(opts: {
  sessionId: string;
  agentHandle: string;
  nearBottomRef: { current: boolean };
  onTurnsFolded: (turns: ThreadTurn[]) => void;
}) {
  const { sessionId, agentHandle, nearBottomRef, onTurnsFolded } = opts;

  const [streamInput, setStreamInput] = useState("");
  const [streamText, setStreamText] = useState<string | null>(null);
  const [toolCards, setToolCards] = useState<StreamToolCard[]>([]);
  const [citations, setCitations] = useState<string[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const streamAccumRef = useRef<{
    tokens: string;
    doneText: string;
    citations: string[];
  }>({ tokens: "", doneText: "", citations: [] });
  const abortRef = useRef<AbortController | null>(null);

  const stopStream = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  /** Clear the live block (e.g. when switching threads). */
  const resetStream = useCallback(() => {
    setStreamText(null);
    setToolCards([]);
    setCitations([]);
    setStreamError(null);
  }, []);

  // Stick to the bottom while streaming when the user hasn't scrolled up.
  useEffect(() => {
    if (streaming && nearBottomRef.current) {
      window.scrollTo({
        top: document.documentElement.scrollHeight,
        behavior: "auto",
      });
    }
  }, [streamText, streaming, nearBottomRef]);

  const runStream = useCallback(async () => {
    const prompt = streamInput.trim();
    if (prompt.length === 0 || streaming) return;
    setStreaming(true);
    setStreamError(null);
    setStreamText("");
    setToolCards([]);
    setCitations([]);
    streamAccumRef.current = { tokens: "", doneText: "", citations: [] };
    const controller = new AbortController();
    abortRef.current = controller;
    let failed = false;
    try {
      const res = await fetch(
        `${API_BASE}/chat/stream?message=${encodeURIComponent(prompt)}&session_id=${encodeURIComponent(sessionId)}&agent=${encodeURIComponent(agentHandle)}`,
        {
          headers: { Accept: "text/event-stream", ...authHeaders() },
          signal: controller.signal,
        }
      );
      if (!res.ok || !res.body) {
        const body = (await res.json().catch(() => ({}))) as {
          detail?: string;
        };
        throw new Error(body.detail ?? `stream failed (${res.status})`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let cut: number;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, cut);
          buf = buf.slice(cut + 2);
          for (const line of frame.split("\n")) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data:")) continue;
            const evt = JSON.parse(trimmed.slice(5)) as {
              type: string;
              text?: string;
              tool?: string;
              result?: string;
              citations?: string[];
              message?: string;
            };
            if (evt.type === "token" && evt.text) {
              setStreamText((prev) => (prev ?? "") + (evt.text as string));
              streamAccumRef.current.tokens += evt.text as string;
            } else if (evt.type === "tool_pending" && evt.tool) {
              const tool = evt.tool;
              setToolCards((prev) => [...prev, { tool, status: "pending" }]);
            } else if (evt.type === "tool_completed" && evt.tool) {
              const tool = evt.tool;
              const result = evt.result;
              setToolCards((prev) => {
                const next = [...prev];
                const open = next.findIndex(
                  (c) => c.tool === tool && c.status === "pending"
                );
                if (open >= 0) {
                  next[open] = { tool, status: "completed", result };
                } else {
                  next.push({ tool, status: "completed", result });
                }
                return next;
              });
            } else if (evt.type === "done") {
              if (evt.text) {
                const text = evt.text;
                setStreamText((prev) =>
                  prev && prev.length > 0 ? prev : text
                );
                streamAccumRef.current.doneText = text;
              }
              streamAccumRef.current.citations = evt.citations ?? [];
              setCitations(evt.citations ?? []);
            } else if (evt.type === "error") {
              throw new Error(evt.message ?? "Stream failed.");
            }
          }
        }
      }
    } catch (err) {
      if (controller.signal.aborted) {
        // Stopped by the user: keep the partial text, no error banner.
        failed = false;
      } else {
        failed = true;
        setStreamError(err instanceof Error ? err.message : "Stream failed.");
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setStreaming(false);
      // Fold the finished turn into local history (the server persisted
      // the same rows): the thread reads as one conversation and the
      // stream block clears instead of duplicating.
      if (!failed) {
        const accum = streamAccumRef.current;
        const finalText = accum.doneText || accum.tokens;
        if (finalText) {
          const userText = prompt;
          const assistantCits = accum.citations;
          onTurnsFolded([
            { role: "user", text: userText, citations: [], created_at: "" },
            {
              role: "assistant",
              text: finalText,
              citations: assistantCits,
              created_at: "",
            },
          ]);
          setStreamText(null);
          setToolCards([]);
          setCitations([]);
        }
      }
    }
  }, [streamInput, streaming, sessionId, agentHandle, onTurnsFolded]);

  const hasStreamTurn =
    (streamText !== null && streamText !== "") ||
    toolCards.length > 0 ||
    citations.length > 0 ||
    streaming ||
    streamError !== null;

  return {
    streamInput,
    setStreamInput,
    streamText,
    toolCards,
    citations,
    streaming,
    streamError,
    hasStreamTurn,
    runStream,
    stopStream,
    resetStream,
  };
}
