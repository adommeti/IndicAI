/** Shapes from the pinned uc3/P6 API contract. Nothing here is invented: every
 *  field appears in the contract table, and the UI never asks for a field that a
 *  role's endpoints do not return. */

export type Role = "compliance_reviewer" | "compliance_lead" | "governance";

export type Severity = "high" | "medium" | "low";

export type Disposition = "confirmed" | "false_positive" | "needs_more_context" | "escalated";

export interface Me {
  identity: string;
  roles: Role[];
  dev_bypass: boolean;
}

export interface FlagSummary {
  flag_id: string;
  call_id: string;
  category: string;
  severity: Severity;
  speaker: string;
  start_ms: number;
  created_at: string;
  /** The LATEST disposition for this flag, or null when nobody has ruled yet. */
  disposition: Disposition | null;
}

export interface QaFlag extends FlagSummary {
  qa_sampled: true;
}

export interface TranscriptSegment {
  seg_id: number;
  speaker: string;
  start_ms: number;
  end_ms: number;
  /** Native script as spoken (Devanagari, Telugu, Tamil or Latin). */
  text: string;
  /** Romanised rendering. Null where the source was already Latin or no
   *  transliteration exists — the UI falls back and says so rather than blanking. */
  text_roman: string | null;
}

export interface DispositionRow {
  disposition: Disposition;
  note: string;
  reviewer_id: string;
  created_at: string;
}

export interface FlagDetail extends FlagSummary {
  evidence_span: string;
  english_rendering: string;
  reasoning: string;
  policy_clause: string;
  transcript: TranscriptSegment[];
  /** Full append-only history, newest first. A changed mind is a new row. */
  dispositions: DispositionRow[];
}

export interface AudioGrant {
  /** Short-lived presigned URL. Never persisted, never logged, never put in a link. */
  url: string;
  expires_in_s: number;
  start_ms: number;
}

export interface DispositionReceipt {
  disposition_id: string;
  seq: number;
  row_hash: string;
}

/** precision is null when nothing has been decided; it is never 0.0 as a
 *  placeholder (see CLAUDE.md: missing data is "unmeasured"). */
export interface CategoryPrecision {
  category: string;
  confirmed: number;
  false_positive: number;
  decided: number;
  precision: number | null;
}

export interface PrecisionPoint extends CategoryPrecision {
  /** The *granularity* -- "day" | "week" | "month" -- not a label. The server
   *  sets this to the `bucket` argument it was called with, so every point in
   *  a response carries the same value. Labelling an axis with it renders
   *  "week" three times; the label comes from `bucket_start`. */
  bucket: string;
  /** Start of the window, ISO-8601 UTC, half-open with `bucket_end`. This is
   *  what identifies a point on the axis. */
  bucket_start: string;
  bucket_end: string;
  flags: number;
}

export interface PrecisionMetrics {
  by_category: CategoryPrecision[];
  over_time: PrecisionPoint[];
  /** Categories (or buckets) the server could not measure at all. */
  unmeasured: string[];
}

export interface FalseNegativeEstimate {
  /** The whole QA sample. */
  sampled: number;
  /** The reviewed part of it, and the denominator of `rate`. Diverges from
   *  `sampled` whenever the queue has a backlog, and the gap must be shown:
   *  a rate over 2 settled calls out of 100 sampled is not a clean bill of
   *  health for the programme. */
  settled: number;
  missed: number;
  /** Sampled calls still carrying an undecided flag. In neither numerator nor
   *  denominator. */
  pending: number;
  /** Settled calls that raised no flag, so no human ever read them — counted
   *  clean on the detector's own word. This is why the rate is a lower bound. */
  flagless: number;
  rate: number | null;
  unmeasured: boolean;
}

export interface ChainTable {
  table: string;
  rows: number;
  ok: boolean;
  /** `null` when this chain has never been anchored -- a first run, before the
   *  nightly verify has written a baseline. That is not a mismatch, and
   *  rendering it as one would put a red "break" on the governance dashboard
   *  of a healthy new deployment. */
  anchor_ok: boolean | null;
  reason: string | null;
}

export interface ChainStatus {
  tables: ChainTable[];
  breaks: number;
  ok: boolean;
  checked_at: string;
}
