---
name: learn
description: Capture a lesson from a correction or mistake, or run the learning loop. Use right after the user corrects you or says "remember this" or "from now on", after a mistake worth not repeating, or when the user runs /learn, /learn review, /learn prune, or /learn status.
argument-hint: "<lesson> | review | prune | status"
---
# Learning loop

Four steps: capture (hooks and this skill), review (the curator proposes), approval (the user), apply (a script). Nothing reaches an instruction file without the user's yes.

The files it edits are the user's own, in `local/` inside the kit: the rulebook `local/AGENTS.md`, and the optional personal additions `local/claude.md` and `local/codex.md`. Captured signals, bundles, proposals and the ledger live in `local/learning/`. The shipped `adapters/` are kit code and are never edited by the loop.

## /learn <lesson>
Capture now, in one command:

`python3 {{KIT}}/bin/learn.py add "<situation>: <what you did> / <what the user wanted> -> <rule that would have prevented it>"`

Make the line stand on its own; the curator reads it weeks later without this conversation. Reply with one word: "Logged."

## /learn review
1. `python3 {{KIT}}/bin/learn.py mine` pulls new correction signals from Claude Code and Codex transcripts into `local/learning/inbox/`, each tagged with its tool.
2. `python3 {{KIT}}/bin/learn.py bundle` writes a compact review bundle and prints the bundle path and the proposals path to use.
3. If it reports nothing to review, say so and stop.
4. Spawn the `curator` subagent with the bundle path and the proposals path. Tell it to follow its output format exactly.
5. Read the proposals file. If the curator proposed nothing, run `python3 {{KIT}}/bin/learn.py close <review dir>` (the folder holding the bundle) and say so. Otherwise show the user each proposal compactly: the rule, why, the evidence in a few words, where it would go, and the test. Ask for all decisions in one message: yes, no, or an edited wording.
6. Leave the proposals file in `local/learning/proposals/`; the guard blocks agents from running `apply.py` with `--approve`. When the user says yes, give them the one command to run themselves: `python3 {{KIT}}/bin/apply.py <proposals file> --approve P1,P3 --reject P2` (add `--edit 'P3=<new rule text>'` for edited wording). For `manual:` proposals the user approves, make the hook, permission, or skill change yourself, then they record it with `--done P4`.
7. If it reports failures, fix the target or wording in the proposals file and rerun for those ids. Then run the `next:` command it prints (`bash {{KIT}}/install.sh --sync`) to push the updated instructions into Claude Code and Codex. Report what changed as a short list.

## /learn prune
Run monthly, or when /learn status shows rules past their review date.
1. `python3 {{KIT}}/bin/learn.py prune-bundle` lists rules due for review, budget usage of the rulebook and personal adapters, and auto-memory files that have grown large.
2. Spawn `curator` with that bundle and ask for keep-or-remove proposals only.
3. Same approval and apply steps as review.

## /learn status
Run `python3 {{KIT}}/bin/learn.py status` and relay the numbers in two or three lines.
