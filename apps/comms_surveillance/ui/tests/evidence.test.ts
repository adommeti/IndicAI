import { describe, expect, it } from "vitest";
import {
  evidenceLocated,
  hasEvidence,
  markEvidence,
  renderingOrAbsent,
} from "../src/lib/evidence";

const HINDI = "मैं आपको गारंटीड रिटर्न दे सकता हूँ, बस नकद में दीजिए।";
const TAMIL = "இது உறுதியான லாபம், கவலைப்பட வேண்டாம்.";

describe("markEvidence", () => {
  it("splits a Devanagari line around the flagged span", () => {
    const parts = markEvidence(HINDI, "गारंटीड रिटर्न");
    expect(hasEvidence(parts)).toBe(true);
    expect(parts.filter((p) => p.evidence).map((p) => p.text)).toEqual(["गारंटीड रिटर्न"]);
    expect(parts.map((p) => p.text).join("")).toBe(HINDI);
  });

  it("works the same on Tamil", () => {
    const parts = markEvidence(TAMIL, "உறுதியான லாபம்");
    expect(parts.filter((p) => p.evidence).map((p) => p.text)).toEqual(["உறுதியான லாபம்"]);
  });

  it("tolerates whitespace differing between the span and the transcript", () => {
    const parts = markEvidence("guaranteed   returns every month", "guaranteed returns");
    expect(parts.filter((p) => p.evidence).map((p) => p.text)).toEqual(["guaranteed   returns"]);
  });

  it("marks every occurrence, not just the first", () => {
    const parts = markEvidence("cash only, cash only", "cash only");
    expect(parts.filter((p) => p.evidence)).toHaveLength(2);
  });

  it("marks nothing at all when the span is not in the line", () => {
    const parts = markEvidence(HINDI, "something else entirely");
    expect(hasEvidence(parts)).toBe(false);
    expect(parts).toEqual([{ text: HINDI, evidence: false }]);
  });

  it("is case-sensitive: a near-miss is not the evidence", () => {
    expect(hasEvidence(markEvidence("Guaranteed returns", "guaranteed returns"))).toBe(false);
  });

  it("treats regex metacharacters in the span as literal text", () => {
    const parts = markEvidence("call me on (+91) 98xxx", "(+91)");
    expect(parts.filter((p) => p.evidence).map((p) => p.text)).toEqual(["(+91)"]);
  });

  it("never rebuilds the line differently from the original", () => {
    const line = "a b a b a";
    expect(markEvidence(line, "b a").map((p) => p.text).join("")).toBe(line);
  });

  it("marks nothing for an empty or whitespace-only span", () => {
    expect(hasEvidence(markEvidence(HINDI, ""))).toBe(false);
    expect(hasEvidence(markEvidence(HINDI, "   "))).toBe(false);
  });
});

describe("evidenceLocated", () => {
  const lines = [HINDI, TAMIL];

  it("is true when some rendered line contains the span", () => {
    expect(evidenceLocated(lines, "गारंटीड रिटर्न")).toBe(true);
  });

  it("is false when no line does — which is what drives the honest notice", () => {
    expect(evidenceLocated(lines, "guaranteed returns")).toBe(false);
  });

  it("is false for an empty span rather than trivially true", () => {
    expect(evidenceLocated(lines, "")).toBe(false);
  });

  it("does not leak regex state between lines", () => {
    // A /g/ regex carries lastIndex; two calls in a row must agree.
    expect(evidenceLocated(["cash only", "cash only"], "cash only")).toBe(true);
    expect(evidenceLocated(["cash only", "cash only"], "cash only")).toBe(true);
  });
});

describe("renderingOrAbsent", () => {
  it("passes a real rendering through unchanged", () => {
    const { text, absent } = renderingOrAbsent("Tell the client it is guaranteed.");
    expect(absent).toBe(false);
    expect(text).toBe("Tell the client it is guaranteed.");
  });

  it("says so rather than leaving an empty box under the heading", () => {
    // `render_english` returns "" when the translation call fails, which the
    // detector treats as non-fatal. The reviewer must be able to tell that
    // apart from a pane that failed to load.
    for (const empty of ["", "   ", "\n"]) {
      const { text, absent } = renderingOrAbsent(empty);
      expect(absent).toBe(true);
      expect(text).not.toHaveLength(0);
    }
  });
});
