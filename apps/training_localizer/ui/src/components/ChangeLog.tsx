import { useState } from "react";
import type { Change } from "../lib/types";

export function ChangeLog({ changes }: { changes: Change[] }) {
  const [open, setOpen] = useState(false);
  if (changes.length === 0) return <span className="text-[11px] text-muted">no changes</span>;
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="rounded text-[11px] font-medium text-accent underline underline-offset-2"
      >
        {changes.length} change{changes.length === 1 ? "" : "s"}
      </button>
      {open && (
        <ul className="mt-1 space-y-1">
          {changes.map((change, index) => (
            <li key={index} className="text-[11px] leading-snug text-muted">
              <span className="font-mono">
                {change.from ? `${change.from} → ` : "+ "}
                <span className="indic text-ink">{change.to}</span>
              </span>
              <span className="block">
                {change.reason}
                {change.enforced ? " (enforced)" : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
