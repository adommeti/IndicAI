import { describe, expect, it } from "vitest";
import {
  UNMEASURED,
  bucketLabel,
  bucketsOf,
  formatCount,
  formatPrecision,
  formatRate,
  groupOverTime,
  measuredCount,
  overallPrecision,
  sparklineRuns,
} from "../src/lib/metrics";
import type { CategoryPrecision, PrecisionPoint } from "../src/lib/types";

function cat(over: Partial<CategoryPrecision> = {}): CategoryPrecision {
  return { category: "c", confirmed: 0, false_positive: 0, decided: 0, precision: null, ...over };
}

function point(over: Partial<PrecisionPoint> = {}): PrecisionPoint {
  // `bucket` is the granularity the server was asked for, identical on every
  // point; `bucket_start` is what identifies the point on the axis. An earlier
  // version of this helper put the label in `bucket`, which is what the real
  // API does not do -- so the fixtures agreed with the UI and both disagreed
  // with the server.
  return {
    ...cat(),
    bucket: "week",
    bucket_start: "2026-08-31T00:00:00+00:00",
    bucket_end: "2026-09-07T00:00:00+00:00",
    flags: 0,
    ...over,
  };
}

describe("formatting a measurement that may not exist", () => {
  // The uc3/P1 defect this guards: a zero denominator reported as a passing 0.0.
  it("renders null as the word unmeasured, never a number", () => {
    expect(formatPrecision(null)).toBe(UNMEASURED);
    expect(formatRate(null)).toBe(UNMEASURED);
    expect(formatPrecision(null)).not.toMatch(/\d/);
  });

  it("renders a genuinely measured zero as 0.0%, because that is a real result", () => {
    expect(formatPrecision(0)).toBe("0.0%");
  });

  it("renders a measured one as 100.0%", () => expect(formatPrecision(1)).toBe("100.0%"));

  it("rounds to the requested precision", () => {
    expect(formatPrecision(0.8642)).toBe("86.4%");
    expect(formatPrecision(0.8642, 0)).toBe("86%");
  });

  it("does not launder NaN into a number", () => {
    expect(formatPrecision(Number.NaN)).toBe(UNMEASURED);
  });

  it("groups counts", () => expect(formatCount(12345)).toBe("12,345"));
});

describe("overallPrecision", () => {
  it("is null — not zero — when nothing has been decided", () => {
    const overall = overallPrecision([cat(), cat({ category: "d" })]);
    expect(overall?.decided).toBe(0);
    expect(overall?.precision).toBeNull();
  });

  it("sums across categories", () => {
    const overall = overallPrecision([
      cat({ confirmed: 8, false_positive: 2, decided: 10, precision: 0.8 }),
      cat({ category: "d", confirmed: 2, false_positive: 8, decided: 10, precision: 0.2 }),
    ]);
    expect(overall).toMatchObject({ confirmed: 10, false_positive: 10, decided: 20, precision: 0.5 });
  });

  it("is null for an empty table rather than a fabricated row", () => {
    expect(overallPrecision([])).toBeNull();
  });

  it("reports a real zero when every decided flag was a false positive", () => {
    expect(overallPrecision([cat({ false_positive: 4, decided: 4, precision: 0 })])?.precision).toBe(0);
  });
});

describe("groupOverTime", () => {
  // Three consecutive Mondays. These differ by `bucket_start`, which is what
  // the server varies; `bucket` stays "week" on every point, as it does in a
  // real response.
  const W36 = "2026-08-31T00:00:00+00:00";
  const W37 = "2026-09-07T00:00:00+00:00";
  const W38 = "2026-09-14T00:00:00+00:00";
  const points = [
    point({ category: "a", bucket_start: W36, precision: 0.5, decided: 2 }),
    point({ category: "a", bucket_start: W38, precision: 1, decided: 1 }),
    point({ category: "b", bucket_start: W37, precision: 0.25, decided: 4 }),
  ];

  it("lists every bucket that appears anywhere, in order", () => {
    expect(bucketsOf(points)).toEqual([W36, W37, W38]);
  });

  it("does not collapse the axis when every point shares one granularity", () => {
    // The defect this pins: keying the axis on `bucket` -- "week" on all three
    // points -- rendered one column labelled "week" instead of three weeks.
    expect(new Set(points.map((p) => p.bucket)).size).toBe(1);
    expect(bucketsOf(points)).toHaveLength(3);
  });

  it("labels a weekly bucket by its ISO week, not by its granularity", () => {
    expect(bucketLabel(point({ bucket: "week", bucket_start: W36 }))).toBe("2026-W36");
    expect(bucketLabel(point({ bucket: "month", bucket_start: W36 }))).toBe("2026-08");
    expect(bucketLabel(point({ bucket: "day", bucket_start: W36 }))).toBe("2026-08-31");
  });

  it("aligns each category on the full axis", () => {
    const series = groupOverTime(points);
    expect(series.map((s) => s.category)).toEqual(["a", "b"]);
    expect(series[0]?.points.map((p) => p.bucket_start)).toEqual([W36, W37, W38]);
  });

  it("fills a missing bucket as unmeasured, not as zero", () => {
    const series = groupOverTime(points);
    expect(series[0]?.points[1]?.precision).toBeNull();
    expect(series[0]?.points[1]?.decided).toBe(0);
    expect(measuredCount(series[0]?.points ?? [])).toBe(2);
  });

  it("returns nothing for an empty series", () => expect(groupOverTime([])).toEqual([]));
});

describe("sparklineRuns", () => {
  it("breaks the line at an unmeasured bucket instead of drawing through it", () => {
    const runs = sparklineRuns([1, null, 0.5], 100, 40);
    expect(runs).toHaveLength(2);
    expect(runs.every((run) => run.single)).toBe(true);
  });

  it("keeps contiguous measured values in one polyline", () => {
    const runs = sparklineRuns([1, 0.8, 0.6], 100, 40);
    expect(runs).toHaveLength(1);
    expect(runs[0]?.points.split(" ")).toHaveLength(3);
    expect(runs[0]?.single).toBe(false);
  });

  it("draws nothing when nothing is measured", () => {
    expect(sparklineRuns([null, null], 100, 40)).toEqual([]);
  });

  it("puts a higher precision higher on the canvas", () => {
    const [top] = sparklineRuns([1], 100, 40);
    const [bottom] = sparklineRuns([0], 100, 40);
    const y = (run?: { points: string }) => Number(run?.points.split(",")[1]);
    expect(y(top)).toBeLessThan(y(bottom));
  });

  it("does not divide by zero on a single-bucket axis", () => {
    expect(sparklineRuns([0.5], 100, 40)[0]?.points).toMatch(/^\d+\.\d\d,\d+\.\d\d$/);
  });
});
