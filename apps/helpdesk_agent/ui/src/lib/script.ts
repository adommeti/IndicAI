import type { LanguageTag } from "./types";

/** Language, and the script a Hindi speaker wants to read and be answered in.
 *
 *  Hindi is the only one of uc1's languages the graph accepts in two scripts:
 *  `hi-IN` (Devanagari) and `hi-Latn` (romanised). It is not a display filter —
 *  `helpdesk_agent/graph.py` replies in whichever tag it was given, and the voice
 *  pipeline picks its Bulbul speaker from it — so this choice changes what the
 *  employee is *answered in*, not just what this page draws.
 *
 *  Telugu, Tamil and English have one script each here, so the toggle applies to
 *  Hindi alone and is hidden for the others rather than sitting there doing nothing.
 */

export type ScriptPref = "deva" | "latn";

export type SpokenLanguage = "hi" | "te" | "ta" | "en";

export interface LanguageOption {
  value: SpokenLanguage;
  /** Endonym first: this is the list an Indian employee is picking from. */
  label: string;
  english: string;
  /** Whether this language offers the Devanagari/Latin choice. */
  scripted: boolean;
}

export const LANGUAGES: readonly LanguageOption[] = [
  { value: "hi", label: "हिन्दी", english: "Hindi", scripted: true },
  { value: "te", label: "తెలుగు", english: "Telugu", scripted: false },
  { value: "ta", label: "தமிழ்", english: "Tamil", scripted: false },
  { value: "en", label: "English", english: "English", scripted: false },
];

export const SCRIPT_PREFS: readonly { value: ScriptPref; label: string; hint: string }[] = [
  { value: "deva", label: "देवनागरी", hint: "Hindi in Devanagari (hi-IN)" },
  { value: "latn", label: "Latin", hint: "Hindi written in Latin script (hi-Latn)" },
];

/** The tag sent to `POST /chat/turn` and used for the voice session. */
export function languageTag(language: SpokenLanguage, script: ScriptPref): LanguageTag {
  switch (language) {
    case "hi":
      return script === "latn" ? "hi-Latn" : "hi-IN";
    case "te":
      return "te-IN";
    case "ta":
      return "ta-IN";
    case "en":
      return "en-IN";
  }
}

export function languageOf(tag: LanguageTag): SpokenLanguage {
  if (tag === "hi-IN" || tag === "hi-Latn") return "hi";
  if (tag === "te-IN") return "te";
  if (tag === "ta-IN") return "ta";
  return "en";
}

export function scriptOf(tag: LanguageTag): ScriptPref {
  return tag === "hi-Latn" ? "latn" : "deva";
}

/** Tag labels for anything the pipeline reports back. Saaras identifies the
 *  language itself (STT runs on `auto`), so a tag outside this table is possible
 *  and is shown verbatim rather than guessed at or hidden. */
const TAG_LABELS: Record<string, string> = {
  "hi-IN": "हिन्दी (Devanagari)",
  "hi-Latn": "Hindi (Latin)",
  "te-IN": "తెలుగు",
  "ta-IN": "தமிழ்",
  "en-IN": "English",
};

export function tagLabel(tag: string | null): string {
  if (!tag) return "language not identified";
  return TAG_LABELS[tag] ?? tag;
}

/** ## Where "per user" can honestly live in a browser app
 *
 *  `.claude/rules/ui.md` asks for the Hindi script preference to be persisted per
 *  user. This app has no preferences endpoint — the pinned uc1/P6 contract has
 *  `GET /me`, `POST /chat/turn` and the replay endpoint, and inventing a fourth to
 *  store a radio button is not this prompt's call to make. So the honest scope is:
 *
 *    per user, per browser profile — keyed by the `employee_id` that `GET /me`
 *    reports from the verified SSO claim, in `localStorage`.
 *
 *  What that buys: a shared helpdesk workstation does not hand the next employee
 *  the previous one's reading preference, because the key changes with the subject.
 *  What it does not buy, and is not claimed anywhere in the UI: it does not follow
 *  a user to another machine or another browser, and it is not a server-side
 *  profile. It is also never sent anywhere — it selects a language tag on requests
 *  this user makes, and is otherwise inert.
 *
 *  `employee_id` is an issuer-qualified subject and goes into a storage key, so it
 *  is encoded rather than interpolated raw.
 */
const PREFIX = "uc1.hindi-script";

export function scriptKey(employeeId: string): string {
  return `${PREFIX}.${encodeURIComponent(employeeId)}`;
}

export function loadScriptPref(employeeId: string, storage?: Storage): ScriptPref {
  try {
    const store = storage ?? window.localStorage;
    const value = store.getItem(scriptKey(employeeId));
    return value === "latn" || value === "deva" ? value : "deva";
  } catch {
    // Private windows and locked-down corporate profiles throw on access. A
    // reading preference is not worth failing the helpdesk over.
    return "deva";
  }
}

export function saveScriptPref(employeeId: string, pref: ScriptPref, storage?: Storage): void {
  try {
    (storage ?? window.localStorage).setItem(scriptKey(employeeId), pref);
  } catch {
    /* see loadScriptPref */
  }
}
