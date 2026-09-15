import type { CategoryPrecision, PrecisionPoint } from "./types";

/** "unmeasured" is a real state, not a rounding of zero.
 *
 *  precision is null when nothing has been decided; rate is null when nothing has
 *  been sampled. Rendering either as 0% (or 100%) is the exact defect the uc3/P1
 *  review caught, so the null path is separate from the zero path here and is
 *  tested as such. A measured 0.0 IS shown as 0.0% — that is a detector with no
 *  true positives, and hiding it would be the same lie in the other direction. */
export const UNMEASURED = "unmeasured";

export function formatPrecision(precision: number | null, digits = 1): string {
  if (precision === null || Number.isNaN(precision)) return UNMEASURED;
  return `${(precision * 100).toFixed(digits)}%`;
}

export function formatRate(rate: number | null, digits = 1): string {
  return formatPrecision(rate, digits);
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat("en-IN").format(value);
}

/** Roll the per-category rows up without ever inventing a denominator. */
export function overallPrecision(rows: readonly CategoryPrecision[]): CategoryPrecision | null {
  if (rows.length === 0) return null;
  const confirmed = rows.reduce((n, row) => n + row.confirmed, 0);
  const falsePositive = rows.reduce((n, row) => n + row.false_positive, 0);
  const decided = confirmed + falsePositive;
  return {
    category: "All categories",
    confirmed,
    false_positive: falsePositive,
    decided,
    precision: decided === 0 ? null : confirmed / decided,
  };
}

export interface CategorySeries {
  category: string;
  points: PrecisionPoint[];
}

export function bucketsOf(points: readonly PrecisionPoint[]): string[] {
  return [...new Set(points.map((point) => point.bucket))].sort((a, b) => a.localeCompare(b));
}

/** One series per category, aligned on the full bucket axis so a category that
 *  went quiet for a week shows a gap instead of a straight line across it. */
export function groupOverTime(points: readonly PrecisionPoint[]): CategorySeries[] {
  const axis = bucketsOf(points);
  const byCategory = new Map<string, Map<string, PrecisionPoint>>();
  for (const point of points) {
    const series = byCategory.get(point.category) ?? new Map<string, PrecisionPoint>();
    series.set(point.bucket, point);
    byCategory.set(point.category, series);
  }
  return [...byCategory.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([category, series]) => ({
      category,
      points: axis.map(
        (bucket) =>
          series.get(bucket) ?? {
            bucket,
            category,
            confirmed: 0,
            false_positive: 0,
            decided: 0,
            precision: null,
          },
      ),
    }));
}

/** Contiguous runs of measured values, as polyline point strings. A run of one
 *  is returned too, so the caller can draw a dot where a line would vanish. */
export function sparklineRuns(
  values: readonly (number | null)[],
  width: number,
  height: number,
  pad = 2,
): { points: string; single: boolean }[] {
  const runs: { points: string; single: boolean }[] = [];
  const span = Math.max(1, values.length - 1);
  const x = (index: number) => pad + (index * (width - pad * 2)) / span;
  const y = (value: number) => height - pad - value * (height - pad * 2);

  let current: string[] = [];
  const flush = () => {
    if (current.length > 0) runs.push({ points: current.join(" "), single: current.length === 1 });
    current = [];
  };
  values.forEach((value, index) => {
    if (value === null || Number.isNaN(value)) {
      flush();
      return;
    }
    current.push(`${x(index).toFixed(2)},${y(value).toFixed(2)}`);
  });
  flush();
  return runs;
}

export function measuredCount(points: readonly PrecisionPoint[]): number {
  return points.filter((point) => point.precision !== null).length;
}
