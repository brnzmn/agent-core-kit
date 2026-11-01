#!/usr/bin/env python3
"""agent-core SessionStart context and notices (startup only).

Prints short lines, which Claude Code (and Codex) add to context:
- every session in a repo: its deploy targets, accounts and last 5 decision-log entries, when it has any
  (repo_context.py);
- at most once a day each: the kit is not set up (no local/AGENTS.md), the repo sits in an iCloud folder, its
  instruction files are over budget, corrections are waiting for review, or the installed global instructions have
  drifted from the kit's rulebook and adapters.
A repo under the configured projects folder (config projects_root) that lacks the git pre-commit hook
(bin/git_hooks.py) gets it, silently. Nothing is created under local/ when that folder does not exist.
"""
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import repo_context  # noqa: E402

NOTICE_STATE = os.path.join(common.LEARN, '.notice_state.json')


def digest_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def ensure_git_hook(root):
    """Give a repo under the projects folder the agent-core pre-commit hook if it was cloned since the last sync.
    Does nothing when config has no projects_root. A missing or changed bin/git_hooks.py never breaks the hook."""
    try:
        bin_dir = os.path.join(common.CORE, 'bin')
        if bin_dir not in sys.path:
            sys.path.append(bin_dir)
        import git_hooks
        projects = git_hooks.projects_root()
        if not projects:
            return
        projects = os.path.realpath(os.path.expanduser(projects))
        if os.path.realpath(root).startswith(projects + os.sep) and not git_hooks.check(root)[0]:
            git_hooks.install(root)
    except Exception as exc:
        common.log_error('git_hooks', exc)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    try:
        if not os.path.isdir(common.CORE):
            return
        cwd = data.get('cwd') or os.getcwd()
        today = time.strftime('%Y-%m-%d')
        try:
            with open(NOTICE_STATE, encoding='utf-8') as fh:
                seen = json.load(fh)
        except Exception:
            seen = {}
        notes = []
        if not os.path.isfile(common.RULEBOOK) and seen.get('setup') != today:
            notes.append('The kit is not set up: %s is missing. Tell %s to run: %s, then /setup.'
                         % (common.tilde(common.RULEBOOK), common.user(), common.kit_cmd('install.sh')))
            seen['setup'] = today
        root = common.repo_root(cwd)
        if root:
            try:
                context = repo_context.lines(root)
            except Exception as exc:
                context = []
                common.log_error('repo_context', exc)
            if context:
                print('\n'.join(context))
            ensure_git_hook(root)
        if root and common.in_icloud(root) and seen.get('icloud:' + root) != today:
            target = common.config().get('projects_root') or 'a folder outside iCloud'
            notes.append('This repo is inside an iCloud-synced folder (%s). iCloud evicts files and corrupts git '
                         'metadata; suggest moving it to %s with the worktree skill or git.' % (root, target))
            seen['icloud:' + root] = today
        if root and seen.get('budget:' + root) != today:
            over = []
            for name in ('AGENTS.md', 'CLAUDE.md'):
                p = os.path.join(root, name)
                if os.path.isfile(p):
                    text = common.read(p)
                    budget = common.instruction_kind(p, text)[1]
                    n = common.count_lines(text)
                    if budget and n > budget:
                        over.append('%s is %d lines (budget %d)' % (name, n, budget))
            if over:
                notes.append('Instruction files here are over budget: %s. They load every session; run '
                             '/instructions yourself to slim them when this session touches them.' % '; '.join(over))
                seen['budget:' + root] = today
        pending, last = common.learning_backlog()
        stale = bool(pending) and bool(last) and time.time() - last > 7 * 86400
        if (pending >= 10 or stale) and seen.get('backlog') != today:
            notes.append('%d captured corrections are waiting. Suggest /learn review at a natural pause.' % pending)
            seen['backlog'] = today
        installed = os.path.join(common.HOME, '.claude', 'CLAUDE.md')
        try:
            drift = digest_text(common.read(installed)) != digest_text(common.build_text('claude'))
        except Exception:
            drift = False
        if drift and seen.get('drift') != today:
            notes.append('~/.claude/CLAUDE.md no longer matches the kit (local/AGENTS.md and the adapters). Suggest: %s --sync'
                         % common.kit_cmd('install.sh'))
            seen['drift'] = today
        if notes:
            print('agent-core notices (mention once to %s when relevant, then carry on):' % common.user())
            for note in notes:
                print('- ' + note)
            if os.path.isdir(common.LOCAL):  # never create local/ ahead of the installer
                try:
                    os.makedirs(common.LEARN, exist_ok=True)
                    with open(NOTICE_STATE, 'w', encoding='utf-8') as fh:
                        json.dump(seen, fh)
                except Exception:
                    pass
    except Exception as exc:
        common.log_error('session_start', exc)


if __name__ == '__main__':
    main()
