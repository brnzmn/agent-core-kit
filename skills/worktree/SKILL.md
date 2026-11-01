---
name: worktree
description: Create, list, move, or retire git worktrees safely, outside iCloud-synced folders and without breaking git's links. Use whenever work needs a separate checkout, or when cleaning up old worktrees.
argument-hint: "new <repo> <name> | list <repo> | retire <path> | move <old> <new>"
---
# Worktrees

## Where they live
- `<projects>` is the projects folder from local/config.json (`projects_root`) when it is set, otherwise the repo's parent folder.
- A repo's worktrees go in `<projects>/<repo>-wt/<name>`. Archived ones go in `<projects>/_archive/<repo>-wt/<name>`.
- On macOS, never inside an iCloud-synced folder: ~/Documents, ~/Desktop or iCloud Drive (~/Library/Mobile Documents). iCloud evicts files and corrupts git metadata. If `<projects>` resolves into one of them, say so and ask the user for a folder outside iCloud before creating anything.
- Never move a worktree with plain `mv`; git loses the link. Use `git worktree move`.

## new
1. `git -C <repo> fetch`, then pick the base (usually the default branch).
2. `git -C <repo> worktree add <projects>/<repo>-wt/<name> -b <branch> <base>`
3. Claude Code and Codex may show a one-time trust prompt for the new folder; the user accepts it.
4. If this worktree is a lane (a separate session or tool works in it), mark it: `bash {{KIT}}/bin/lane.sh <path> <claude|codex>`. The other tool's guard then refuses commits there. A Codex lane lives at `<projects>/<repo>-wt/codex-<name>`. Don't add lane files to existing worktrees.
5. Which tool takes a new implementation lane: Claude, unless `python3 {{KIT}}/bin/usage.py` says Claude's week is past the routing threshold, then Codex.
6. Install dependencies there only if the task needs them. Report the path and branch.

## list
`git -C <repo> worktree list --porcelain`, plus for each: last commit date, ahead/behind the base, uncommitted changes. Flag the merged ones, stale ones (no commits for 30+ days), and anything marked prunable.

## retire
1. Confirm it is merged or pushed: `git -C <worktree> status`, `git -C <repo> branch --merged <base>`, or the PR state. Unpushed work stops the retire; tell the user.
2. Archive it: `git -C <repo> worktree move <path> <projects>/_archive/<repo>-wt/<name>`. The guard blocks `worktree remove` and `rm -rf`; the user does final deletions.
3. List the archived path and its branch for the user. Delete the branch only if they agree.

## move
`git -C <repo> worktree move <old> <new>`. If a worktree was already moved by hand and git lost track of it, run `git -C <repo> worktree repair <new path>` from the main checkout.

## prune
Show `git -C <repo> worktree prune --dry-run` and list what it would prune for the user. The guard blocks a real prune; a prune after a hand-move orphans worktrees.
