#!/usr/bin/env python3
"""agent-core-kit learning loop helper.

  learn.py add "<lesson>"    log a lesson (what /learn <lesson> runs)
  learn.py mine [--days N]   pull correction signals from Claude Code and Codex transcripts into the inbox
  learn.py bundle [--max N]  write a review bundle for the curator; prints its path and the proposals path
  learn.py close <review dir> mark that bundle's signals reviewed when the curator proposed nothing
  learn.py prune-bundle      write a prune bundle: rules due for review, budgets, large auto-memory files
  learn.py status            counts and dates

Learning targets are the user's own files in local/: the rulebook (AGENTS.md) and the optional personal adapters
(claude.md, codex.md). The shipped adapters/ are kit code and are never targets.
"""
import argparse
import glob
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

PROJECTS = os.path.join(common.HOME, '.claude', 'projects')
CODEX_HOME = os.environ.get('CODEX_HOME') or os.path.join(common.HOME, '.codex')
# archived threads move out of sessions/; a moved file is read again and its signals dedupe by id
CODEX_SESSIONS = [os.path.join(CODEX_HOME, d) for d in ('sessions', 'archived_sessions')]
PROPOSALS = os.path.join(common.LEARN, 'proposals')
REVIEW = os.path.join(common.LEARN, 'review')
LEDGER = os.path.join(common.LEARN, 'ledger.md')
WRAPPER = re.compile(r'^\s*<(command-name|command-message|command-args|local-command-stdout|local-command-stderr|'
                     r'system-reminder|bash-input|bash-stdout|bash-stderr|user-memory-input)\b')


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text') or '' for b in content if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def is_tool_result(content):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content)


def one_line(s, n):
    s = re.sub(r'\s+', ' ', str(s)).strip()
    return s if len(s) <= n else s[:n - 3] + '...'


def line_count(path):
    try:
        return common.count_lines(common.read(path))
    except OSError:
        return None


def scope_files():
    """(target file name, path, line budget) for the user's rulebook and each personal adapter that exists."""
    out = [('AGENTS.md', common.RULEBOOK, common.BUDGETS['core'])]
    for name in ('claude.md', 'codex.md'):
        path = os.path.join(common.LOCAL, name)
        if os.path.isfile(path):
            out.append((name, path, common.BUDGETS['adapter']))
    return out


def usage(path, budget):
    n = line_count(path)
    return 'missing, run install.sh' if n is None else '%d/%d lines' % (n, budget)


def cmd_add(args):
    text = ' '.join(args.text).strip()
    if not text:
        print('nothing to log')
        return 1
    common.append_inbox({'kind': 'manual', 'score': 3, 'signals': ['manual'], 'text': text[:1200],
                         'cwd': os.getcwd(), 'session': os.environ.get('CLAUDE_SESSION_ID', '')})
    print('logged')
    return 0


def recent(pattern, cutoff):
    out = []
    for path in glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(path) >= cutoff:
                out.append(path)
        except OSError:
            pass
    return sorted(out)


