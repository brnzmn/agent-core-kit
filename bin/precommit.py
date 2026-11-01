#!/usr/bin/env python3
"""agent-core-kit git pre-commit check for instruction files, run by the hook bin/git_hooks.py installs.

Rejects the commit when a staged CLAUDE.md or AGENTS.md (at any depth)
- grows past its line budget (150 lines; 40 for a CLAUDE.md that only imports AGENTS.md; a file already over
  its budget may still shrink), or
- adds log-shaped lines: a status or changelog heading, dated progress entries, or status marks.
Says where that text goes instead. Its own errors never block a commit.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'claude', 'hooks'))
import common  # noqa: E402

NAMES = ('CLAUDE.md', 'AGENTS.md')


def git(*args):
    out = subprocess.run(['git'] + list(args), capture_output=True, timeout=20)
    return out.stdout.decode('utf-8', 'replace') if out.returncode == 0 else None


def problems():
    staged = git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z') or ''
    found = []
    for path in [p for p in staged.split('\0') if os.path.basename(p) in NAMES]:
        new = git('show', ':' + path)
        if new is None:
            continue
        old = git('show', 'HEAD:' + path) or ''
        old_n, new_n = common.count_lines(old), common.count_lines(new)
        limit = common.instruction_kind(path, new)[1] or 150
        if new_n > limit and new_n > old_n:
            found.append('%s would be %d lines (limit %d, was %d). Procedures go in a skill, path-specific rules in '
                         '.claude/rules/, background in docs/.' % (path, new_n, limit, old_n))
        example = common.log_entry(common.added_lines(old, new))
        if example:
            found.append('%s gains a log or status entry ("%s"). Build logs and status go in docs/build-log.md, the PR, '
                         'or the issue tracker; decisions go in docs/decisions.md.' % (path, example))
    return found


def main():
    try:
        found = problems()
    except Exception as exc:
        common.log_error('precommit', exc)
        return 0
    if not found:
        return 0
    print('agent-core: commit rejected. CLAUDE.md and AGENTS.md load into every agent session, so they hold '
          'standing rules only.', file=sys.stderr)
    for line in found:
        print('- ' + line, file=sys.stderr)
    print('Unstage or move that text, then commit again.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
