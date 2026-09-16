import { describe, expect, it } from "vitest";
import {
  languageOf,
  languageTag,
  loadScriptPref,
  saveScriptPref,
  scriptKey,
  scriptOf,
  tagLabel,
} from "../src/lib/script";

/** A minimal Storage double: the real one is per-origin and throws in private
 *  windows, which is exactly the case the loader has to survive. */
function memoryStorage(seed: Record<string, string> = {}): Storage {
  const map = new Map(Object.entries(seed));
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key: string) => map.get(key) ?? null,
    key: (index: number) => [...map.keys()][index] ?? null,
    removeItem: (key: string) => void map.delete(key),
    setItem: (key: string, value: string) => void map.set(key, value),
  } as Storage;
}

function throwingStorage(): Storage {
  const blocked = () => {
    throw new Error("blocked");
  };
  return {
    get length(): number {
      return blocked();
    },
    clear: blocked,
    getItem: blocked,
    key: blocked,
    removeItem: blocked,
    setItem: blocked,
  } as unknown as Storage;
}

describe("the Hindi script choice picks a language tag, not a font", () => {
  it("maps Hindi to the two tags the graph accepts", () => {
    expect(languageTag("hi", "deva")).toBe("hi-IN");
    expect(languageTag("hi", "latn")).toBe("hi-Latn");
  });

  it("leaves the single-script languages alone whatever the preference says", () => {
    // Telugu, Tamil and English have one tag each here. A Latin preference must
    // not invent `te-Latn`, which the graph would reject.
    for (const script of ["deva", "latn"] as const) {
      expect(languageTag("te", script)).toBe("te-IN");
      expect(languageTag("ta", script)).toBe("ta-IN");
      expect(languageTag("en", script)).toBe("en-IN");
    }
  });

  it("round-trips a tag back to its language and script", () => {
    for (const language of ["hi", "te", "ta", "en"] as const) {
      for (const script of ["deva", "latn"] as const) {
        const tag = languageTag(language, script);
        expect(languageOf(tag)).toBe(language);
        if (language === "hi") expect(scriptOf(tag)).toBe(script);
      }
    }
  });

  it("labels a tag the recogniser reports, including one not in the table", () => {
    expect(tagLabel("hi-Latn")).toMatch(/Latin/);
    expect(tagLabel("bn-IN")).toBe("bn-IN");
    expect(tagLabel(null)).toMatch(/not identified/i);
  });
});

describe("persisted per user", () => {
  it("keys on the employee subject, so a shared workstation does not cross users", () => {
    expect(scriptKey("sso|asha@example.com")).not.toBe(scriptKey("sso|ravi@example.com"));
    // The subject is issuer-qualified and goes into a storage key, so it is encoded.
    expect(scriptKey("sso|a/b")).not.toContain("/");
  });

  it("round-trips, and one employee's choice is not another's", () => {
    const store = memoryStorage();
    saveScriptPref("asha", "latn", store);
    expect(loadScriptPref("asha", store)).toBe("latn");
    expect(loadScriptPref("ravi", store)).toBe("deva");
  });

  it("defaults to Devanagari for an unset or corrupt value", () => {
    expect(loadScriptPref("x", memoryStorage())).toBe("deva");
    expect(loadScriptPref("x", memoryStorage({ [scriptKey("x")]: "klingon" }))).toBe("deva");
  });

  it("survives storage being blocked entirely", () => {
    expect(loadScriptPref("x", throwingStorage())).toBe("deva");
    expect(() => saveScriptPref("x", "latn", throwingStorage())).not.toThrow();
  });
});