def new_lines(path, offsets, skipped=None):
    """Lines added since the last mine. A .zst rollout is final once written, so it is read whole the first time
    and skipped while its size is unchanged. Without zstd (the macOS default) it is left unread, not recorded in
    offsets, and appended to skipped so a later run with zstd installed picks it up."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    start = offsets.get(path, 0)
    if start > size:
        start = 0
    if start == size:
        return []
    if path.endswith('.zst'):
        try:
            out = subprocess.run(['zstd', '-dcq', path], capture_output=True, timeout=60)
        except OSError:  # zstd is not installed or cannot run
            if skipped is not None:
                skipped.append(path)
            return []
        offsets[path] = size
        return out.stdout.splitlines() if out.returncode == 0 else []
    with open(path, 'rb') as fh:
        fh.seek(start)
        chunk = fh.read()
    cut = chunk.rfind(b'\n') + 1
    offsets[path] = start + cut
    return chunk[:cut].splitlines()


class Miner:
    def __init__(self):
        self.known = set(r.get('id') for r in common.inbox_records())
        self.found = 0

    def turn(self, text, context, interrupted, tool, cwd, session, ts):
        score, signals = common.score_prompt(text)
        if interrupted:
            score, signals = score + 2, signals + ['after-interrupt']
        if score < 2:
            return
        rec = {'kind': 'mined', 'tool': tool, 'score': score, 'signals': signals, 'text': text[:800],
               'context': context[-600:], 'cwd': cwd, 'session': session, 'ts': ts}
        rec['id'] = common.make_id(rec)
        if rec['id'] in self.known:
            return
        common.append_inbox(rec)
        self.known.add(rec['id'])
        self.found += 1


def mine_claude(path, lines, miner):
    last_assistant, after_interrupt = '', None
    for raw in lines:
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        msg = ev.get('message') or {}
        if ev.get('type') == 'assistant':
            t = text_of(msg.get('content'))
            if t.strip():
                last_assistant = t
            continue
        if ev.get('type') != 'user' or ev.get('isMeta') or ev.get('isSidechain'):
            continue
        content = msg.get('content')
        if is_tool_result(content):
            continue
        t = text_of(content)
        if not t.strip() or WRAPPER.match(t):
            continue
        if '[Request interrupted by user' in t:
            after_interrupt = last_assistant
            continue
        miner.turn(t, after_interrupt if after_interrupt is not None else last_assistant, after_interrupt is not None,
                   'claude', ev.get('cwd') or '', ev.get('sessionId') or '', ev.get('timestamp') or '')
        after_interrupt = None


def mine_codex(path, lines, miner):
    """Both rollout shapes: paginated (user turns as event_msg item_completed UserMessage) and legacy
    (event_msg user_message). Assistant text is the response_item assistant message in both."""
    try:
        meta = common.codex_meta(path)  # a .zst rollout is read through zstd
    except (OSError, subprocess.SubprocessError):
        return
    if not meta or not common.codex_typed(meta):
        return
    session, cwd = meta.get('id') or meta.get('session_id') or '', meta.get('cwd') or ''
    last_assistant, after_interrupt = '', None
    for raw in lines:
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        p = ev.get('payload') if isinstance(ev.get('payload'), dict) else {}
        kind = (ev.get('type'), p.get('type'))
        if ev.get('type') == 'turn_context':
            cwd = p.get('cwd') or cwd
        elif kind == ('response_item', 'message') and p.get('role') == 'assistant':
            t = '\n'.join(b.get('text') or '' for b in p.get('content') or [] if isinstance(b, dict))
            if t.strip():
                last_assistant = t
        elif kind == ('event_msg', 'turn_aborted'):
            after_interrupt = last_assistant
        elif kind in (('event_msg', 'item_completed'), ('event_msg', 'user_message')):
            if kind[1] == 'user_message':
                t = p.get('message') or ''
            elif (p.get('item') or {}).get('type') == 'UserMessage':
                t = text_of(p['item'].get('content'))
            else:
                continue
            t = common.codex_user_text(t)
            if not t:
                continue
            miner.turn(t, after_interrupt if after_interrupt is not None else last_assistant,
                       after_interrupt is not None, 'codex', cwd, session, ev.get('timestamp') or '')
            after_interrupt = None


def cmd_mine(args):
    st = common.load_state()
    offsets = st.get('mine_offsets') or {}
    cutoff = time.time() - args.days * 86400
    miner = Miner()
    claude = recent(os.path.join(PROJECTS, '*', '*.jsonl'), cutoff)
    codex = [p for d in CODEX_SESSIONS for p in recent(os.path.join(d, '**', 'rollout-*.jsonl*'), cutoff)
             if p.endswith(('.jsonl', '.jsonl.zst'))]
    skipped = []
    for path in claude:
        mine_claude(path, new_lines(path, offsets), miner)
    for path in codex:
        lines = new_lines(path, offsets, skipped)
        if lines:
            mine_codex(path, lines, miner)
    st['mine_offsets'] = offsets
    st['last_mine_ts'] = time.time()
    common.save_state(st)
    print('mined %d new signal(s) from %d Claude and %d Codex transcript file(s) changed in the last %d days'
          % (miner.found, len(claude), len(codex) - len(skipped), args.days))
    if skipped:
        print('%d compressed Codex sessions skipped: install zstd to include them' % len(skipped))
    return 0


def unreviewed():
    done = set(common.load_state().get('processed_ids') or [])
    seen, out = set(), []
    for r in common.inbox_records():
        i = r.get('id')
        if i and i not in done and i not in seen:
            seen.add(i)
            out.append(r)
    return out


def cmd_bundle(args):
    recs = unreviewed()
    if not recs:
        print('nothing to review')
        return 0
    recs.sort(key=lambda r: (-int(r.get('score') or 0), str(r.get('ts') or '')))
    total, recs = len(recs), recs[:args.max]
    date = time.strftime('%Y-%m-%d')
    rdir = os.path.join(REVIEW, date)
    os.makedirs(rdir, exist_ok=True)
    os.makedirs(PROPOSALS, exist_ok=True)
    bundle, proposals = os.path.join(rdir, 'bundle.md'), os.path.join(PROPOSALS, date + '.md')
    roots = set()
    for r in recs:
        root = common.repo_root(r.get('cwd') or '')
        if root:
            roots.add(root)
    lines = ['# Learning review bundle %s' % date, '',
             '%d signal(s) below, %d unreviewed in total. Write proposals to: %s' % (len(recs), total, proposals), '',
             'Captured text and context may hold personal or client data. Never quote or paraphrase them in a '
             'proposal: state the lesson and cite the signal number only.', '',
             '## Files in scope (use the target exactly as written, with a section heading after the #)']
    for name, path, budget in scope_files():
        what = 'the rulebook shared by Claude Code and Codex' if name == 'AGENTS.md' else 'personal additions for one tool'
        lines.append('- target %s#<section>: %s (%s; %s)' % (name, path, what, usage(path, budget)))
    for name in ('claude.md', 'codex.md'):
        path = os.path.join(common.LOCAL, name)
        if not os.path.isfile(path):
            lines.append('- target %s#<section>: %s (not created yet; the first add creates it)' % (name, path))
    lines.append('- Ledger of past decisions: %s' % LEDGER)
    for root in sorted(roots):
        a = os.path.join(root, 'AGENTS.md')
        if os.path.isfile(a):
            lines.append('- target repo:%s#<section>: %s' % (a, usage(a, common.BUDGETS['agents'])))
        else:
            lines.append('- Repo %s: no AGENTS.md yet' % root)
        for rule in sorted(glob.glob(os.path.join(root, '.claude', 'rules', '*.md'))):
            lines.append('- target repo:%s#<section>: %s' % (rule, usage(rule, common.BUDGETS['rule'])))
    lines += ['', '## Signals, highest score first', '']
    for n, r in enumerate(recs, 1):
        where = common.repo_root(r.get('cwd') or '') or r.get('cwd') or '?'
        lines.append('### %d. %s | %s | %s | %s | score %s | %s' % (
            n, str(r.get('ts') or '')[:16], r.get('tool') or 'claude', r.get('kind'), where, r.get('score'),
            ', '.join(r.get('signals') or [])))
        if r.get('context'):
            lines.append('Assistant just before: ' + one_line(r['context'], 500))
        if r.get('reason'):
            lines.append('Denial reason: ' + one_line(r['reason'], 300))
        lines.append('Captured: ' + one_line(r.get('text') or '', 800))
        lines.append('')
    with open(bundle, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    with open(os.path.join(rdir, 'ids.json'), 'w', encoding='utf-8') as fh:
        json.dump([r['id'] for r in recs], fh)
    print('bundle: %s' % bundle)
    print('proposals: %s' % proposals)
    print('signals: %d (of %d unreviewed)' % (len(recs), total))
    return 0


def ledger_rows():
    rows = []
    if not os.path.isfile(LEDGER):
        return rows
    for line in common.read(LEDGER).splitlines():
        if not line.startswith('| ') or line.startswith('| date ') or line.startswith('|---'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) >= 8:
            rows.append(dict(zip(('date', 'id', 'status', 'action', 'target', 'rule', 'why', 'review_by'), cells[:8])))
    return rows


def memory_files():
    out = []
    for p in glob.glob(os.path.join(PROJECTS, '*', 'memory', 'MEMORY.md')):
        try:
            out.append((p, common.count_lines(common.read(p))))
        except OSError:
            pass
    return out


def cmd_prune_bundle(args):
    today = time.strftime('%Y-%m-%d')
    due = [r for r in ledger_rows() if r['status'].startswith('applied') and r['review_by'] and r['review_by'] <= today]
    rdir = os.path.join(REVIEW, today + '-prune')
    os.makedirs(rdir, exist_ok=True)
    os.makedirs(PROPOSALS, exist_ok=True)
    bundle, proposals = os.path.join(rdir, 'prune.md'), os.path.join(PROPOSALS, today + '-prune.md')
    lines = ['# Prune bundle %s' % today, '', 'Propose keep or remove only. Write proposals to: %s' % proposals, '',
             '## Rules past their review date']
    lines += ['- %s | %s | %s | why: %s' % (r['id'], r['target'], r['rule'], r['why']) for r in due] or ['- none']
    lines += ['', '## Budgets']
    for name, path, budget in scope_files():
        lines.append('- %s: %s' % (path, usage(path, budget)))
    lines += ['', '## Auto-memory files over 150 lines (Claude loads the first 200 every session)']
    lines += ['- %s: %d lines' % (p, n) for p, n in memory_files() if n > 150] or ['- none']
    lines += ['', 'Also read the whole rulebook and the personal adapters. Flag rules that are contradicted, '
              'duplicated, or no longer needed with current models.']
    with open(bundle, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    print('bundle: %s' % bundle)
    print('proposals: %s' % proposals)
    print('rules due for review: %d' % len(due))
    return 0


def cmd_status(args):
    pending, last = common.learning_backlog()
    props = []
    if os.path.isdir(PROPOSALS):
        props = sorted(f for f in os.listdir(PROPOSALS) if f.endswith('.md') and not f.endswith('.done.md'))
    rows = ledger_rows()
    today = time.strftime('%Y-%m-%d')
    applied = sum(1 for r in rows if r['status'].startswith('applied'))
    rejected = sum(1 for r in rows if r['status'] == 'rejected')
    due = sum(1 for r in rows if r['status'].startswith('applied') and r['review_by'] and r['review_by'] <= today)
    print('inbox: %d unreviewed signal(s)' % pending)
    print('last review: %s' % (time.strftime('%Y-%m-%d', time.localtime(last)) if last else 'never'))
    print('open proposal files: %s' % (', '.join(props) or 'none'))
    print('ledger: %d applied, %d rejected, %d due for review' % (applied, rejected, due))
    for name, path, budget in scope_files():
        print('%s: %s' % (name, usage(path, budget)))
    big = [p for p, n in memory_files() if n > 150]
    if big:
        print('auto-memory files over 150 lines: %d (run /learn prune)' % len(big))
    if os.path.isfile(common.ERRLOG):
        n = len(common.read(common.ERRLOG).splitlines())
        if n:
            print('hook errors logged: %d, see %s' % (n, common.ERRLOG))
    return 0


def cmd_close(args):
    """The curator read a bundle and proposed nothing: its signals are reviewed, so the next bundle moves on to
    others. apply.py does the same for a proposals file once every proposal in it is decided."""
    ids_file = os.path.join(os.path.abspath(os.path.expanduser(args.review_dir)), 'ids.json')
    try:
        with open(ids_file, encoding='utf-8') as fh:
            ids = set(json.load(fh))
    except (OSError, ValueError) as exc:
        print('no ids to close: %s' % exc)
        return 1
    st = common.load_state()
    before = set(st.get('processed_ids') or [])
    st['processed_ids'] = sorted(before | ids)
    st['last_review_ts'] = time.time()
    common.save_state(st)
    print('closed %d signal(s)' % len(ids - before))
    return 0


def main():
    ap = argparse.ArgumentParser(description='agent-core-kit learning loop')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('add').add_argument('text', nargs='+')
    sub.add_parser('mine').add_argument('--days', type=int, default=14)
    sub.add_parser('bundle').add_argument('--max', type=int, default=150)
    sub.add_parser('close').add_argument('review_dir')
    sub.add_parser('prune-bundle')
    sub.add_parser('status')
    args = ap.parse_args()
    handler = {'add': cmd_add, 'mine': cmd_mine, 'bundle': cmd_bundle, 'close': cmd_close,
               'prune-bundle': cmd_prune_bundle, 'status': cmd_status}.get(args.cmd)
    if not handler:
        ap.print_help()
        return 1
    return handler(args)


if __name__ == '__main__':
    sys.exit(main())
