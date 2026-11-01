---
name: handoff
description: Write a handoff note so another session, agent, or tool can continue the work without this conversation. Use when ending mid-task, switching between Claude Code and Codex, before a long break, or when the user asks for a handoff.
argument-hint: "[short-slug]"
---
# Handoff

Write the note to `docs/handoffs/<YYYY-MM-DD>-<slug>.md` in the repo (create the folder if needed), or to `{{KIT}}/local/handoffs/` when there is no repo. Never into AGENTS.md or CLAUDE.md.

Contents, in this order, as short as completeness allows:
1. Goal: what "done" means, in one or two sentences.
2. State: repo, branch, worktree path, last commit, uncommitted files.
3. Done so far, each item with its evidence (command and result).
4. Decisions made, each with its reason, plus anything the user explicitly asked for.
5. Not done yet, in order, with the exact next commands.
6. Risks, open questions, and anything unverified.
7. Where the relevant files, logs, and links are.

Reply with the path and a two-line summary.
