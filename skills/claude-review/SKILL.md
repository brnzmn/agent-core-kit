---
name: claude-review
description: Get a read-only Claude Opus review of your own diff before it merges. Use before merging any branch you changed, and always before a PR into a gated branch merges; the merge waits for this verdict and the other tool's own.
agent-core-only: codex
argument-hint: "[base branch, default origin/HEAD]"
---
# Claude review

Claude reviews your diff read-only: it can read files and run `git diff`, `git log`, `git show` and `git status`, nothing else. It never edits.

1. Commit your work on its branch. Pick the base: the argument, else the PR's base, else `origin/HEAD`.
2. From the repo root, run (it takes a few minutes; it loads none of the user's settings, only the kit's inline deny rules, so nothing widens its tools):

```bash
bash {{KIT}}/bin/claude-review.sh <base>
```

3. Read the whole output. The last line is the verdict; the exit code says the same (0 pass, 1 block, 3 no review).
   - `VERDICT: BLOCK`: fix every blocking finding, commit, and run the review again. Don't merge.
   - `VERDICT: PASS`: fix or answer the should-fix items, then merge as the repo's rules allow.
   - Exit 3 (no verdict line, base not found, or `claude` missing or signed out): the review did not happen. Don't merge; say so in your report.
4. On a PR, run `python3 {{KIT}}/bin/pr_review.py <pr> claude` instead of step 2: it runs the same review on the PR head and posts it as the `## Claude review` comment the merge gate looks for. A PR into a gated branch (`pr_gate.branches` in local/config.json; main, master and production by default) also needs your own verdict, `python3 {{KIT}}/bin/pr_review.py <pr> codex`. The guard holds `gh pr merge` until both are posted and neither says BLOCK.

Claude runs on the user's Claude plan, one review per run. Don't loop it more than three times on one branch; after the third BLOCK, stop and report the findings.
