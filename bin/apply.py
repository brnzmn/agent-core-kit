#!/usr/bin/env python3
"""Apply learning-loop decisions.

  apply.py <proposals.md> --approve P1,P3 [--reject P2] [--done P4] [--edit 'P3=<new rule text>']

Allowed targets (the curator writes them in each proposal):
  AGENTS.md#<section>        the user's rulebook, local/AGENTS.md
  claude.md#<section>        personal additions for Claude Code, local/claude.md (created on the first add)
  codex.md#<section>         personal additions for Codex, local/codex.md (created on the first add)
  repo:<path>#<section>      a repo's AGENTS.md or .claude/rules/*.md
  manual:<change>            something no bullet can do (hook, permission, skill); approved here, done by hand
Anything else is refused, including the shipped adapters/ (kit code).

Approved add/replace/remove proposals become exact bullet edits in their target file; nothing is rewritten
wholesale, and an edit that would break a line budget is refused. Every decision is appended to
local/learning/ledger.md. Changes inside local/ are committed to its git repo, files staged by name, as
agent-core-kit <agent-core-kit@localhost> when git has no user.email of its own.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('AGENT_CORE_DIR', CORE)
sys.path.insert(0, os.path.join(CORE, 'claude', 'hooks'))
import common  # noqa: E402

LEDGER = os.path.join(common.LEARN, 'ledger.md')
FIELD = re.compile(r'^-\s*(action|target|rule|replaces|why|evidence|test|retire-if|review-by):\s*(.*)$')
FINAL = ('applied', 'rejected', 'applied-manual')
LOCAL_FILES = ('AGENTS.md', 'claude.md', 'codex.md')  # exact names: CLAUDE.md is a repo file, not a personal adapter
PERSONAL_ADAPTERS = ('claude.md', 'codex.md')
KIT_CODE = ('adapters', 'templates', 'vendor')  # never learning targets, even through repo:
NOT_ALLOWED = ('failed: target not allowed (use AGENTS.md#, claude.md#, codex.md#, '
               'repo:<AGENTS.md or .claude/rules/*.md>#, or manual:)')


def parse(path):
    props, cur = {}, None
    for line in common.read(path).splitlines():
        m = re.match(r'^##\s+(P\d+)\s*:\s*(.*)$', line.strip())
        if m:
            cur = {'id': m.group(1).upper(), 'title': m.group(2).strip()}
            props[cur['id']] = cur
            continue
        f = FIELD.match(line.strip()) if cur is not None else None
        if f:
            cur[f.group(1)] = f.group(2).strip()
    return props


def id_list(s):
    return [x.strip().upper() for x in (s or '').split(',') if x.strip()]


def inside(path, folder):
    folder = os.path.abspath(folder)
    return path == folder or path.startswith(folder + os.sep)


def resolve(target):
    """(path, section). path is the file, 'manual', 'forbidden', or None for an empty target."""
    t = (target or '').strip()
    if t.lower().startswith('manual:'):
        return 'manual', None
    path, _, section = t.partition('#')
    path = path.strip()
    if not path:
        return None, None
    if path.lower().startswith('repo:'):
        fp = os.path.abspath(os.path.expanduser(path[5:].strip()))
        ok = os.path.basename(fp) == 'AGENTS.md' or (os.sep + os.path.join('.claude', 'rules') + os.sep) in fp
        ok = ok and fp.endswith('.md') and not any(inside(fp, os.path.join(CORE, d)) for d in KIT_CODE)
        return (fp if ok else 'forbidden'), section.strip()
    return (os.path.join(common.LOCAL, path) if path in LOCAL_FILES else 'forbidden'), section.strip()


def find_section(lines, section):
    want = section.strip().lower()
    for i, line in enumerate(lines):
        m = re.match(r'^(#{1,6})\s+(.*?)\s*$', line)
        if m and m.group(2).strip().lower() == want:
            level = len(m.group(1))
            for j in range(i + 1, len(lines)):
                m2 = re.match(r'^(#{1,6})\s+', lines[j])
                if m2 and len(m2.group(1)) <= level:
                    return i, j
            return i, len(lines)
    return None, None


def apply_one(p, rule):
    path, section = resolve(p.get('target'))
    action = (p.get('action') or '').strip().lower()
    if path == 'manual':
        return 'approved-manual', None
    if path == 'forbidden':
        return NOT_ALLOWED, None
    if not path:
        return 'failed: proposal has no target', None
    personal = os.path.dirname(path) == common.LOCAL and os.path.basename(path) in PERSONAL_ADAPTERS
    if os.path.isfile(path):
        old = common.read(path)
    elif personal and action == 'add':
        old = ''
    else:
        return 'failed: target file not found', None
    lines = old.split('\n') if old else []
    if action in ('add', 'replace') and not rule:
        return 'failed: proposal has no rule text', None
    if action == 'add':
        if any(l.strip() == '- ' + rule for l in lines):
            return 'failed: rule already present', None
        start, end = find_section(lines, section) if section else (None, None)
        if start is None:
            if not (personal and section):
                return 'failed: section "%s" not found' % section, None
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                lines.append('')
            lines += ['### ' + section, '', '- ' + rule, '']
        else:
            at = end
            while at > start + 1 and not lines[at - 1].strip():
                at -= 1
            lines.insert(at, '- ' + rule)
    elif action in ('replace', 'remove'):
        old_rule = (p.get('replaces') or '').strip()
        hits = [i for i, l in enumerate(lines) if old_rule and l.strip() in ('- ' + old_rule, '* ' + old_rule)]
        if len(hits) != 1:
            return 'failed: rule to %s %s' % (action, 'not found' if not hits else 'appears more than once'), None
        i = hits[0]
        if action == 'replace':
            indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
            lines[i] = indent + '- ' + rule
        else:
            del lines[i]
    else:
        return 'failed: unknown action %r' % action, None
    new = '\n'.join(lines)
    budget = common.instruction_kind(path, new)[1]
    if budget and common.count_lines(new) > budget and common.count_lines(new) > common.count_lines(old):
        return 'failed: would exceed the %d-line budget; pair it with a removal' % budget, None
    return 'applied', (path, new)


def prior_decisions(stem):
    """Latest status per proposal id for this proposals file, from the ledger."""
    out = {}
    if not os.path.isfile(LEDGER):
        return out
    for line in common.read(LEDGER).splitlines():
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) >= 3 and cells[1].startswith(stem + '/'):
            out[cells[1].split('/', 1)[1].upper()] = cells[2]
    return out


def cell(s, n=140):
    s = re.sub(r'\s+', ' ', str(s or '')).replace('|', '/').strip()
    return s if len(s) <= n else s[:n - 3] + '...'


KIT_IDENTITY = ['-c', 'user.name=agent-core-kit', '-c', 'user.email=agent-core-kit@localhost']


def git(*args, options=()):
    return subprocess.run(['git', '-C', common.LOCAL] + list(options) + list(args), capture_output=True, text=True)


def identity_options():
    """git -c options naming the kit as committer, only when git has no user.email for LOCAL. Nothing is written
    to any config file."""
    return [] if git('config', 'user.email').stdout.strip() else KIT_IDENTITY


def git_commit(files, message):
    """Commit the given files in the git repo that is LOCAL, staged by name. Never touches another repo."""
    if not os.path.exists(os.path.join(common.LOCAL, '.git')):
        return 'not committed: %s is not a git repo; the changes are saved there' % common.tilde(common.LOCAL)
    base = os.path.abspath(common.LOCAL) + os.sep
    rels = sorted(set(os.path.relpath(f, common.LOCAL) for f in files if os.path.abspath(f).startswith(base)))
    if not rels:
        return 'nothing to commit'
    try:
        paths = []
        for rel in rels:
            if os.path.lexists(os.path.join(common.LOCAL, rel)):
                if git('check-ignore', '-q', '--', rel).returncode == 0:  # an ignored file stays out of the commit
                    continue
                git('add', '--', rel)
                paths.append(rel)
            elif git('ls-files', '--error-unmatch', '--', rel).returncode == 0:  # tracked, now gone: a renamed file
                paths.append(rel)
        if not paths or not git('status', '--porcelain', '--', *paths).stdout.strip():
            return 'nothing new to commit'
        r = git('commit', '-q', '-m', message, '--', *paths,  # only these paths, whatever else is staged
                options=identity_options())
        why = ((r.stderr or r.stdout).strip().splitlines() or ['git commit failed'])[0]
        return 'committed' if r.returncode == 0 else 'not committed: %s (the changes are saved; commit them in %s)' % (
            why[:160], common.tilde(common.LOCAL))
    except Exception as exc:
        return 'not committed: %r' % exc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)  # --appr must not quietly mean --approve
    ap.add_argument('proposals')
    ap.add_argument('--approve', default='')
    ap.add_argument('--reject', default='')
    ap.add_argument('--done', default='', help='approved manual proposals you have now implemented')
    ap.add_argument('--edit', action='append', default=[], help="'P3=<new rule text>'")
    args = ap.parse_args()
    path = os.path.abspath(os.path.expanduser(args.proposals))
    try:
        props = parse(path)
    except OSError as exc:
        print('cannot read %s: %s' % (path, exc.strerror or exc))
        return 1
    if not props:
        print('no proposals found in %s' % path)
        return 1
    edits = {}
    for e in args.edit:
        k, _, v = e.partition('=')
        edits[k.strip().upper()] = v.strip()
    approve, reject, done = id_list(args.approve), id_list(args.reject), id_list(args.done)
    unknown = [i for i in approve + reject + done + list(edits) if i not in props]
    if unknown:
        print('unknown proposal ids: %s' % ', '.join(unknown))
        return 1
    stem = os.path.basename(path)
    for suffix in ('.done.md', '.md'):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    today = datetime.date.today()
    review_by = (today + datetime.timedelta(days=90)).isoformat()
    changed, rows, statuses = set(), [], {}
    prior = prior_decisions(stem)
    for pid in sorted(props, key=lambda x: int(x[1:])):
        p = props[pid]
        rule = edits.get(pid) or p.get('rule') or ''
        if pid in approve and prior.get(pid) == 'applied':
            status, result = 'applied', None
            print('%s already applied earlier; skipped' % pid)
            statuses[pid] = status
            continue
        if pid in approve:
            status, result = apply_one(p, rule)
            if result:
                fp, new = result
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, 'w', encoding='utf-8') as fh:
                    fh.write(new)
                changed.add(fp)
        elif pid in reject:
            status = 'rejected'
        elif pid in done:
            status = 'applied-manual'
        elif pid in prior and prior[pid] != 'approved-manual':
            statuses[pid] = prior[pid]
            continue
        else:
            status = 'deferred' if pid not in prior else 'approved-manual'
        statuses[pid] = status
        if status != 'deferred' and not (status == 'approved-manual' and prior.get(pid) == 'approved-manual'):
            rows.append('| %s | %s/%s | %s | %s | %s | %s | %s | %s |' % (
                today.isoformat(), stem, pid, cell(status, 60), cell(p.get('action'), 10), cell(p.get('target'), 80),
                cell(rule), cell(p.get('why')), (p.get('review-by') if re.fullmatch(r'\d{4}-\d\d-\d\d', p.get('review-by') or '')
                                                  else review_by) if status.startswith('applied') else ''))
        print('%s %s  %s' % (pid, status, p.get('target') or ''))
    os.makedirs(common.LEARN, exist_ok=True)
    if not os.path.isfile(LEDGER):
        with open(LEDGER, 'w', encoding='utf-8') as fh:
            fh.write('# Learning ledger\n\n| date | id | status | action | target | rule | why | review-by |\n'
                     '|---|---|---|---|---|---|---|---|\n')
    if rows:
        with open(LEDGER, 'a', encoding='utf-8') as fh:
            fh.write('\n'.join(rows) + '\n')
    final = all(s in FINAL for s in statuses.values())
    failed = any(s.startswith('failed') for s in statuses.values())
    st = common.load_state()
    ids_file = os.path.join(common.LEARN, 'review', stem, 'ids.json')
    if final and os.path.isfile(ids_file):
        with open(ids_file, encoding='utf-8') as fh:
            st['processed_ids'] = sorted(set(st.get('processed_ids') or []) | set(json.load(fh)))
    st['last_review_ts'] = time.time()
    common.save_state(st)
    record, renamed = path, []
    if final and path.endswith('.md') and not path.endswith('.done.md'):
        record = path[:-3] + '.done.md'
        os.replace(path, record)
        renamed = [path]  # the old name leaves the repo if it was ever committed
    msg = 'learn: %s (%s)' % (stem, ', '.join('%s %s' % (k, v.split(':')[0]) for k, v in statuses.items()))
    print(git_commit(sorted(changed) + [LEDGER, record] + renamed, msg))
    local = os.path.abspath(common.LOCAL) + os.sep
    for f in sorted(f for f in changed if not os.path.abspath(f).startswith(local)):
        print('changed %s; commit it in that repo when ready' % f)
    if any(os.path.abspath(f).startswith(local) for f in changed):
        print('next: %s --sync' % common.kit_cmd('install.sh'))
    if failed:
        print('some proposals failed; fix their target or wording in %s and rerun' % os.path.basename(record))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
