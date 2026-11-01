
## Claude Code

### Models and delegation
- The main session orchestrates on Opus at medium effort. Raise it with `/effort high` for planning, hard debugging, and merging parallel work.
- Delegate to the cheapest agent that does the job well:
  - `Explore` (Sonnet): find files, symbols, usages, config values; summarize many files when only the conclusion matters.
  - `worker` (Sonnet, high effort): scoped implementation with tests in one area.
  - `verifier` (Sonnet): runs the repo's gates and real-boundary checks, returns evidence.
  - `researcher` (Sonnet): web and docs research with sources and confidence.
  - `reviewer` (Opus, high effort): read-only, defect-first review of a diff before {{USER}} sees it.
  - `curator` (Opus, high effort): runs the learning-loop review.
  - `advisor` (Fable): second opinion on a high-stakes design call, or on a task that failed its checks twice at Opus high. Gated: the guard hook asks {{USER}} first, so say why in one line.
  - Haiku: very simple mechanical work only, after a script couldn't do it; pass `model: haiku` on that call.
- Delegate work that is parallel, bulky enough to flood your context, or routine. Small, sequential, judgment-heavy work is cheaper done directly.
- Subagents don't see this conversation. Brief them fully: goal, scope, files, constraints, expected output, and how to verify.
- If a model refuses or a task keeps failing, say so in one line and name the next tier to try: advisor, then Opus, then Sonnet. Never Haiku.
- Independent subagents launch in one message so they run in parallel, up to 10.

### Learning loop
- When {{USER}} corrects you, or you make a mistake worth not repeating, finish the task, then run `/learn <what went wrong> -> <the rule that would have prevented it>`.
- Don't add lessons to CLAUDE.md, AGENTS.md, or rule files yourself. `/learn review` turns captured lessons into proposals, and applying them needs {{USER}}'s yes; nothing lands without it.
- Hooks log corrections and permission friction on their own; no need to mention it.

### Instruction files
- In a repo, CLAUDE.md is `@AGENTS.md` plus Claude-only notes. Shared rules go in AGENTS.md, kept under 150 lines.
- A hook denies edits that add log entries to an instruction file or push it past its line budget, including growth of a file already over budget. Such content goes in docs/ or the PR; an over-budget file gets slimmed by running `/instructions` yourself, never by suggesting it to {{USER}}.
- This file is generated from {{KIT}} (local/AGENTS.md, adapters/claude.md, local/claude.md). Changes go to the source there (through `/learn`, or directly when {{USER}} asks), followed by `bash {{KIT}}/install.sh --sync`.
