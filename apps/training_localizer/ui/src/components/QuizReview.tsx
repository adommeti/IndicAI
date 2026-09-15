import type { QuizItem } from "../lib/types";

export function QuizReview({
  items,
  busy,
  onToggle,
}: {
  items: QuizItem[];
  busy: boolean;
  onToggle: (item: QuizItem, approved: boolean) => void;
}) {
  if (items.length === 0)
    return (
      <p className="rounded border border-dashed border-border p-6 text-center text-sm text-muted">
        No quiz items yet. They are written by the <code className="font-mono">quiz</code> stage.
      </p>
    );
  return (
    <ul className="space-y-3">
      {items.map((item) => (
        <li
          key={`${item.language}-${item.item_id}`}
          className="rounded border border-border bg-surface p-4"
          data-testid={`quiz-${item.item_id}`}
        >
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium">{item.question}</p>
              <p className="mt-1 text-[11px] text-muted">
                segment #{item.seg_id} · {item.language}
              </p>
            </div>
            <label className="flex shrink-0 items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={item.approved}
                disabled={busy}
                onChange={(event) => onToggle(item, event.target.checked)}
                className="h-4 w-4"
              />
              Approved
            </label>
          </div>
          <ol className="mt-2 space-y-1 text-sm">
            {item.options.map((option, index) => (
              <li
                key={index}
                className={index === item.answer ? "font-semibold text-ok" : "text-muted"}
              >
                {String.fromCharCode(65 + index)}. {option}
                {index === item.answer && <span className="ml-2 text-[11px]">correct</span>}
              </li>
            ))}
          </ol>
          <p className="mt-2 text-[11px] italic text-muted">{item.rationale}</p>
        </li>
      ))}
    </ul>
  );
}
