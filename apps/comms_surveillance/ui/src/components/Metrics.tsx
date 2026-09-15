import {
  UNMEASURED,
  bucketLabel,
  bucketsOf,
  formatCount,
  formatPrecision,
  groupOverTime,
  measuredCount,
  overallPrecision,
  sparklineRuns,
} from "../lib/metrics";
import type { ChainStatus, FalseNegativeEstimate, PrecisionMetrics } from "../lib/types";
import { EmptyState } from "./States";

/** Nothing on this screen may render a missing measurement as a number.
 *  `unmeasured` is typeset as a word, in muted ink, and is never a 0 or a dash
 *  that could be mistaken for a value. */
function Measure({ value }: { value: string }) {
  if (value === UNMEASURED)
    return (
      <span className="font-mono text-xs uppercase tracking-wider text-muted" title="No decided outcomes yet — not a score of zero">
        {UNMEASURED}
      </span>
    );
  return <span className="font-mono text-sm font-semibold tabular-nums">{value}</span>;
}

export function PrecisionTable({ metrics }: { metrics: PrecisionMetrics }) {
  const overall = overallPrecision(metrics.by_category);
  const rows = [...metrics.by_category].sort((a, b) => a.category.localeCompare(b.category));

  return (
    <section aria-labelledby="precision-heading" className="rounded border border-border bg-surface">
      <div className="border-b border-border px-4 py-3">
        <h2 id="precision-heading" className="text-sm font-semibold">
          Precision by category
        </h2>
        <p className="mt-1 max-w-prose text-xs text-muted">
          Confirmed ÷ (confirmed + false positive), from the latest disposition on each flag.
          “Needs more context” and “escalated” are not decided outcomes and are counted on neither
          side.
        </p>
      </div>

      {rows.length === 0 ? (
        <div className="p-4">
          <EmptyState title="No categories yet" body="No flag has been dispositioned, so there is nothing to compute." />
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[34rem] text-sm" data-testid="precision-table">
            <caption className="sr-only">Precision by detection category</caption>
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted">
                <th scope="col" className="px-4 py-2 font-semibold">Category</th>
                <th scope="col" className="px-4 py-2 text-right font-semibold">Confirmed</th>
                <th scope="col" className="px-4 py-2 text-right font-semibold">False pos.</th>
                <th scope="col" className="px-4 py-2 text-right font-semibold">Decided</th>
                <th scope="col" className="px-4 py-2 text-right font-semibold">Precision</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((row) => (
                <tr key={row.category} data-testid={`precision-${row.category}`}>
                  <th scope="row" className="px-4 py-2 text-left font-medium">{row.category}</th>
                  <td className="px-4 py-2 text-right font-mono tabular-nums">{formatCount(row.confirmed)}</td>
                  <td className="px-4 py-2 text-right font-mono tabular-nums">{formatCount(row.false_positive)}</td>
                  <td className="px-4 py-2 text-right font-mono tabular-nums">{formatCount(row.decided)}</td>
                  <td className="px-4 py-2 text-right"><Measure value={formatPrecision(row.precision)} /></td>
                </tr>
              ))}
            </tbody>
            {overall ? (
              <tfoot>
                <tr className="border-t-2 border-border-strong font-semibold">
                  <th scope="row" className="px-4 py-2 text-left">{overall.category}</th>
                  <td className="px-4 py-2 text-right font-mono tabular-nums">{formatCount(overall.confirmed)}</td>
                  <td className="px-4 py-2 text-right font-mono tabular-nums">{formatCount(overall.false_positive)}</td>
                  <td className="px-4 py-2 text-right font-mono tabular-nums">{formatCount(overall.decided)}</td>
                  <td className="px-4 py-2 text-right"><Measure value={formatPrecision(overall.precision)} /></td>
                </tr>
              </tfoot>
            ) : null}
          </table>
        </div>
      )}

      {metrics.unmeasured.length > 0 ? (
        <p className="border-t border-border px-4 py-3 text-xs text-muted" data-testid="unmeasured-list">
          <span className="font-semibold uppercase tracking-wider">Unmeasured:</span>{" "}
          {metrics.unmeasured.join(", ")} — reported as unmeasured rather than given a placeholder.
        </p>
      ) : null}
    </section>
  );
}

