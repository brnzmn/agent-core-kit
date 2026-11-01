# <Project name>

<One or two sentences: what this is, who uses it, and what must never break.>

## Commands
- Install: `<pnpm install>`
- Run locally: `<pnpm dev>` (<port, required env, seed data>)
- Gate before calling anything done: `<pnpm lint && pnpm typecheck && pnpm test>`
- <Anything non-obvious: required Node version, how to run one test, how to reset local data>

## Conventions that differ from the defaults
- <Only what an agent would otherwise get wrong. Skip what the code or the linter already shows.>

## Gotchas
- <The trap>: <why it bites>. <What to do instead.>

## Boundaries
- <Data, services, or files that are read-only, or that need the owner's OK before changing.>

## Code Review Rules
- A PR into `<deploy branch>` merges only after a Claude review and a Codex review of the PR head, each posted as a PR comment that ends in `VERDICT: PASS` or `VERDICT: BLOCK`. A BLOCK holds the merge until it is fixed and the new head is reviewed again.
- Read both reviews before merging. A new commit needs new reviews.
- <Repo-specific review focus, e.g. migrations, auth, money paths. Delete this line if none.>

## Where things live
- Build log and history: `docs/build-log.md`. Decisions with their reasons: `docs/decisions/`.
- Path-specific rules: `.claude/rules/`. Handoffs: `docs/handoffs/`.

<!-- Budget: under 150 lines. No status, progress, or dated log entries; those go in docs/ or the PR. -->
