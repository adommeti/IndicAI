---
paths: ["prompts/**", "apps/*/prompts/**", "docs/build/**"]
---
# Prompt-file rules

- `prompts/<group>/<Pn>.md` are build prompts executed by `scripts/run-prompt.sh`. They are the
  contract for a PR: acceptance criteria at the end are restated verbatim, with pass/fail, in
  `.claude/run/report.md`. Do not edit a build prompt to make it easier; if it is wrong, record
  the deviation in the report and in `docs/adr/`.
- `apps/*/prompts/*.md` are runtime prompts (system prompts for Claude). Changing one changes a
  control: bump nothing manually — `prompt_version` is content-derived — but run the relevant
  `make eval-ucN` and paste the before/after numbers in the PR.
- `docs/build/PROMPT-PLAN.md` is the source of truth for sequencing and status. Only the row of
  the prompt being shipped changes in a PR.
