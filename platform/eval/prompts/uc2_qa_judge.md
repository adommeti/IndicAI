---
id: uc2_qa_judge
version: 1
model: claude-haiku-4-5
temperature: 0
schema: "FidelityVerdict {score: int 1-5, reason: str, lost_or_changed: list[str]}"
source: docs/prd-v2.md D7 (qa_judge.md) — text below is the PRD's, verbatim
---
Given SOURCE and BACKTRANSLATION, score fidelity 1–5 (5 = same meaning and all obligations intact; 1 = meaning changed or an obligation lost). Return JSON {score, reason, lost_or_changed: [..]}.
