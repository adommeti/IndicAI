/** Locating the flagged span inside the transcript.
 *
 *  The API hands back `evidence_span` as text, not as offsets, so the UI has to
 *  find it. Two rules govern this: never highlight something that is not the
 *  evidence, and never silently pretend the evidence was found. A reviewer who
 *  cannot see the span in context has to be told that, because the alternative —
 *  an arbitrary highlight — changes what they think they are ruling on.
 */

export interface TextPart {
  text: string;
  evidence: boolean;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Whitespace in a transcript is not meaningful and does not survive a round trip
 *  through a detector prompt, so the match is whitespace-tolerant. Nothing else
 *  is normalised: matching case-insensitively across scripts, or stripping
 *  diacritics, would let the highlight land on the wrong word. */
function evidencePattern(span: string): RegExp | null {
  const tokens = span.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return null;
  return new RegExp(tokens.map(escapeRegExp).join("\\s+"), "gu");
}

/** Split one rendered string into plain and evidence parts. Every occurrence is
 *  marked: a phrase repeated in the same utterance is flagged evidence twice. */
export function markEvidence(text: string, span: string): TextPart[] {
  const pattern = evidencePattern(span);
  if (!pattern || !text) return [{ text, evidence: false }];
  const parts: TextPart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) parts.push({ text: text.slice(cursor, index), evidence: false });
    parts.push({ text: match[0], evidence: true });
    cursor = index + match[0].length;
  }
  if (parts.length === 0) return [{ text, evidence: false }];
  if (cursor < text.length) parts.push({ text: text.slice(cursor), evidence: false });
  return parts;
}

export function hasEvidence(parts: readonly TextPart[]): boolean {
  return parts.some((part) => part.evidence);
}

/** True when the span could not be found in any of the rendered lines, which is
 *  what drives the "evidence shown separately" notice rather than a bare screen. */
export function evidenceLocated(rendered: readonly string[], span: string): boolean {
  const pattern = evidencePattern(span);
  if (!pattern) return false;
  return rendered.some((line) => {
    pattern.lastIndex = 0;
    return pattern.test(line);
  });
}

/**
 * What to show where an English rendering should be, when there is none.
 *
 * `render_english` is allowed to come back empty: the detector treats a failed
 * translation as non-fatal, because a reviewer who reads the language does not
 * need one and an empty rendering is better than a wrong one. Rendered
 * verbatim that produced an empty bordered box under a heading promising a
 * translation, which reads as a UI that has broken rather than as a field with
 * nothing in it. Say so instead.
 */
export function renderingOrAbsent(value: string): { text: string; absent: boolean } {
  const text = value.trim();
  return text
    ? { text, absent: false }
    : { text: "No English rendering was produced for this span.", absent: true };
}
