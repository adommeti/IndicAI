import { describe, expect, it } from "vitest";
import {
  MIN_OVERRIDE_REASON,
  applyFilter,
  blockedFromBulk,
  bulkApprovable,
  progress,
  summarise,
  timecode,
} from "../src/lib/review";
import type { Segment } from "../src/lib/types";

function segment(over: Partial<Segment> = {}): Segment {
  return {
    seg_id: 1,
    start_ms: 0,
    end_ms: 5000,
    source_text: "Report phishing.",
    locked: false,
    locked_id: null,
    translation: "अनुवाद",
    backtranslation: null,
    qa_score: null,
    qa_reason: null,
    glossary_hits: [],
    glossary_misses: [],
    change_log: [],
    approved: false,
    approved_by: null,
    locked_mismatch: false,
    flagged: false,
    stage_version: 1,
    ...over,
  };
}

describe("filters", () => {
  const segments = [
    segment({ seg_id: 1 }),
    segment({ seg_id: 2, flagged: true }),
    segment({ seg_id: 3, approved: true }),
  ];

  it("shows everything by default", () => {
    expect(applyFilter(segments, "all")).toHaveLength(3);
  });

  it("flagged keeps only flagged rows", () => {
    expect(applyFilter(segments, "flagged").map((s) => s.seg_id)).toEqual([2]);
  });

  it("unapproved hides what is already done", () => {
    expect(applyFilter(segments, "unapproved").map((s) => s.seg_id)).toEqual([1, 2]);
  });
});

describe("approve all remaining", () => {
  const segments = [
    segment({ seg_id: 1 }),
    segment({ seg_id: 2, approved: true }),
    segment({ seg_id: 3, locked: true, locked_id: "lock-a", locked_mismatch: true }),
  ];

  it("never bulk-approves a LOCKED mismatch", () => {
    // The whole point of the control: a bulk action cannot be the thing that
    // sets aside a compliance statement.
    expect(bulkApprovable(segments).map((s) => s.seg_id)).toEqual([1]);
    expect(blockedFromBulk(segments).map((s) => s.seg_id)).toEqual([3]);
  });

  it("skips segments that are already approved", () => {
    expect(bulkApprovable(segments).some((s) => s.approved)).toBe(false);
  });
});

describe("summarise", () => {
  it("counts what the header shows", () => {
    const summary = summarise([
      segment({ seg_id: 1, approved: true, qa_score: 5 }),
      segment({ seg_id: 2, flagged: true, glossary_misses: ["MFA", "VPN"], qa_score: 2 }),
      segment({ seg_id: 3, locked: true, locked_mismatch: true }),
    ]);
    expect(summary).toMatchObject({
      segments: 3,
      approved: 1,
      flagged: 1,
      locked: 1,
      locked_mismatches: 1,
      glossary_misses: 2,
    });
    expect(summary.fidelity_mean).toBeCloseTo(3.5);
  });

  it("reports no fidelity rather than zero when the judge has not run", () => {
    expect(summarise([segment()]).fidelity_mean).toBeNull();
  });

  it("progress is a whole percent of segments approved", () => {
    expect(progress(summarise([segment({ approved: true }), segment({ seg_id: 2 })]))).toBe(50);
    expect(progress(summarise([]))).toBe(0);
  });
});

describe("timecode", () => {
  it("renders minutes and padded seconds", () => {
    expect(timecode(0)).toBe("0:00");
    expect(timecode(65_000)).toBe("1:05");
    expect(timecode(600_000)).toBe("10:00");
  });
});

it("the override minimum is a shared constant, not a magic number", () => {
  expect(MIN_OVERRIDE_REASON).toBe(10);
});
