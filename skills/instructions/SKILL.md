---
name: instructions
description: Create or slim a repo's agent instruction files (AGENTS.md, CLAUDE.md, .claude/rules, .claude/settings.json) to the lean standard. Use when the user runs /instructions, when a repo has no AGENTS.md, or when its instruction files are over budget or contain build logs and status.
argument-hint: "[repo path]"
disable-model-invocation: true
---
# Lean instruction files for a repo

Goal: a root AGENTS.md under 150 lines that every agent tool reads, a CLAUDE.md that imports it, path-specific rules where they belong, and nothing lost. Work on the main checkout, not a feature worktree.

## 1. Inventory
- `python3 {{KIT}}/bin/lint.py <repo>` shows line counts, log sections, stale paths, and shouting.
- List every instruction file, including nested ones and copies under `.claude/worktrees/`. Check `git status`; if someone else has uncommitted edits to these files, ask before proceeding.

## 2. Classify every section
- Keep: commands an agent can't guess, the gate to run before "done", non-default conventions, gotchas (the trap, why it bites, what to do instead), invariants and boundaries, decisions with their reason.
- Move: build logs, status, dated progress, backlog, session notes, long explanations, file-by-file maps. Append them verbatim to `docs/build-log.md` under a heading naming the source file and today's date. Nothing gets deleted.
- Personal data, credentials, and private server hostnames: flag them to the user and leave them out of the new files.

## 3. Verify what you keep
- Run every command you keep, or its `--help` or dry run if it is slow or destructive. Check every path with `test -e`. Fix or drop what fails, and list what you couldn't verify.

## 4. Write the new files
- AGENTS.md from `{{KIT}}/templates/repo/AGENTS.md`. CLAUDE.md from `{{KIT}}/templates/repo/CLAUDE.md`: `@AGENTS.md` plus Claude-only notes, under 40 lines.
- Path-specific conventions go to `.claude/rules/<area>.md` with a `paths:` frontmatter list, under 80 lines each. `{{KIT}}/templates/repo/.claude/rules/example.md` shows the shape.
- Rules that must always hold become proposals for hooks, permissions, lint, or CI. List them for the user; install nothing without their yes.
- `.gitignore`: add the lines from `{{KIT}}/templates/repo/gitignore-snippet.txt` (ignore `.claude/settings.local.json` and `.claude/worktrees/`, track `.claude/rules/`, `.claude/settings.json`, and `.claude/skills/`). Replace any blanket `.claude/` ignore.
- If the repo has only AGENTS.md, still add the one-line CLAUDE.md: Claude Code reads AGENTS.md by itself only when no CLAUDE.md exists in that folder or any parent.

## 5. Check and hand back
- `python3 {{KIT}}/bin/lint.py <repo>` must pass.
- Show the user: line counts before and after, what moved where, what you couldn't verify, and the enforcement proposals. Commit only with their OK, staging files by name.
- Remind them that worktrees branched earlier keep the old files until they rebase or merge.
