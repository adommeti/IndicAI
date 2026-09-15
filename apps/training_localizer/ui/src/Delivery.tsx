import { useCallback, useEffect, useMemo, useState } from "react";

interface QuizQuestion {
  item_id: number;
  seg_id: number;
  question: string;
  options: string[];
}

interface DeliveryPayload {
  module_id: string;
  title: string;
  language: string;
  cohort: "native" | "control";
  pilot_id: string;
  video_uri: string | null;
  captions_uri: string | null;
  summary_audio_uri: string | null;
  quiz: QuizQuestion[];
}

interface Result {
  score: number;
  max_score: number;
  passed: boolean;
  cohort: string;
}

const BASE = import.meta.env.VITE_API_BASE ?? "/api";

/** Object-store URIs are not browser URLs. The delivery page asks the API for a
 *  playable URL rather than guessing a mapping the deployment may not have. */
function playable(uri: string | null): string | null {
  if (!uri) return null;
  return uri.startsWith("s3://") || uri.startsWith("memory://")
    ? `${BASE}/media?uri=${encodeURIComponent(uri)}`
    : uri;
}

export default function Delivery({ moduleId }: { moduleId: string }) {
  const [payload, setPayload] = useState<DeliveryPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<number, number>>({});
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const startedAt = useMemo(() => Date.now(), []);

  useEffect(() => {
    (async () => {
      try {
        const response = await fetch(`${BASE}/delivery/${moduleId}`, {
          credentials: "include",
        });
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        setPayload(await response.json());
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    })();
  }, [moduleId]);

  const submit = useCallback(async () => {
    if (!payload) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${BASE}/delivery/attempts`, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          module_id: payload.module_id,
          language: payload.language,
          answers,
          duration_ms: Date.now() - startedAt,
        }),
      });
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      setResult(await response.json());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }, [payload, answers, startedAt]);

  if (error)
    return (
      <p role="alert" className="m-6 rounded border border-danger/40 bg-danger/10 p-4 text-danger">
        {error}
      </p>
    );
  if (!payload) return <p className="p-8 text-center text-muted">Loading…</p>;

  const video = playable(payload.video_uri);
  const captions = playable(payload.captions_uri);
  const answered = Object.keys(answers).length;

  return (
    <main className="mx-auto max-w-3xl p-4 sm:p-6">
      <h1 className="text-xl font-semibold">{payload.title}</h1>
      <p className="mt-1 text-xs text-muted">
        {payload.language} · {payload.pilot_id}
        {/* The cohort is shown, not hidden: an employee in the control arm is
            entitled to know why they were served English. */}
        {payload.cohort === "control" && (
          <> · you are in the English-only comparison group for this pilot</>
        )}
      </p>

      {video ? (
        <video
          className="mt-4 w-full rounded border border-border bg-black"
          controls
          preload="metadata"
          data-testid="module-video"
        >
          <source src={video} type="video/mp4" />
          {captions && (
            <track kind="captions" src={captions} srcLang={payload.language} label={payload.language} default />
          )}
        </video>
      ) : (
        <p className="mt-4 rounded border border-dashed border-border p-6 text-center text-sm text-muted">
          No dubbed video has been produced for this module and language yet.
        </p>
      )}

      <h2 className="mt-8 text-lg font-semibold">Questions</h2>
      {payload.quiz.length === 0 ? (
        <p className="mt-2 rounded border border-dashed border-border p-6 text-center text-sm text-muted">
          No approved quiz items yet.
        </p>
      ) : (
        <ol className="mt-3 space-y-5">
          {payload.quiz.map((item, index) => (
            <li key={item.item_id} className="rounded border border-border bg-surface p-4">
              <fieldset>
                <legend className="text-sm font-medium">
                  {index + 1}. {item.question}
                </legend>
                <div className="mt-2 space-y-1">
                  {item.options.map((option, optionIndex) => (
                    <label key={optionIndex} className="flex items-start gap-2 text-sm">
                      <input
                        type="radio"
                        name={`q-${item.item_id}`}
                        checked={answers[item.item_id] === optionIndex}
                        disabled={result !== null}
                        onChange={() =>
                          setAnswers((current) => ({ ...current, [item.item_id]: optionIndex }))
                        }
                        className="mt-1"
                      />
                      <span>{option}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
            </li>
          ))}
        </ol>
      )}

      {result ? (
        <div
          className={`mt-6 rounded border p-4 ${
            result.passed ? "border-ok/40 bg-ok/10" : "border-warn/40 bg-warn/10"
          }`}
          data-testid="result"
        >
          <p className="font-semibold">
            {result.score} / {result.max_score} — {result.passed ? "passed" : "not yet"}
          </p>
          <p className="mt-1 text-sm text-muted">
            {result.passed
              ? "Your attempt has been recorded."
              : "Your attempt has been recorded. Rewatch the module and try again."}
          </p>
        </div>
      ) : (
        <div className="mt-6 flex items-center gap-3">
          <button
            type="button"
            disabled={busy || payload.quiz.length === 0}
            onClick={() => void submit()}
            className="rounded bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
            data-testid="submit-quiz"
          >
            Submit answers
          </button>
          <span className="text-xs text-muted">
            {answered} of {payload.quiz.length} answered
            {answered < payload.quiz.length && " — unanswered questions count as wrong"}
          </span>
        </div>
      )}
    </main>
  );
}
