---
id: uc2_quiz
version: 1
model: claude-sonnet-5
temperature: 0
temperature_sent: false  # claude-sonnet-5 rejects the parameter; the adapter omits it
schema: "QuizDraft {items: [{seg_id: int, question: str, options: list[str], answer: int, rationale: str}]}"
source: docs/prd-v2.md D7 (quiz.md) — text below is the PRD's, verbatim
---
Write 5 multiple-choice comprehension items for this module. Each item must test a behaviour the learner should perform or avoid, cite the seg_id it is based on, have 4 options with exactly one correct, and include a one-sentence rationale. Avoid trick questions and negations. Return JSON.
