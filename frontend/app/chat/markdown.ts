/** Markdown-safe helpers for incremental (streaming) render (V5).
 *
 * While tokens stream in, the accumulated text is routinely a *partial*
 * document — most visibly a code fence that opened but has not closed
 * yet. Rendering the raw partial would swallow the rest of the reply
 * into one code block (or drop it). `closeUnclosedFences` appends the
 * missing fence so every incremental frame renders sanely; once the
 * real closing fence streams in, the count is even and the text passes
 * through untouched.
 */

export function closeUnclosedFences(text: string): string {
  const matches = text.match(/```/g);
  const count = matches === null ? 0 : matches.length;
  if (count % 2 === 1) {
    return text + "\n```";
  }
  return text;
}

export type MarkdownSegment =
  | { kind: "prose"; text: string }
  | { kind: "code"; lang: string; text: string };

/** Split fence-balanced text into alternating prose/code segments. */
export function splitSegments(text: string): MarkdownSegment[] {
  const closed = closeUnclosedFences(text);
  const raw = closed.split("```");
  const segments: MarkdownSegment[] = [];
  for (let i = 0; i < raw.length; i++) {
    if (i % 2 === 1) {
      const chunk = raw[i].replace(/^\r?\n/, "");
      const newline = chunk.indexOf("\n");
      if (newline === -1) {
        segments.push({ kind: "code", lang: chunk.trim(), text: "" });
      } else {
        segments.push({
          kind: "code",
          lang: chunk.slice(0, newline).trim(),
          text: chunk.slice(newline + 1),
        });
      }
    } else if (raw[i] !== "") {
      segments.push({ kind: "prose", text: raw[i] });
    }
  }
  return segments;
}
