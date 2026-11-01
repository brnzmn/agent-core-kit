# agent-core-kit

One rulebook, one set of guards, one learning loop, shared by Claude Code and Codex. Claude orchestrates and does the judgment work. Codex gives a second opinion, reviews PRs next to Claude, and takes overflow work when your Claude week runs low. Your personal rules and settings live in `local/`, which updates never touch.

## Short version

- A rulebook (`local/AGENTS.md`) that both tools load, plus a small adapter per tool.
- Subagents with model routing: Opus orchestrates, Sonnet carries the volume, Opus reviews, a gated top-tier advisor for hard calls.
- Hooks in both tools that deny destructive commands and name the safe alternative.
- A learning loop: corrections are captured, a curator proposes rules, you approve, a script applies them.
- A PR gate: `gh pr merge` into your deploy branches waits for a Claude review and a Codex review of the exact head commit.
- Skills for handoffs, fan-out, worktrees, lean instruction files and verification.

Install:

```
git clone <repo-url> ~/agent-core-kit
cd ~/agent-core-kit
bash install.sh
claude
/setup
```

`/setup` is a short interview that writes your rulebook and config, syncs both tools and checks the result. Then run `/hooks` once inside Codex and trust the kit's hooks.

## Requirements

- macOS or Linux. Windows is not supported.
- Python 3.9 or newer, standard library only.
- git.
- The Claude Code CLI (v2.1.251 or later, so each subagent's own model wins over the Sonnet default) and the Codex CLI, both signed in.
- `gh`, signed in, for the PR gate and `pr_review.py`.
- Node.js, for OpenAI's Codex plugin for Claude Code (`/codex:review`, `pr_review.py <pr> codex`).
- Optional: `zstd`, so the learning loop can read Codex's compressed sessions (`brew install zstd`).

## Install and /setup

Clone the repo anywhere. `~/agent-core-kit` is the suggested spot, and the examples below use it.

1. `bash install.sh` checks that your settings file is valid JSON, creates `local/` (your own git repo) from the templates, backs up the files it replaces into `local/backups/<timestamp>/`, installs, and merges settings. Your existing agents, skills, commands, hooks, model and env settings stay where they are; the kit installs next to them, and a same-named agent or skill of yours is left alone with a message. `local/install-state.json` records what install changed, so uninstall can put it back. Add `--clean-slate` if you want the old ones archived first.
2. Start a new Claude Code session so hooks and subagents load, then run `/setup`. It reads your previous global instructions, asks about you, how much autonomy agents get, your projects folder and data you want protected, writes `local/AGENTS.md` and `local/config.json`, runs `install.sh --sync` and `--check`, and ends with a numbered list of anything only you can do.
3. In Codex, run `/hooks` and trust the kit's hooks. Codex runs hooks only after you review them.
4. Run `gh auth login` if `gh` is not signed in yet.

Rerun `/setup` any time to change answers.

## What gets installed where

| From the kit | Installed to |
|---|---|
| `local/AGENTS.md` + `adapters/claude.md` + `local/claude.md` | `~/.claude/CLAUDE.md` (generated, do not edit) |
| `local/AGENTS.md` + `adapters/codex.md` + `local/codex.md` | `~/.codex/AGENTS.md` (generated, do not edit) |
| `claude/agents/*.md` | `~/.claude/agents/` |
| The same roles for Codex (`explorer`, `worker`, `verifier`, `reviewer`) | `~/.codex/agents/*.toml` |
| `skills/*` | `~/.claude/skills/` and `~/.agents/skills/` (Codex reads that one) |
| `claude/hooks/*.py` | `~/.claude/hooks/agent-core/`, with `.core-dir` and `.local-dir` naming the kit and your `local/` |
| `claude/settings.json` | merged into `~/.claude/settings.json`; your own rules and settings are kept |
| Codex hooks and rules | `~/.codex/hooks.json`, `~/.codex/rules/` |
| `vendor/codex-plugin-cc` | Claude Code plugin `codex@openai-codex` |
| A pre-commit hook | every git repo under `projects_root` (off when it is empty) |
| What the installer replaced | `local/backups/<timestamp>/` |

Skills and agents are tracked in manifests. The installer adds or replaces only its own files and leaves anything of yours with the same name alone, with a message. If `~/.claude/CLAUDE.md` or another file it writes is a symlink (dotfiles), the link stays and the content goes through it.

The pre-commit hook is skipped, with a message, in repos whose hooks come from the pre-commit framework, husky or lefthook. A hook of your own is chained only when you run `install.sh`, never silently at session start.

## Daily use

| You want | Run |
|---|---|
| Log a lesson now | `/learn <what went wrong> -> <rule>` |
| Turn captured corrections into proposed rules | `/learn review` |
| Remove rules that stopped earning their place (monthly) | `/learn prune` |
| Check the learning loop | `/learn status` |
| Create or slim a repo's AGENTS.md, CLAUDE.md and rules | `/instructions` |
| Hand work to another session or tool | `/handoff` |
| Split work across subagents | `/fanout` |
| Create, list or retire a worktree safely | `/worktree` |
| Get a read-only Claude review of a Codex diff | the `claude-review` skill, in Codex |
| Post a Claude or Codex verdict on a PR | `python3 ~/agent-core-kit/bin/pr_review.py <pr> claude` or `codex` (`--adversarial` for risky diffs) |
| See plan usage and where new lanes should go | `python3 ~/agent-core-kit/bin/usage.py` |
| Redo the first-run interview | `/setup` |
| Compare what is installed with the kit | `bash ~/agent-core-kit/install.sh --check` |

Some skills load on their own when the task matches: `explain-codebase`, `verify-deployment`, `verify-final-artifacts`, `verify-native-controls`, `recover-background-work` and `audit-data-sources`.

## Scripts in bin/

| Script | What it does |
|---|---|
| `trash.py` | Moves files into a dated `_to_delete/` with a manifest; `--restore` puts one back. Nothing here deletes. |
| `secret.sh` | Stores and reads tokens as `agent/<provider>/<account>` in the macOS keychain, or the desktop secret store on Linux. Values never pass through argv. |
| `lint.py` | Checks instruction files against their line budgets and flags log sections, stale paths and shouting. |
| `learn.py`, `apply.py` | The learning loop: capture, mine, bundle, and apply approved proposals. |
| `usage.py` | Claude and Codex plan usage in under 10 seconds, plus the routing call. A number it cannot read is reported as unknown, never guessed. |
| `pr_review.py`, `claude-review.sh` | The two PR reviews and the read-only Claude review. |
| `lane.sh` | Marks a worktree as one tool's lane. |
| `codex_scratch.py` | A throwaway Codex home for tests. Scratch runs count against your Codex plan. |
| `build.py`, `git_hooks.py`, `precommit.py` | What `install.sh` drives: builds, installs, checks, and the instruction-file pre-commit hook. |

## Model routing

| Role | Model | Effort | Job |
|---|---|---|---|
| Main session | Opus | medium | Plans, delegates, judges. `/effort high` for planning, hard debugging and merging parallel work. |
| `Explore` | Sonnet | medium | Read-only search and summaries. |
| `worker` | Sonnet | high | One scoped implementation with tests. |
| `verifier` | Sonnet | medium | Runs the repo's gates and checks the real boundary. Returns PASS, FAIL or UNVERIFIED. |
| `researcher` | Sonnet | medium | Web and docs research with sources and confidence. |
| `reviewer` | Opus | high | Read-only, defect-first review of a diff. |
| `curator` | Opus | high | Turns captured lessons into proposed rules. |
| `advisor` | Fable | high | Second opinion on a high-stakes call, or after a task failed its checks twice at Opus high. Gated: Claude Code asks you before it starts. |
| Haiku | Haiku | n/a | Trivial mechanical work only, after a script could not do it. Never a judgment call. |
| Codex `explorer`, `worker`, `verifier`, `reviewer` | Codex's default model | its default | Codex's own subagents. Pin a model per role with `codex.models` in the config. |

Subagents without a pinned model run on Sonnet. When a model refuses or a task keeps failing, the agent names the next tier to try (advisor, then Opus, then Sonnet) and never Haiku.

## How Claude and Codex split the work

- **Claude orchestrates.** Planning, delegation, judgment calls and work you steer stay in Claude Code.
- **Codex is the second opinion and the overflow lane.** New implementation lanes go to Claude until `bin/usage.py` reports Claude's weekly usage past `routing.claude_week_limit_pct` (80 by default). Past that, new lanes go to Codex. If both are past it, running lanes finish and no new ones start.
- **Both review PRs into deploy branches.** `pr_review.py <pr> claude` and `pr_review.py <pr> codex` each review the PR head and post a comment that starts `## Claude review` or `## Codex review`, names the head commit and ends with `VERDICT: PASS` or `VERDICT: BLOCK`. The guard holds `gh pr merge` into a gated branch until both comments exist for the current head and neither says BLOCK. A new commit needs new reviews. Only comments by you (your signed-in gh user) or the logins in `pr_gate.reviewers` count. Codex's GitHub code-review app counts as the Codex review only when `pr_gate.trust_codex_bot` is true. The gate also covers `gh -R owner/repo pr merge` and denies merging through `gh api`. If gh can't be read within 8 seconds, the merge is held.
- **Claude reviews Codex's own diffs.** Codex runs the `claude-review` skill, which starts a read-only Claude Opus session. It loads none of your settings, only the kit's own inline deny rules (no credential files, no git writes), so your allow rules cannot widen what a hostile diff can make it do.
- **Lanes.** A lane is a worktree one tool works in. `bin/lane.sh <worktree> <claude|codex>` writes `.agent-lane` there, and the other tool's guard refuses commits in it. Work crosses over through a note from `/handoff`.
- **The Codex plugin.** OpenAI's plugin adds `/codex:review`, `/codex:adversarial-review`, `/codex:rescue`, `/codex:status`, `/codex:result` and `/codex:cancel` to Claude Code. Rescue is read-only unless you pass `--write`, and the stop-time review gate stays off.

Codex reviews block on any P0 or P1 finding. The adversarial review blocks on a needs-attention verdict.

## The guard hook

A PreToolUse hook parses every shell command (quotes, pipes, `bash -c`, heredocs, `sudo` and `env` wrappers) and denies the dangerous ones, including commands hidden in `eval`, `$( )`, backticks, pipes into `sh` and heredocs fed to a shell. Each denial names the alternative. It asks only for gated models; everything else is a plain deny, so bypass mode does not stall on it. The same policy runs in Codex through its hooks, with a rules file as a second layer for the git commands. Codex rules match a command prefix, so they also forbid `git add -A <path>`, which the hook allows.

| Blocked | Use instead |
|---|---|
| `git add -A`, `git add .`, `git add ..`, `git stage`, `:(top)` pathspecs, `git commit -a` | Stage files by name. |
| Force-push to main or master (including a lease push with no refspec while on main), deleting main, `git update-ref -d` | Push a branch and open a PR. |
| Plain `git push --force`, `--mirror`, remote branch deletes | `--force-with-lease` on your own branch; push branches by name; leave remote branches alone. |
| `git reset --hard`, discarding all changes (`checkout .`, `restore .`, `switch -f`, `--discard-changes`) | `git stash push -u` first, then `git reset --keep <ref>`. |
| `git clean`, `git branch -D`, dropping stashes, history rewrites | Move untracked files to `_to_delete/`; `git branch -d`; leave stashes; add a new commit. |
| `git worktree remove` or a real `prune`, `mv` on a worktree | The `worktree` skill: `git worktree move` into `_archive/`. |
| `rm -r` outside build folders and temp | `bin/trash.py <paths>`: dated `_to_delete/`, a manifest, `--restore`. A lone `rm -r` of plain literal paths is rewritten to it; anything with shell syntax in it is denied instead. |
| `find -delete`, `find -exec rm`, bulk or pattern moves out of a repo | List the matches, show them to the user, then `bin/trash.py --approved`. |
| `DROP`, `TRUNCATE`, `ALTER ... DROP COLUMN` in SQL clients, heredocs and SQL MCP tools | Rename to `_to_delete_<date>_<name>`. |
| Reading `.env` files (cat, grep, rg, awk, sed, source, cp, globs like `.env*`) | Read `.env.example` or check variable names. |
| Writes, deletes and moves in `protected_dirs`, or of a folder that holds one | Prove the change on a scratch copy; the user decides. |
| Schema changes to a `protected_supabase` project outside `scripts/deploy` | Use the repo's deploy script. |
| Applying learning proposals directly | Leave them for the user and say how many wait. |
| Spawning a subagent on a model in `gated_models` | Not denied: Claude Code asks you first. |
| `gh pr merge` into a gated branch without both reviews, merging through `gh api` | Run `pr_review.py` for the missing one, then `gh pr merge`. |
| Committing in a worktree whose `.agent-lane` names the other tool | Leave the change uncommitted and write a handoff note. |

Two more hooks guard the instruction files. The budget hook denies edits that add log entries to an instruction file or push it past its line budget: the rulebook 120 lines, each adapter 60, a repo's AGENTS.md 150, a CLAUDE.md that imports AGENTS.md 40, each `.claude/rules` file 80. The Stop hook pushes back once when a turn ends by handing work to you that the agent could do.

A hook that fails for its own reasons lets the call through and logs to `local/learning/hook-errors.log`. Neither layer covers commands run inside scripts (`bash x.sh`, `python -c`), MCP tools other than SQL ones, or hosted tools. The hooks stop accidents, not a determined agent: an agent with shell access can edit `local/config.json` or `local/AGENTS.md` like any other file, so review what your agents change there.

## The learning loop

```
 sessions (Claude Code, Codex)
   | corrections, interrupts, denied commands, /learn <lesson>
   v
 local/learning/inbox/*.jsonl          capture: raw, local, never loaded into a session
   | /learn review: mine transcripts, build a bundle
   v
 curator (Opus, high) -------------->  local/learning/proposals/<date>.md   (at most 7)
   |
 you: yes / no / edit
   v
 bin/apply.py                          exact one-bullet edits, budget check, ledger, commit in local/
   v
 local/AGENTS.md  (or local/claude.md, local/codex.md)
   | install.sh --sync
   v
 ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md      the next session runs with the lesson
```

Hooks capture corrections in both tools without being asked. `/learn review` mines new signals, the curator drafts at most seven proposals with the evidence, a test for when each rule should and should not fire, and a review date. You answer yes, no or an edited wording in one message. Agents cannot run `apply.py` themselves; the skill hands you the one command. `/learn prune` runs monthly and proposes removing rules that stopped earning their line. The ledger in `local/learning/ledger.md` records every decision, so a rejected rule does not come back without new evidence.

## Config reference

`local/config.json` is yours. Keys that start with `_` are ignored. Hooks read the file on every run. Keys that shape generated files (`protected_dirs` deny rules, `permission_mode`, `auto_mode_environment`, `codex`) take effect after `bash install.sh --sync`.

| Key | Default | Meaning |
|---|---|---|
| `user_name` | `""` | How instructions and hook messages address you. Empty reads as "the user". |
| `projects_root` | `""` | The folder holding your repos. The pre-commit hook is installed there and `/worktree` puts worktrees there. Empty switches both off and worktrees go next to their repo. |
| `protected_dirs` | `[]` | Read-only data. A bare name (`client-exports`) matches that folder name anywhere; a path (`~/clients/acme/data`) protects that path only. The guard denies writes, deletes and moves there. |
| `protected_supabase` | `{}` | Supabase project ref mapped to a one-line note. Schema changes are denied unless they go through `scripts/deploy`. |
| `gated_models` | `["fable"]` | Subagent models that need your yes. Claude Code asks you before spawning one, the `advisor` agent included. |
| `stop_hook` | `true` | The Stop hook pushes back once when a turn ends by handing work to you. |
| `icloud_warning` | `true` | macOS: warn when a repo sits in iCloud-synced Desktop or Documents. |
| `spend_limit_usd` | `5` | What an agent may spend per action without asking. Used in the messages. |
| `permission_mode` | `""` | Claude's `defaultMode` to set: `auto`, `acceptEdits` or `bypassPermissions`. Empty leaves yours alone. |
| `auto_mode_environment` | `[]` | Extra lines for Claude's auto mode environment, such as your GitHub user and deploy targets. |
| `pr_gate.enabled` | `true` | Turn the merge gate on or off. |
| `pr_gate.branches` | `["main", "master", "production"]` | Base branches that count as deploy branches. |
| `pr_gate.repos` | `"*"` | `"*"` for every repo, or a list of `"owner/repo"`. |
| `pr_gate.reviewers` | `[]` | GitHub logins whose verdict comments count. Empty means your signed-in gh user. |
| `pr_gate.trust_codex_bot` | `false` | Count a review by Codex's GitHub app on the head commit as the Codex verdict. |
| `routing.claude_week_limit_pct` | `80` | Past this share of Claude's weekly limit, new lanes go to Codex. |
| `codex.models` | `{}` | Role (`explorer`, `worker`, `verifier`, `reviewer`, or the Claude names `Explore`, `worker`, `verifier`, `reviewer`) to Codex model name. Empty means no model line, so Codex uses its default. |
| `codex.pin_memories_off` | `false` | Write `memories = false` into Codex's config. Only done while no Codex process runs; on Python without tomllib only in simple files, otherwise it says how to set it by hand. Uninstall undoes it. |

## Updating

```
cd ~/agent-core-kit
git pull
bash install.sh --sync
```

`--sync` rebuilds the generated files and copies the kit's agents, skills and hooks. It leaves your rulebook and config alone; it only updates `local/install-state.json` and, once, `local/.gitignore`. `bash install.sh --check` lists any drift between the kit and what is installed.

Codex files other than its `AGENTS.md` (agents, hooks, rules, the memories pin) are written only while no Codex process is running, and the desktop app counts. If Codex is open, `--sync` says which files are waiting; quit Codex and run it again. Codex asks you to trust changed hooks again in `/hooks`.

## Uninstall and restore

- `bash install.sh --uninstall` takes the kit's hooks and permission rules out of `~/.claude/settings.json` and `~/.codex/hooks.json`, and moves the generated `CLAUDE.md` and `AGENTS.md`, the kit's agents, hooks, skills and Codex rules into `local/backups/<timestamp>-uninstall/`. It then puts back what install changed: your own `CLAUDE.md` and `AGENTS.md` (through symlinks), `defaultMode`, the ask rules a bypass setup dropped, the model, env and `cleanupPeriodDays` values the kit set, the Codex memories pin, and anything `--clean-slate` moved aside. A value you changed yourself since install is kept and named. Nothing is deleted. `local/` and the kit folder stay. The Codex plugin for Claude Code stays installed; remove it with `claude plugin uninstall codex@openai-codex --scope user`.
- `bash install.sh --restore <name>` puts back the files from `local/backups/<name>/`. The first install's backup holds what it replaced, such as your own `CLAUDE.md` and `settings.json`. An uninstall backup restores the kit. What was in place before a restore goes into a `<timestamp>-before-restore` backup, so a restore can be undone.

## Privacy

`local/` stays on your machine. The kit's `.gitignore` excludes it, and it is its own git repo: keep it off public remotes. The learning inbox holds the text of prompts the hooks flagged as corrections, and review bundles quote them, so treat those folders like your shell history; `local/.gitignore` keeps them, the backups, the install state and the hook log out of `local/`'s commits. Mining reads Claude Code and Codex transcripts and never changes them. `pr_review.py` redacts secret-like strings (tokens, keys, private key blocks) from a review before posting it as a PR comment. Nothing in the kit sends data anywhere by itself; the curator reads signals inside a normal Claude session when you run `/learn review`, and it is told never to quote captured text in a proposal.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.

`vendor/codex-plugin-cc` is OpenAI's Codex plugin for Claude Code (Apache-2.0, with its own `LICENSE` and `NOTICE`), pinned at commit `db52e28f4d9ded852ab3942cea316258ae4ef346`. The kit changes four things in it:

1. `agents/codex-rescue.md`: "Proactively" and the "Do not wait for the user" line are removed, so Claude only hands work to Codex when asked.
2. Rescue is read-only unless the request carries `--write`. The change is in the agent, in `commands/rescue.md` and in two lines of `skills/codex-cli-runtime/SKILL.md` that defaulted to write.
3. `commands/rescue.md` and `commands/setup.md` get `disable-model-invocation: true`, so only you can start them.
4. `commands/setup.md` loses `Bash(npm:*)` and the install block, so the plugin never installs Codex itself.
