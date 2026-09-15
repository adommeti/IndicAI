import { describe, expect, it } from "vitest";
import {
  DISPOSITION_OPTIONS,
  EMPTY_FILTER,
  applyDisposition,
  dispositionLabel,
  filterQueue,
  nextFlagId,
  noteAdvice,
  noteProblem,
  queueCounts,
  severityRank,
  shortId,
  sortQueue,
  timecode,
} from "../src/lib/queue";
import type { FlagSummary, Severity } from "../src/lib/types";

function flag(over: Partial<FlagSummary> = {}): FlagSummary {
  return {
    flag_id: "f1",
    call_id: "call-0001",
    category: "undisclosed_commission",
    severity: "low",
    speaker: "agent",
    start_ms: 0,
    created_at: "2026-09-01T10:00:00Z",
    disposition: null,
    ...over,
  };
}

describe("queue ordering", () => {
  it("ranks high before medium before low", () => {
    const order: Severity[] = ["low", "high", "medium"];
    expect([...order].sort((a, b) => severityRank(a) - severityRank(b))).toEqual([
      "high",
      "medium",
      "low",
    ]);
  });

  it("sorts severity first, then oldest first within a severity", () => {
    const sorted = sortQueue([
      flag({ flag_id: "c", severity: "low", created_at: "2026-09-01T09:00:00Z" }),
      flag({ flag_id: "b", severity: "high", created_at: "2026-09-02T09:00:00Z" }),
      flag({ flag_id: "a", severity: "high", created_at: "2026-09-01T09:00:00Z" }),
      flag({ flag_id: "d", severity: "medium", created_at: "2026-09-03T09:00:00Z" }),
    ]);
    expect(sorted.map((f) => f.flag_id)).toEqual(["a", "b", "d", "c"]);
  });

  it("does not mutate the list it was given", () => {
    const input = [flag({ flag_id: "x", severity: "low" }), flag({ flag_id: "y", severity: "high" })];
    sortQueue(input);
    expect(input.map((f) => f.flag_id)).toEqual(["x", "y"]);
  });
});

describe("filters", () => {
  const flags = [
    flag({ flag_id: "a", severity: "high" }),
    flag({ flag_id: "b", severity: "low", disposition: "confirmed" }),
    flag({ flag_id: "c", severity: "high", disposition: "escalated" }),
  ];

  it("passes everything through by default", () => {
    expect(filterQueue(flags, EMPTY_FILTER)).toHaveLength(3);
  });

  it("narrows by severity", () => {
    expect(filterQueue(flags, { severity: "high", undispositioned: false }).map((f) => f.flag_id)).toEqual([
      "a",
      "c",
    ]);
  });

  it("open-only keeps flags with no ruling at all", () => {
    expect(filterQueue(flags, { severity: "", undispositioned: true }).map((f) => f.flag_id)).toEqual(["a"]);
  });

  it("treats a non-decided ruling as still dispositioned", () => {
    // escalated is not a decided OUTCOME for precision, but the flag has been
    // ruled on; it must not reappear in the "open" queue.
    expect(filterQueue([flag({ disposition: "escalated" })], { severity: "", undispositioned: true })).toHaveLength(0);
  });
});

describe("counts", () => {
  it("counts by severity and by openness", () => {
    expect(
      queueCounts([
        flag({ severity: "high" }),
        flag({ severity: "high", disposition: "confirmed" }),
        flag({ severity: "medium" }),
        flag({ severity: "low", disposition: "false_positive" }),
      ]),
    ).toEqual({ total: 4, high: 2, medium: 1, low: 1, open: 2 });
  });
});

describe("applyDisposition", () => {
  const flags = [flag({ flag_id: "a" }), flag({ flag_id: "b", disposition: "confirmed" })];

  it("replaces only the latest ruling on the named flag", () => {
    const next = applyDisposition(flags, "b", "false_positive");
    expect(next.map((f) => f.disposition)).toEqual([null, "false_positive"]);
  });

  it("leaves the original list untouched", () => {
    applyDisposition(flags, "a", "confirmed");
    expect(flags[0]?.disposition).toBeNull();
  });
});

describe("nextFlagId", () => {
  const flags = [flag({ flag_id: "a" }), flag({ flag_id: "b" })];
  it("walks forward", () => expect(nextFlagId(flags, "a")).toBe("b"));
  it("stops at the end", () => expect(nextFlagId(flags, "b")).toBeNull());
  it("starts at the top when the current flag is gone", () => expect(nextFlagId(flags, "zz")).toBe("a"));
  it("has nothing to offer on an empty queue", () => expect(nextFlagId([], null)).toBeNull());
});

describe("timecode", () => {
  it("formats under an hour", () => expect(timecode(95_000)).toBe("1:35"));
  it("pads minutes past an hour", () => expect(timecode(3_725_000)).toBe("1:02:05"));
  it("clamps negatives", () => expect(timecode(-10)).toBe("0:00"));
});

describe("shortId", () => {
  it("leaves short ids alone", () => expect(shortId("abc")).toBe("abc"));
  it("elides long ids", () => expect(shortId("0123456789abcdef")).toBe("01234567…"));
});

describe("dispositions", () => {
  it("counts only confirmed and false_positive as decided outcomes", () => {
    expect(DISPOSITION_OPTIONS.filter((o) => o.decided).map((o) => o.value)).toEqual([
      "confirmed",
      "false_positive",
    ]);
  });

  it("labels an unruled flag as open", () => expect(dispositionLabel(null)).toBe("Open"));

  it("blocks only a note past the cap", () => {
    expect(noteProblem("fine")).toBeNull();
    expect(noteProblem("x".repeat(4001))).toMatch(/4000/);
  });

  it("advises but does not block an empty note", () => {
    expect(noteProblem("")).toBeNull();
    expect(noteAdvice("")).toMatch(/permanent/);
    expect(noteAdvice("checked")).toBeNull();
  });
});
