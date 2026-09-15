import { severityRank } from "../lib/queue";
import type { Severity } from "../lib/types";

/** Severity is never carried by colour alone: each level has its own glyph, a
 *  three-cell rank bar and the word itself. A reviewer sorting on a projector, in
 *  high contrast mode, or with a colour-vision deficiency still gets the order. */
const MARKS: Record<Severity, { glyph: string; bar: string; word: string; tone: string }> = {
  high: { glyph: "▲", bar: "███", word: "HIGH", tone: "text-high" },
  medium: { glyph: "◆", bar: "██░", word: "MED", tone: "text-medium" },
  low: { glyph: "▪", bar: "█░░", word: "LOW", tone: "text-low" },
};

export function SeverityTag({ severity, size = "sm" }: { severity: Severity; size?: "sm" | "md" }) {
  const mark = MARKS[severity];
  return (
    <span
      className={`inline-flex items-center gap-1.5 font-mono font-semibold tracking-tight ${mark.tone} ${
        size === "md" ? "text-sm" : "text-xs"
      }`}
      aria-label={`Severity ${severity}`}
      title={`Severity ${severity}`}
    >
      <span aria-hidden="true">{mark.glyph}</span>
      <span aria-hidden="true" className="tracking-[0.15em]">
        {mark.bar}
      </span>
      <span aria-hidden="true">{mark.word}</span>
    </span>
  );
}

/** The left rule on a queue row. Width, not hue, is what reads at a glance down a
 *  long list; the colour is a second, redundant signal. */
export function SeverityRule({ severity }: { severity: Severity }) {
  const width = ["w-1.5", "w-1", "w-0.5"][severityRank(severity)] ?? "w-0.5";
  const tone =
    severity === "high" ? "bg-high" : severity === "medium" ? "bg-medium" : "bg-low";
  return <span aria-hidden="true" className={`${width} ${tone} shrink-0 self-stretch rounded-sm`} />;
}
