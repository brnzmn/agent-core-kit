---
name: setup
description: First-run interview for agent-core-kit. Asks the user about themselves, how much autonomy agents get, and their machine, then writes their personal rulebook (local/AGENTS.md) and config (local/config.json), folds in any global instructions they had before, syncs both tools and verifies. Use right after install.sh, when the user says setup, or to redo answers later.
agent-core-only: claude
disable-model-invocation: true
---
# Setup

You are setting up agent-core-kit for {{USER}} (the name may still be empty). The kit lives in `{{KIT}}`. Everything personal lives in `{{KIT}}/local/`, which is the user's own git repo:
- `local/AGENTS.md`: their rulebook, shared by Claude Code and Codex (budget 120 lines).
- `local/claude.md`, `local/codex.md`: optional per-tool additions (budget 60 lines each).
- `local/config.json`: settings the hooks and installer read. Keys and defaults: `{{KIT}}/config.example.json` and the README.

Work in this order. Ask, then write, then verify. Don't skip the verify step.

## 1. Read what exists
- `local/AGENTS.md` and `local/config.json`. If `local/AGENTS.md` still says "/setup fills this in" this is a first run; otherwise this is a redo: show the current answers and ask only what the user wants to change.
- Their previous global instructions, which install.sh backed up: the newest `local/backups/*/claude/CLAUDE.md` and `local/backups/*/codex/AGENTS.md`. If either holds rules of their own, you will fold them in at step 3. Read them now so your questions don't ask what they already wrote down.
- `git config --global user.name`, `gh api user --jq .login` (if gh is signed in), the OS (`uname -s`), and which of `claude`, `codex`, `gh` are on PATH. Use these as defaults.

## 2. Ask (one batch per round, AskUserQuestion, at most three rounds)
Offer a recommended default first in each question. Accept free text.

Round 1, about them:
- Name to use in instructions.
- What they do (one line), and their core domains where their spec beats the agent's judgment (e.g. legal, product, design, a language or framework).
- Languages they write to agents in (replies follow the message's language).

Round 2, autonomy:
- How far agents go without asking. Options:
  - Act first (recommended for people who review outcomes, not steps): commit, push, merge own PRs once checks and both reviews pass, deploy previews and production, migrate dev databases.
  - Balanced (the template's default): as above, but production deploys need a yes.
  - Careful: commit and push branches and open PRs; merging and deploying need a yes.
- Spend per action without asking: $0, $1, $5 (default), $20.
- Claude Code permission mode: leave as is (default), auto (classifier approves routine actions), acceptEdits, bypassPermissions (only for people who rely on the guard hook and know the risk; say that in the option).
- Should the agent be pushed back when it ends a turn by handing them a list of steps? (Stop hook; default yes.)

Round 3, machine and safety:
- The folder their repos live in (projects_root). Suggest `~/Projects` or `~/code` if it exists. On macOS warn against `~/Documents` and `~/Desktop` when iCloud syncs them.
- Folders that hold data agents must never write to, delete or move (protected_dirs): data lakes, exports, client files. A bare name matches that folder name anywhere; a path (`~/clients/acme/data`) protects that path only. Prefer paths unless the name is unusual.
- Supabase projects whose schema must only change through a deploy script (protected_supabase), if any: ref and a one-line note.
- GitHub user or org and deploy targets (Vercel team, Cloudflare account, VPS host, Supabase org), for Claude's auto mode: these become auto_mode_environment lines, so routine pushes and deploys to them aren't blocked as unknown.
- The PR double-review gate: on for every repo (default), only listed repos, or off; which base branches count as deploy branches (default main, master, production); whose verdict comments count (default: their signed-in gh user; add teammates' logins to pr_gate.reviewers); whether a review by Codex's GitHub app counts as the Codex verdict (default no).

## 3. Write
- `local/config.json`: set the keys from the answers. Keep keys you weren't told to change. Valid JSON; check with `python3 -m json.tool`.
- `local/AGENTS.md`: start from the current file. Change only what the answers decide:
  - "About" section: role and core domains.
  - "How to talk to": the language line; if they gave tone preferences, one line each.
  - "Standing authorization": the do-without-asking and needs-a-yes lists for their autonomy level, with their spend limit in both.
  - "Machine": OS, projects folder, GitHub account, runtime quirks they mentioned (one line each).
  - Rules from their old global CLAUDE.md or AGENTS.md that still apply: merge them into the matching section, one line each, without duplicating what's there. Status logs, dated notes and project-specific rules from the old file don't go in; list them for the user instead. If the rulebook would pass 120 lines, put tool-specific rules in `local/claude.md` or `local/codex.md` and tell the user what moved.
- Never write secrets, tokens or passwords into either file.

## 4. Sync and verify
1. `bash {{KIT}}/install.sh --sync`
2. `bash {{KIT}}/install.sh --check` must print "in sync". Fix and rerun until it does.
3. `python3 {{KIT}}/bin/lint.py` must pass (budgets, no logs in instruction files).
4. Show the user the first lines of `~/.claude/CLAUDE.md` so they see their name and rules took.
5. Commit in `local/` with files staged by name: `git -C {{KIT}}/local add AGENTS.md config.json` (plus claude.md or codex.md if you wrote them), then commit with message "setup: <first run|update>".

## 5. Tell the user, briefly
- What you set (autonomy level, spend limit, protected folders, PR gate scope), in a few lines.
- What needs them, as a numbered checklist with the exact command per line and a last line starting `Blocked:` (or `Blocked: nothing` when nothing does). Typical items:
  - Start a new Claude Code session so hooks and agents load.
  - In Codex, run `/hooks` once and trust the kit's hooks.
  - Sign in to gh (`gh auth login`) if the PR gate is on and gh isn't signed in.
  - If they chose auto mode: run `/auto-mode-setup` once in Claude Code.
- How to change answers later: run /setup again, or edit `local/config.json` and run `bash {{KIT}}/install.sh --sync`.
