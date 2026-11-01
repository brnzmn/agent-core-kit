#!/usr/bin/env python3
"""Lint agent instruction files.

  lint.py                     the kit's own sources: your local/AGENTS.md, the adapters, local/claude.md and local/codex.md
  lint.py <repo-or-file> ...  a repo (root and nested AGENTS.md/CLAUDE.md, .claude/rules) or single files
Exits 1 when a file is over its line budget or holds a log section. Warnings alone pass.
"""
import glob
import os
import re
import sys

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('AGENT_CORE_DIR', CORE)
sys.path.insert(0, os.path.join(CORE, 'claude', 'hooks'))
import common  # noqa: E402

SHOUT = re.compile(r'\b(MUST|NEVER|ALWAYS|IMPORTANT|CRITICAL|MANDATORY|REQUIRED)\b')
TICKED = re.compile(r'`([^`\n]+)`')
FILE_EXT = re.compile(r'\.(md|mdx|ts|tsx|js|jsx|mjs|cjs|py|json|jsonc|toml|ya?ml|sh|sql|txt|css|html|swift|sol|rs|go)$')
PATH_ROOTS = ('.', 'src', 'docs', 'scripts', 'apps', 'packages', 'lib', 'test', 'tests', 'app', 'public', 'supabase', 'bin')
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', '.next', '.turbo', 'worktrees', '.venv', 'venv', 'vendor', 'coverage'}
INSTRUCTION_NAMES = ('AGENTS.md', 'CLAUDE.md', 'CLAUDE.local.md', 'GEMINI.md')
TEMPLATE = os.path.join(CORE, 'templates', 'core', 'AGENTS.md')  # the default rulebook has the rulebook's budget


def path_candidates(text):
    for m in TICKED.finditer(text):
        t = m.group(1).strip()
        if not t or ' ' in t or any(ch in t for ch in '*<>{}$=()|;,!?'):
            continue
        if t.startswith(('http:', 'https:', '-', '@', '#', '.env')):
            continue
        if '/' not in t and not FILE_EXT.search(t):
            continue
        if FILE_EXT.fullmatch(t) or t in ('/', '~'):
            continue
        if '/' in t and not FILE_EXT.search(t) and not t.startswith(PATH_ROOTS + ('~', '/')):
            continue
        yield t


def lint_file(path, base=None, check_paths=True, budget=None):
    try:
        text = common.read(path)
    except OSError as exc:
        return [('ERROR', 'unreadable: %s' % exc)], 0, None
    if not budget and os.path.abspath(path) == TEMPLATE:
        budget = common.BUDGETS['core']
    budget = budget or common.instruction_kind(path, text)[1]
    n = common.count_lines(text)
    issues = []
    heads, dated, marks = common.log_signals(text.splitlines())
    if budget and n > budget:
        issues.append(('ERROR', '%d lines, budget %d' % (n, budget)))
    if heads:
        issues.append(('ERROR', 'log or status section "%s"' % heads[0].strip()[:70]))
    if len(dated) + len(marks) >= 3:
        issues.append(('WARN', '%d dated progress or status lines' % (len(dated) + len(marks))))
    shouts = len(SHOUT.findall(text))
    if shouts > 5:
        issues.append(('WARN', '%d all-caps imperatives; give the reason instead' % shouts))
    if check_paths:
        root = base or os.path.dirname(os.path.abspath(path))
        missing = []
        for t in sorted(set(path_candidates(text))):
            p = os.path.expanduser(t) if t.startswith('~') else (t if os.path.isabs(t) else os.path.join(root, t))
            if not os.path.exists(p):
                missing.append(t)
        if missing:
            more = ' (+%d more)' % (len(missing) - 8) if len(missing) > 8 else ''
            issues.append(('WARN', 'referenced paths not found: %s%s' % (', '.join(missing[:8]), more)))
    return issues, n, budget


