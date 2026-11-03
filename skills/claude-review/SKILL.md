---
name: claude-review
description: Get an independent read-only review before merging a changed branch. PRs use both providers when available, or one passing review plus passing tests during a recorded provider outage.
agent-core-only: codex
argument-hint: "[base branch, default origin/HEAD]"
---
# Claude review

Claude reviews your diff read-only: it can read files and run `git diff`, `git log`, `git show` and `git status`, nothing else. It never edits.

1. Commit your work on its branch. Pick the base: the argument, else the PR's base, else `origin/HEAD`.
2. From the repo root, run (it takes a few minutes; it loads no settings files, so nothing widens its tools):

```bash
bash {{KIT}}/bin/claude-review.sh <base>
```

3. Read the whole output. The last line is the verdict; the exit code says the same (0 pass, 1 block, 3 no review).
   - `VERDICT: BLOCK`: fix every blocking finding, commit, and run the review again. Don't merge.
   - `VERDICT: PASS`: fix or answer the should-fix items, then merge as the repo's rules allow.
   - Exit 3 is a local error or incomplete review; exit 4 is a failed provider call. On a PR, the automatic path below distinguishes an outage from other failures.
4. On a PR, run `python3 {{KIT}}/bin/pr_review.py <pr> auto` instead of step 2. It attempts Claude and Codex independently on the PR head and posts their real verdicts. Read the comments before merging.
5. When one provider is unavailable because of quota, authentication, service failure or a missing CLI, the configured fallback requires a real PASS from the other provider and the configured full test suite passing on that same head. The runner posts the outage and test evidence itself. A BLOCK still holds the merge. Both unavailable, failed tests, stale evidence or unknown errors also hold it. No hand-written verdict or evidence comments.

Claude runs on the user's Claude plan, one review per run. Don't loop it more than three times on one branch; after the third BLOCK, stop and report the findings.
