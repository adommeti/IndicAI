import type { TranscriptSegment } from "./types";

/** Hindi (and the other Indic transcripts) can be read in the native script or
 *  romanised. Reviewers differ: some read Devanagari fluently, some only Latin.
 *  The API carries both on every segment; this module is the whole of the choice.
 */
export type ScriptPref = "native" | "latn";

export const SCRIPT_PREFS: { value: ScriptPref; label: string; hint: string }[] = [
  { value: "native", label: "देवनागरी / native", hint: "As spoken: Devanagari, Telugu or Tamil" },
  { value: "latn", label: "Latin", hint: "Romanised rendering where one exists" },
];

export interface RenderedText {
  text: string;
  /** What is actually on screen, which is not always what was asked for. */
  script: ScriptPref;
  /** True when Latin was asked for and no romanisation exists, so the native
   *  text is shown instead. Surfaced in the UI rather than passed off silently. */
  fellBack: boolean;
}

export function pickText(segment: TranscriptSegment, pref: ScriptPref): RenderedText {
  if (pref === "latn") {
    const roman = segment.text_roman;
    if (roman && roman.trim()) return { text: roman, script: "latn", fellBack: false };
    return { text: segment.text, script: "native", fellBack: true };
  }
  return { text: segment.text, script: "native", fellBack: false };
}

const PREFIX = "cs.script";

/** Persisted per user, keyed by the identity GET /me returns, so a shared
 *  workstation does not hand one reviewer another's reading preference. It is a
 *  display preference only — it is never sent to the server and never affects
 *  what the server will answer. */
export function scriptKey(identity: string): string {
  return `${PREFIX}.${identity}`;
}

export function loadScriptPref(identity: string, storage?: Storage): ScriptPref {
  try {
    const store = storage ?? window.localStorage;
    const value = store.getItem(scriptKey(identity));
    return value === "latn" || value === "native" ? value : "native";
  } catch {
    // Private windows and locked-down profiles throw on access; a default
    // preference is not worth failing a compliance screen over.
    return "native";
  }
}

export function saveScriptPref(identity: string, pref: ScriptPref, storage?: Storage): void {
  try {
    (storage ?? window.localStorage).setItem(scriptKey(identity), pref);
  } catch {
    /* see loadScriptPref */
  }
}
