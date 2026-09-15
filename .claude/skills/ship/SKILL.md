---
name: ship
description: Commit, push, open the PR, wait for CI and squash-merge the current branch using scripts/ship.sh; the only sanctioned way to land work. Use when asked to ship, merge, open a PR, land the branch, or when a prompt run is complete.
allowed-tools: Bash(bash scripts/ship.sh*), Bash(git *), Bash(gh pr *), Bash(gh run *), Read
argument-hint: "[--no-merge] [--draft] [--title \"...\"] — usually no arguments"
---
# Ship the current branch

1. Make sure everything is committed: `git status --porcelain` must be empty. Commit messages are
   plain and imperative, e.g. `feat(uc3): lexicon matcher with transliteration variants`. No
   trailers, footers, emoji, tool names, or links to sessions. The commit-msg hook strips such
   lines and CI rejects them.
2. Ensure `.claude/run/report.md` exists (it becomes the PR's Verification section) and the
   `docs/build/PROMPT-PLAN.md` row for this prompt is updated in the branch.
3. Run `bash scripts/ship.sh`. Optional flags: `--title "..."`, `--body-file <md>`, `--no-merge`
   (leave the PR open), `--draft`, `--ci-timeout <s>`.
4. On failure read the message: it names the failing stage.
   - gate failed → `.claude/run/gate.log`; fix, commit, re-run.
   - attribution failed → a commit in the range has a wrong author/committer or a forbidden
     line. History rewriting is blocked, so: `git switch -c <branch>-clean origin/<base>` and
     `git cherry-pick <sha>...` (the commit-msg hook cleans messages), then re-run ship.
   - rebase conflicts → resolve in the working tree, `git rebase --continue`, re-run ship.
   - CI red → `gh pr checks <n>` and `gh run view <run-id> --log-failed`; fix; commit; re-run.
   - merge failed → read `.claude/run/merge.log` (branch protection, conflicts) and report.
5. After a successful merge you are on the updated base branch; report the PR number, URL and
   the merged SHA.

Never use `gh pr merge`, `gh pr create`, or `git push` to the base branch directly; the Bash
guard blocks them so that CI and attribution checks can never be skipped.