def repo_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        dirnames[:] = [] if depth >= 4 else [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            p = os.path.join(dirpath, f)
            in_rules = (os.sep + os.path.join('.claude', 'rules') + os.sep) in p and f.endswith('.md')
            if f in INSTRUCTION_NAMES or in_rules:
                found.append(p)
    return sorted(found)


def show(rel, n, budget, issues):
    status = '%d/%s lines' % (n, budget if budget else '-')
    errors = 0
    if not issues:
        print('  OK     %s (%s)' % (rel, status))
    for level, msg in issues:
        print('  %-5s  %s (%s): %s' % (level, rel, status, msg))
        errors += level == 'ERROR'
    return errors


def lint_repo(root):
    root = os.path.abspath(root)
    print('== %s' % root)
    notes = []
    agents, claude = os.path.join(root, 'AGENTS.md'), os.path.join(root, 'CLAUDE.md')
    if os.path.isfile(agents) and not os.path.isfile(claude):
        notes.append('AGENTS.md without CLAUDE.md: Claude Code skips AGENTS.md when any parent folder has a '
                     'CLAUDE.md. Add a CLAUDE.md containing @AGENTS.md.')
    if os.path.isfile(agents) and os.path.isfile(claude) and '@AGENTS.md' not in common.read(claude):
        notes.append('CLAUDE.md does not import AGENTS.md (@AGENTS.md), so the two will drift.')
    gitignore = os.path.join(root, '.gitignore')
    if os.path.isfile(gitignore) and re.search(r'^\s*/?\.claude/?\s*$', common.read(gitignore), re.M):
        notes.append('.gitignore ignores all of .claude/, so shared rules, skills and settings.json cannot be committed.')
    wt = os.path.join(root, '.claude', 'worktrees')
    if os.path.isdir(wt):
        k = len([d for d in os.listdir(wt) if os.path.isdir(os.path.join(wt, d))])
        if k:
            notes.append('%d worktrees under .claude/worktrees each carry their own copy of these files.' % k)
    if common.in_icloud(root):
        where = configured_projects_root()
        notes.append('This repo sits in an iCloud-synced folder, which evicts files and corrupts git metadata; move it %s.'
                     % (('under ' + where) if where else 'out of Desktop and Documents'))
    for note in notes:
        print('  WARN   ' + note)
    errors = 0
    files = repo_files(root)
    if not files:
        print('  WARN   no instruction files found')
    for p in files:
        issues, n, budget = lint_file(p, base=os.path.dirname(p))
        errors += show(os.path.relpath(p, root), n, budget, issues)
    return errors


def configured_projects_root():
    root = str(common.config().get('projects_root') or '').strip()
    return common.tilde(root) if root else ''


def main(argv):
    targets = argv[1:]
    errors = 0
    if not targets:
        print('== agent-core-kit sources (%s)' % common.tilde(CORE))
        if os.path.isfile(common.RULEBOOK):
            issues, n, budget = lint_file(common.RULEBOOK, check_paths=False)
            errors += show(common.tilde(common.RULEBOOK), n, budget, issues)
        else:
            print('  WARN   %s not found; run install.sh to create it' % common.tilde(common.RULEBOOK))
        if os.path.isfile(TEMPLATE):
            issues, n, budget = lint_file(TEMPLATE, check_paths=False)
            errors += show(os.path.relpath(TEMPLATE, CORE), n, budget, issues)
        extra = [p for p in (os.path.join(common.LOCAL, 'claude.md'), os.path.join(common.LOCAL, 'codex.md')) if os.path.isfile(p)]
        for p in sorted(glob.glob(os.path.join(CORE, 'adapters', '*.md'))) + extra:
            issues, n, budget = lint_file(p, check_paths=False)
            errors += show(common.tilde(p) if p in extra else os.path.relpath(p, CORE), n, budget, issues)
        for p in sorted(glob.glob(os.path.join(CORE, 'skills', '*', 'SKILL.md'))):
            n = common.count_lines(common.read(p))
            print('  %-5s  %s (%d lines)' % ('WARN' if n > 300 else 'OK', os.path.relpath(p, CORE), n))
        return 1 if errors else 0
    for t in targets:
        t = os.path.abspath(os.path.expanduser(t))
        if os.path.isdir(t):
            errors += lint_repo(t)
        elif os.path.isfile(t):
            issues, n, budget = lint_file(t)
            errors += show(t, n, budget, issues)
        else:
            print('not found: %s' % t)
            errors += 1
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
