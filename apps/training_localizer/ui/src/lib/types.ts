export type Language = "hi-IN" | "te-IN" | "ta-IN";

export interface Change {
  from: string;
  to: string;
  reason: string;
  enforced?: boolean;
}

export interface Segment {
  seg_id: number;
  start_ms: number;
  end_ms: number;
  source_text: string;
  locked: boolean;
  locked_id: string | null;
  translation: string;
  backtranslation: string | null;
  qa_score: number | null;
  qa_reason: string | null;
  glossary_hits: string[];
  glossary_misses: string[];
  change_log: Change[];
  approved: boolean;
  approved_by: string | null;
  locked_mismatch: boolean;
  flagged: boolean;
  stage_version: number;
}

export interface QuizItem {
  item_id: number;
  language: string;
  seg_id: number;
  question: string;
  options: string[];
  answer: number;
  rationale: string;
  approved: boolean;
}

export interface Summary {
  segments: number;
  approved: number;
  flagged: number;
  locked: number;
  locked_mismatches: number;
  glossary_misses: number;
  fidelity_mean: number | null;
}

export interface ReviewPayload {
  module_id: string;
  language: Language;
  reviewer: string;
  summary: Summary;
  segments: Segment[];
  quiz_items: QuizItem[];
}

export interface OverrideRequired {
  error: "locked_override_required";
  locked_id: string;
  expected: string;
  hint: string;
}
