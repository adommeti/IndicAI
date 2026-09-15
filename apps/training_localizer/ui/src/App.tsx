import { useCallback, useEffect, useMemo, useState } from "react";
import { OverrideDialog } from "./components/OverrideDialog";
import { QuizReview } from "./components/QuizReview";
import { SegmentRow } from "./components/SegmentRow";
import { SummaryBar } from "./components/Summary";
import { LockedOverrideRequired, api } from "./lib/api";
import { applyFilter, blockedFromBulk, bulkApprovable, summarise } from "./lib/review";
import type { Filter } from "./lib/review";
import type { Language, QuizItem, ReviewPayload, Segment } from "./lib/types";

const LANGUAGES: Language[] = ["hi-IN", "te-IN", "ta-IN"];

/** The module under review. Taken from the URL so the page is linkable from the
 *  pipeline's status output; there is no module browser yet because uc2/P5 owns
 *  the delivery surface and one list in two places would drift. */
function moduleIdFromUrl(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("module");
}

type Pending = { segment: Segment; text: string; lockedId: string; expected: string };

export default function App() {
  const moduleId = useMemo(moduleIdFromUrl, []);
  const [language, setLanguage] = useState<Language>("hi-IN");
  const [payload, setPayload] = useState<ReviewPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [pending, setPending] = useState<Pending | null>(null);
  const [tab, setTab] = useState<"segments" | "quiz">("segments");

  const load = useCallback(async () => {
    if (!moduleId) return;
    setLoading(true);
    setError(null);
    try {
      setPayload(await api.review(moduleId, language));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, [moduleId, language]);

  useEffect(() => {
    void load();
  }, [load]);

  const applyApproval = useCallback(
    (segId: number, text: string, by: string) => {
      setPayload((current) => {
        if (!current) return current;
        const segments = current.segments.map((s) =>
          s.seg_id === segId
            ? { ...s, translation: text, approved: true, approved_by: by, locked_mismatch: false }
            : s,
        );
        return { ...current, segments, summary: summarise(segments) };
      });
    },
    [],
  );

  const approve = useCallback(
    async (segment: Segment, text: string, overrideReason?: string) => {
      if (!moduleId || !payload) return;
      setBusy(true);
      setError(null);
      try {
        await api.approve(moduleId, segment.seg_id, {
          language,
          text,
          ...(overrideReason ? { override_reason: overrideReason } : {}),
        });
        applyApproval(segment.seg_id, text, payload.reviewer);
        setPending(null);
      } catch (cause) {
        if (cause instanceof LockedOverrideRequired) {
          // The API refused; ask for a reason rather than guessing one.
          setPending({
            segment,
            text,
            lockedId: cause.detail.locked_id,
            expected: cause.detail.expected,
          });
        } else {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      } finally {
        setBusy(false);
      }
    },
    [moduleId, payload, language, applyApproval],
  );

  const approveAllRemaining = useCallback(async () => {
    if (!payload) return;
    const targets = bulkApprovable(payload.segments);
    const blocked = blockedFromBulk(payload.segments);
    const message =
      `Approve ${targets.length} remaining segment${targets.length === 1 ? "" : "s"} as they stand?` +
      (blocked.length
        ? `\n\n${blocked.length} LOCKED segment${blocked.length === 1 ? "" : "s"} will be skipped: ` +
          "a mismatch has to be handled one at a time, with a reason."
        : "");
    if (!window.confirm(message)) return;
    for (const segment of targets) {
      await approve(segment, segment.translation);
    }
  }, [payload, approve]);

  const toggleQuiz = useCallback(
    async (item: QuizItem, approved: boolean) => {
      if (!moduleId) return;
      setBusy(true);
      try {
        await api.approveQuiz(moduleId, item.item_id, item.language, approved);
        setPayload((current) =>
          current
            ? {
                ...current,
                quiz_items: current.quiz_items.map((q) =>
                  q.item_id === item.item_id && q.language === item.language
                    ? { ...q, approved }
                    : q,
                ),
              }
            : current,
        );
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setBusy(false);
      }
    },
    [moduleId],
  );

  if (!moduleId)
    return (
      <Shell>
        <p className="rounded border border-dashed border-border p-8 text-center text-muted">
          Add <code className="font-mono">?module=&lt;uuid&gt;</code> to the URL to review a module.
        </p>
      </Shell>
    );

  const visible = payload ? applyFilter(payload.segments, filter) : [];

  return (
    <Shell>
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Segment review</h1>
          <p className="text-xs text-muted">
            module <code className="font-mono">{moduleId}</code>
            {payload && <> · reviewing as {payload.reviewer}</>}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-muted" htmlFor="language">
            Language
          </label>
          <select
            id="language"
            value={language}
            onChange={(event) => setLanguage(event.target.value as Language)}
            className="rounded border border-border bg-surface px-2 py-1 text-sm"
          >
            {LANGUAGES.map((code) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
          <div className="flex rounded border border-border" role="group" aria-label="Filter">
            {(["all", "flagged", "unapproved"] as Filter[]).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setFilter(option)}
                aria-pressed={filter === option}
                className={`px-3 py-1 text-xs capitalize ${
                  filter === option ? "bg-accent text-white" : "text-muted"
                }`}
                data-testid={`filter-${option}`}
              >
                {option}
              </button>
            ))}
          </div>
          <button
            type="button"
            disabled={busy || !payload}
            onClick={approveAllRemaining}
            className="rounded bg-accent px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
            data-testid="approve-all"
          >
            Approve all remaining
          </button>
        </div>
      </header>

      {payload && <SummaryBar summary={payload.summary} />}

      {error && (
        <p role="alert" className="mt-4 rounded border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
          {error}
        </p>
      )}

      <nav className="mt-5 flex gap-4 border-b border-border" aria-label="Sections">
        {(["segments", "quiz"] as const).map((name) => (
          <button
            key={name}
            type="button"
            onClick={() => setTab(name)}
            aria-current={tab === name}
            className={`-mb-px border-b-2 px-1 pb-2 text-sm capitalize ${
              tab === name ? "border-accent font-semibold" : "border-transparent text-muted"
            }`}
          >
            {name}
            {name === "quiz" && payload ? ` (${payload.quiz_items.length})` : ""}
          </button>
        ))}
      </nav>

      {loading && <p className="p-8 text-center text-muted">Loading review…</p>}

      {!loading && payload && tab === "segments" && (
        <div className="mt-4 overflow-x-auto rounded border border-border bg-surface">
          <table className="w-full min-w-[60rem] border-collapse text-left">
            <thead className="bg-surface-2 text-[11px] uppercase tracking-wide text-muted">
              <tr>
                <th className="p-3">Segment</th>
                <th className="p-3">Source</th>
                <th className="p-3">Translation</th>
                <th className="p-3">Back-translation</th>
                <th className="p-3 text-center">QA</th>
                <th className="p-3">Glossary</th>
                <th className="p-3">Change log</th>
                <th className="p-3 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((segment) => (
                <SegmentRow
                  key={segment.seg_id}
                  segment={segment}
                  language={language}
                  busy={busy}
                  onApprove={(text) => void approve(segment, text)}
                />
              ))}
            </tbody>
          </table>
          {visible.length === 0 && (
            <p className="p-8 text-center text-sm text-muted">
              Nothing matches this filter — every segment here is clean and approved.
            </p>
          )}
        </div>
      )}

      {!loading && payload && tab === "quiz" && (
        <div className="mt-4">
          <QuizReview items={payload.quiz_items} busy={busy} onToggle={(i, a) => void toggleQuiz(i, a)} />
        </div>
      )}

      {pending && (
        <OverrideDialog
          lockedId={pending.lockedId}
          expected={pending.expected}
          onCancel={() => setPending(null)}
          onConfirm={(reason) => void approve(pending.segment, pending.text, reason)}
        />
      )}
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto max-w-[96rem] p-4 sm:p-6">{children}</main>;
}
