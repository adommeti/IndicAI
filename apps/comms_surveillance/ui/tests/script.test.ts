import { describe, expect, it } from "vitest";
import { loadScriptPref, pickText, saveScriptPref, scriptKey } from "../src/lib/script";
import type { TranscriptSegment } from "../src/lib/types";

function segment(over: Partial<TranscriptSegment> = {}): TranscriptSegment {
  return {
    seg_id: 1,
    speaker: "agent",
    start_ms: 0,
    end_ms: 4000,
    text: "गारंटीड रिटर्न",
    text_roman: "guaranteed return",
    ...over,
  };
}

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
  return {
    get length(): number {
      throw new Error("blocked");
    },
    clear: () => {
      throw new Error("blocked");
    },
    getItem: () => {
      throw new Error("blocked");
    },
    key: () => {
      throw new Error("blocked");
    },
    removeItem: () => {
      throw new Error("blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
  } as unknown as Storage;
}

describe("pickText", () => {
  it("shows the native script by default", () => {
    expect(pickText(segment(), "native")).toEqual({
      text: "गारंटीड रिटर्न",
      script: "native",
      fellBack: false,
    });
  });

  it("shows the romanisation when Latin is preferred", () => {
    expect(pickText(segment(), "latn")).toEqual({
      text: "guaranteed return",
      script: "latn",
      fellBack: false,
    });
  });

  it("falls back to the native text — and says so — when there is no romanisation", () => {
    expect(pickText(segment({ text_roman: null }), "latn")).toEqual({
      text: "गारंटीड रिटर्न",
      script: "native",
      fellBack: true,
    });
  });

  it("treats a blank romanisation as missing rather than showing an empty line", () => {
    expect(pickText(segment({ text_roman: "   " }), "latn").fellBack).toBe(true);
  });

  it("leaves Tamil and Telugu alone in native mode", () => {
    expect(pickText(segment({ text: "உறுதியான லாபம்" }), "native").text).toBe("உறுதியான லாபம்");
    expect(pickText(segment({ text: "గ్యారెంటీ రాబడి" }), "native").text).toBe("గ్యారెంటీ రాబడి");
  });
});

describe("persistence", () => {
  it("keys the preference by identity so a shared desk does not cross users", () => {
    expect(scriptKey("asha@example.com")).not.toBe(scriptKey("ravi@example.com"));
  });

  it("round-trips through storage", () => {
    const store = memoryStorage();
    saveScriptPref("asha@example.com", "latn", store);
    expect(loadScriptPref("asha@example.com", store)).toBe("latn");
    expect(loadScriptPref("ravi@example.com", store)).toBe("native");
  });

  it("defaults to native for an unset or corrupt value", () => {
    expect(loadScriptPref("x", memoryStorage())).toBe("native");
    expect(loadScriptPref("x", memoryStorage({ [scriptKey("x")]: "klingon" }))).toBe("native");
  });

  it("survives storage being blocked entirely", () => {
    expect(loadScriptPref("x", throwingStorage())).toBe("native");
    expect(() => saveScriptPref("x", "latn", throwingStorage())).not.toThrow();
  });
});
