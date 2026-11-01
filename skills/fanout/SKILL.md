---
name: fanout
description: Plan and run parallel work across subagents with the right model for each piece, then verify and merge the results. Use when a task splits into independent parts, when the user says fan out, or when reading or changing many files at once.
---
# Fan-out

Delegation buys speed and keeps your context clean, and it costs tokens. Use it when the parts are independent, bulky, or routine. Do small sequential work yourself.

## 1. Split
- Break the task into units that don't edit the same files. Write the split as a short table: unit, scope (files or question), agent, expected output, how it gets verified.
- If two units must touch the same files, run them one after the other, or give each its own worktree with the `worktree` skill.

## 2. Pick the agent
- Search and reading: `Explore` (Sonnet).
- Implementation: `worker` (Sonnet, high).
- Research: `researcher` (Sonnet).
- Verification: `verifier` (Sonnet).
- Review of the combined result: `reviewer` (Opus, high).
- A unit hard enough to need Opus: keep it in the main session, or pass `model: opus` on that call.
- Mechanical transforms over many files (renames, extraction, reformatting): a script first, Haiku second.
- Overflow to Codex: when `python3 {{KIT}}/bin/usage.py` says Claude's week is past the routing threshold, give implementation units a Codex lane (a worktree marked with `bash {{KIT}}/bin/lane.sh <path> codex`) instead of a `worker`.

## 3. Brief
Each brief stands alone: goal, scope and files, constraints (repo rules that matter, what not to touch), the exact output to return, and how to verify it. Ask for conclusions, not file dumps.

## 4. Launch
Launch independent units in one message so they run together. Default to at most five at a time; more only when the units are small.

## 5. Collect, verify, merge
- Read results critically. A subagent's "done" is a claim until checked.
- Run `verifier` on the combined change, then `reviewer` if code changed.
- Merge and resolve conflicts yourself. Report to the user: what each unit did, the evidence, anything unverified.
- Retire any worktrees you created once their work is merged.