const SPARK_W = 200;
const SPARK_H = 40;

/** Precision over time, as small multiples: one row per category, so a single
 *  category degrading is visible without hunting through an overlaid chart.
 *  Gaps are gaps — a bucket with nothing decided breaks the line instead of
 *  being interpolated through. The table beside it carries the same numbers for
 *  anyone reading with a screen reader or without the graphic. */
export function PrecisionOverTime({ metrics }: { metrics: PrecisionMetrics }) {
  const series = groupOverTime(metrics.over_time);
  const axis = bucketsOf(metrics.over_time);
  // The axis is keyed on `bucket_start`; readers get the ISO week.
  const labelFor = (bucketStart: string) => {
    const point = metrics.over_time.find((row) => row.bucket_start === bucketStart);
    return point ? bucketLabel(point) : bucketStart;
  };

  if (series.length === 0)
    return (
      <section aria-labelledby="overtime-heading" className="rounded border border-border bg-surface p-4">
        <h2 id="overtime-heading" className="text-sm font-semibold">Precision over time</h2>
        <p className="mt-2 text-sm text-muted">
          No time series yet: precision over time needs decided outcomes in at least one bucket.
        </p>
      </section>
    );

  return (
    <section aria-labelledby="overtime-heading" className="rounded border border-border bg-surface">
      <div className="border-b border-border px-4 py-3">
        <h2 id="overtime-heading" className="text-sm font-semibold">Precision over time</h2>
        <p className="mt-1 text-xs text-muted">
          {axis.length} buckets, {labelFor(axis[0] ?? "")} → {labelFor(axis[axis.length - 1] ?? "")}. A break in a line is a bucket
          with nothing decided, not a drop to zero.
        </p>
      </div>
      <ul className="divide-y divide-border" data-testid="over-time">
        {series.map((row) => {
          const values = row.points.map((point) => point.precision);
          const runs = sparklineRuns(values, SPARK_W, SPARK_H);
          const last = [...values].reverse().find((value) => value !== null) ?? null;
          return (
            <li key={row.category} className="flex flex-wrap items-center gap-4 px-4 py-3">
              <span className="w-44 shrink-0 text-sm font-medium">{row.category}</span>
              <svg
                width={SPARK_W}
                height={SPARK_H}
                viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
                role="img"
                aria-label={`${row.category}: ${measuredCount(row.points)} of ${axis.length} buckets measured, latest ${formatPrecision(last)}`}
                className="shrink-0"
              >
                <line x1="0" y1={SPARK_H - 2} x2={SPARK_W} y2={SPARK_H - 2} stroke="var(--border)" strokeWidth="1" />
                {runs.map((run, index) =>
                  run.single ? (
                    <circle
                      key={index}
                      cx={Number(run.points.split(",")[0])}
                      cy={Number(run.points.split(",")[1])}
                      r="2.5"
                      fill="var(--accent)"
                    />
                  ) : (
                    <polyline
                      key={index}
                      points={run.points}
                      fill="none"
                      stroke="var(--accent)"
                      strokeWidth="2"
                      strokeLinejoin="round"
                      strokeLinecap="round"
                    />
                  ),
                )}
              </svg>
              <span className="font-mono text-xs text-muted">
                {measuredCount(row.points)}/{axis.length} measured
              </span>
              <span className="ml-auto"><Measure value={formatPrecision(last)} /></span>
            </li>
          );
        })}
      </ul>
      <div className="overflow-x-auto border-t border-border">
        <table className="w-full min-w-[34rem] text-xs">
          <caption className="px-4 py-2 text-left text-xs text-muted">
            The same series as numbers.
          </caption>
          <thead>
            <tr className="border-b border-border text-left uppercase tracking-wider text-muted">
              <th scope="col" className="px-4 py-2 font-semibold">Category</th>
              {axis.map((bucketStart) => (
                <th key={bucketStart} scope="col" className="px-3 py-2 text-right font-semibold">
                  {labelFor(bucketStart)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {series.map((row) => (
              <tr key={row.category}>
                <th scope="row" className="px-4 py-1.5 text-left font-medium">{row.category}</th>
                {row.points.map((point) => (
                  <td key={point.bucket_start} className="px-3 py-1.5 text-right">
                    <Measure value={formatPrecision(point.precision, 0)} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function FalseNegativePanel({ estimate }: { estimate: FalseNegativeEstimate }) {
  return (
    <section aria-labelledby="fn-heading" className="rounded border border-border bg-surface p-4">
      <h2 id="fn-heading" className="text-sm font-semibold">False-negative estimate</h2>
      <p className="mt-1 max-w-prose text-xs text-muted">
        From the random QA sample: how much the detector is missing, not how much it catches.
      </p>
      <dl className="mt-3 grid grid-cols-3 gap-4">
        <div>
          <dt className="text-xs uppercase tracking-wider text-muted">Sampled</dt>
          <dd className="mt-0.5 font-mono text-lg tabular-nums">{formatCount(estimate.sampled)}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wider text-muted">Missed</dt>
          <dd className="mt-0.5 font-mono text-lg tabular-nums">{formatCount(estimate.missed)}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wider text-muted">Rate</dt>
          <dd className="mt-0.5" data-testid="fn-rate">
            <Measure value={formatPrecision(estimate.rate)} />
          </dd>
        </div>
      </dl>
      {estimate.unmeasured || estimate.rate === null ? (
        <p className="mt-3 rounded border border-border bg-surface-2 p-2 text-xs text-muted">
          Nothing has been sampled and ruled on yet, so there is no rate. This is reported as
          unmeasured — it is not a rate of zero and must not be read as one.
        </p>
      ) : null}
    </section>
  );
}

export function ChainStatusPanel({ status }: { status: ChainStatus }) {
  return (
    <section aria-labelledby="chain-heading" className="rounded border border-border bg-surface">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border px-4 py-3">
        <h2 id="chain-heading" className="text-sm font-semibold">Audit chain verification</h2>
        <p className="font-mono text-xs text-muted">checked {status.checked_at}</p>
      </div>

      <p
        className={`flex items-center gap-2 border-b border-border px-4 py-3 text-sm font-semibold ${
          status.ok ? "text-ok" : "text-danger"
        }`}
        data-testid="chain-verdict"
      >
        <span aria-hidden="true" className="font-mono">{status.ok ? "[ OK ]" : "[ !! ]"}</span>
        {status.ok
          ? "Every chain verified; no breaks found."
          : `${status.breaks} break${status.breaks === 1 ? "" : "s"} found. The affected rows are listed below.`}
      </p>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[34rem] text-sm" data-testid="chain-table">
          <caption className="sr-only">Hash-chain status per audited table</caption>
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted">
              <th scope="col" className="px-4 py-2 font-semibold">Table</th>
              <th scope="col" className="px-4 py-2 text-right font-semibold">Rows</th>
              <th scope="col" className="px-4 py-2 font-semibold">Chain</th>
              <th scope="col" className="px-4 py-2 font-semibold">Anchor</th>
              <th scope="col" className="px-4 py-2 font-semibold">Detail</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {status.tables.map((table) => (
              <tr key={table.table}>
                <th scope="row" className="px-4 py-2 text-left font-mono text-xs">{table.table}</th>
                <td className="px-4 py-2 text-right font-mono tabular-nums">{formatCount(table.rows)}</td>
                <td className={`px-4 py-2 font-mono text-xs ${table.ok ? "text-ok" : "text-danger"}`}>
                  {table.ok ? "✓ intact" : "✗ broken"}
                </td>
                <td
                  className={`px-4 py-2 font-mono text-xs ${
                    table.anchor_ok === null
                      ? "text-muted"
                      : table.anchor_ok
                        ? "text-ok"
                        : "text-danger"
                  }`}
                >
                  {/* Three states, not two. `null` means this chain has never
                      been anchored -- a new deployment before the first nightly
                      verify. Folding that into "mismatch" would paint a red
                      break across a healthy dashboard, which is how an audit
                      control teaches people to ignore it. */}
                  {table.anchor_ok === null
                    ? "— not yet anchored"
                    : table.anchor_ok
                      ? "✓ anchored"
                      : "✗ mismatch"}
                </td>
                <td className="px-4 py-2 text-xs text-muted">{table.reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="border-t border-border px-4 py-2 text-xs text-muted">
        Status only. Row hashes are not exposed here.
      </p>
    </section>
  );
}
