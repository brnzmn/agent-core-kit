#!/usr/bin/env python3
"""agent-core SessionStart context and notices (startup only).

Prints short lines, which Claude Code (and Codex) add to context:
- every session in a repo: its deploy targets, accounts and last 5 decision-log entries, when it has any
  (repo_context.py);
- at most once a day each: the kit is not set up (no local/AGENTS.md), the repo sits in an iCloud folder, its
  instruction files are over budget, corrections are waiting for review, or the installed global instructions have
  drifted from the kit's rulebook and adapters.
Only when config sync_on_start is true (default false) and AGENT_CORE_CATCH_UP is not 0: a session start also launches
bin/catch_up.py in the background when its last result is over CATCH_UP_MAX_AGE old, and says once a day when that
run could not bring the kit checkout up to GitHub, could not sync, held the sync back for uncommitted edits, or has
been offline for over a day. The hook itself makes no git or network call; it
reads the result of the previous run. With the switch off it reads and starts nothing.
A repo under the configured projects folder (config projects_root) that lacks the git pre-commit hook
(bin/git_hooks.py) gets it, silently. Nothing is created under local/ when that folder does not exist.
"""
import hashlib
import json
import math
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import repo_context  # noqa: E402

NOTICE_STATE = os.path.join(common.LEARN, '.notice_state.json')
CATCH_UP_STATUS = os.path.join(common.LEARN, '.catch_up.json')
CATCH_UP_MAX_AGE = 600
OFFLINE_NOTICE_AFTER = 86400
CATCH_UP_BLOCKED = ('blocked-dirty', 'blocked-diverged', 'other-branch', 'error')


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


def catch_up_script():
    """Path of bin/catch_up.py when the hook should drive it: switched on in config, not switched off by
    AGENT_CORE_CATCH_UP=0, the personal folder set up, the kit a git checkout and the script present."""
    if os.environ.get('AGENT_CORE_CATCH_UP') == '0' or common.config().get('sync_on_start') is not True:
        return None
    script = os.path.join(common.CORE, 'bin', 'catch_up.py')
    if os.path.isdir(common.LOCAL) and os.path.exists(os.path.join(common.CORE, '.git')) and os.path.isfile(script):
        return script
    return None


def catch_up_notice(script, seen, today):
    """The notice for the last catch-up's result (read from its status file), and start a new catch-up in the
    background when that result is missing or old. Nothing here waits: no git call, no network call."""
    note = None
    try:
        with open(CATCH_UP_STATUS, encoding='utf-8') as fh:
            status = json.load(fh)
        age = time.time() - float(status['at'])
        if not math.isfinite(age):
            raise ValueError('at is not a finite number')
    except Exception:
        status, age = None, None
    if isinstance(status, dict) and seen.get('catchup') != today:
        line = str(status.get('line') or '')[:300]
        sync = ('install.sh --sync failed on %s, so the installed hooks and rules are old. Run: %s --sync'
                % (common.tilde(common.CORE), common.kit_cmd('install.sh')))
        if status.get('outcome') in CATCH_UP_BLOCKED and line:
            note = line
        elif status.get('attention') and line:
            note = line + (' ' + sync if status.get('sync') == 'failed' else '')
        elif status.get('sync') == 'failed':
            note = sync
        elif status.get('outcome') == 'offline':
            since = status.get('offline_since')
            if isinstance(since, (int, float)) and not isinstance(since, bool) and math.isfinite(since) \
                    and 0 < since < time.time() - OFFLINE_NOTICE_AFTER:
                note = ('agent-core-kit: the background check has not reached GitHub since %s, so the rules here may be '
                        'behind. Run: git -C %s fetch origin main'
                        % (time.strftime('%Y-%m-%d', time.localtime(since)), common.tilde(common.CORE)))
        if note:
            seen['catchup'] = today
    if age is None or age > CATCH_UP_MAX_AGE or age < 0:
        try:
            subprocess.Popen([sys.executable, script, '--quiet', '--core', common.CORE, '--status-file', CATCH_UP_STATUS],
                             cwd=common.CORE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as exc:
            common.log_error('catch_up', exc)
    return note


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
        script = catch_up_script()
        if script:
            note = catch_up_notice(script, seen, today)
            if note:
                notes.append(note)
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
