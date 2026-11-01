#!/usr/bin/env python3
"""Move files and folders into a dated _to_delete/ trash instead of deleting them.

  trash.py [--pattern] [--approved] [--why TEXT] PATH...   move PATHs into the trash
  trash.py --list                                          show what is in the trash (newest last)
  trash.py --restore TRASHED_PATH                          put one item back where it came from

The trash is <disk>/_to_delete/<YYYY-MM-DD>/<original absolute path>, so moves stay on the
same disk: an external disk on macOS keeps its own _to_delete folder, everything else goes
to ~/_to_delete. Every move is logged to _to_delete/manifest.jsonl. Nothing here ever
deletes; the user empties the trash themselves.

Stops and prints the full list without moving anything when the batch is over 1 GB,
holds a populated database, or came from a name pattern (--pattern). Rerun with --approved only after the user said yes
in the conversation.
Exit codes: 0 moved, 2 usage or refused path, 3 stopped for the user's review.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('AGENT_CORE_DIR', CORE)
sys.path.insert(0, os.path.join(CORE, 'claude', 'hooks'))
import common  # noqa: E402

HOME = os.path.expanduser('~')
LIMIT = 1 << 30
DB_SUFFIXES = ('.db', '.sqlite', '.sqlite3', '.duckdb')
DISKS = os.sep + 'Volumes'  # where macOS mounts external disks; each one keeps its own trash


def trash_root(path):
    parts = path.split(os.sep)
    if len(parts) > 2 and os.sep + parts[1] == DISKS:
        return os.path.join(DISKS, parts[2], '_to_delete')
    return os.path.join(HOME, '_to_delete')


def size_of(path):
    if os.path.islink(path) or not os.path.isdir(path):
        return os.lstat(path).st_size
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def sqlite_rows(path):
    """Rows across user tables, or None when the file is not a readable SQLite database."""
    try:
        with open(path, 'rb') as fh:
            if fh.read(16) != b'SQLite format 3\x00':
                return None
        con = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=1)
        try:
            names = [r[0] for r in con.execute("select name from sqlite_master where type='table' "
                                                "and name not like 'sqlite_%'")]
            return sum(con.execute('select count(*) from "%s"' % n.replace('"', '""')).fetchone()[0] for n in names)
        finally:
            con.close()
    except Exception:
        return None


def databases_in(path):
    """(path, description) for every populated database file or Postgres data folder under path."""
    found = []
    candidates = [path] if not os.path.isdir(path) else []
    if os.path.isdir(path) and not os.path.islink(path):
        for root, dirs, files in os.walk(path):
            if 'PG_VERSION' in files and 'base' in dirs:
                found.append((root, 'Postgres data folder'))
                dirs[:] = []
                continue
            candidates.extend(os.path.join(root, f) for f in files if f.lower().endswith(DB_SUFFIXES))
    for f in candidates:
        if not f.lower().endswith(DB_SUFFIXES):
            continue
        rows = sqlite_rows(f)
        if rows:
            found.append((f, 'SQLite, %d rows' % rows))
        elif rows is None and f.lower().endswith('.duckdb') and os.path.getsize(f) > 0:
            found.append((f, 'DuckDB file'))
    return found


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0


def refused(path):
    if path in ('/', HOME, DISKS, '/Users', '/home') or os.path.dirname(path) == DISKS:
        return 'is a root folder'
    if (os.sep + '_to_delete' + os.sep) in path + os.sep:
        return 'is already in the trash; the user empties it themselves'
    return None


def destination(path, day):
    root = trash_root(path)
    dest = os.path.join(root, day, path.lstrip(os.sep))
    n = 1
    while os.path.lexists(dest):
        dest = os.path.join(root, day, path.lstrip(os.sep) + '.%d' % n)
        n += 1
    return root, dest


def log(root, rec):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, 'manifest.jsonl'), 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


def move(paths, args):
    targets, errors = [], []
    for raw in paths:
        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.lexists(p):
            if not args.force_missing:
                errors.append('%s: no such file or folder' % raw)
            continue
        why = refused(p)
        if why:
            errors.append('%s %s' % (raw, why))
        else:
            targets.append(p)
    if errors:
        for e in errors:
            print('trash: ' + e, file=sys.stderr)
        print('trash: nothing was moved.', file=sys.stderr)
        return 2
    if not targets:
        return 0
    sizes = {t: size_of(t) for t in targets}
    total = sum(sizes.values())
    dbs = [d for t in targets for d in databases_in(t)]
    stops = []
    if total > LIMIT:
        stops.append('the batch is %s (over 1 GB)' % human(total))
    if dbs:
        stops.append('it holds %d populated database(s)' % len(dbs))
    if args.pattern:
        stops.append('it was matched by a name pattern')
    if stops and not args.approved:
        print('STOPPED, nothing moved: %s. Show %s this full list and wait for their yes, then rerun with '
              '--approved.' % ('; '.join(stops), common.user()))
        for t in targets:
            print('  %10s  %s' % (human(sizes[t]), t))
        for path, what in dbs:
            print('  database   %s (%s)' % (path, what))
        print('  total %s in %d item(s)' % (human(total), len(targets)))
        return 3
    day = time.strftime('%Y-%m-%d')
    for t in targets:
        root, dest = destination(t, day)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(t, dest)
        log(root, {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'from': t, 'to': dest, 'bytes': sizes[t],
                   'cwd': os.getcwd(), 'why': args.why or '', 'approved': bool(args.approved),
                   'session': os.environ.get('CLAUDE_SESSION_ID', '')})
        print('moved %s -> %s' % (t, dest))
    print('%d item(s), %s, moved to the trash. Restore with: %s --restore <path>'
          % (len(targets), human(total), common.kit_cmd('bin/trash.py')))
    return 0


def manifests():
    roots = [os.path.join(HOME, '_to_delete')]
    if os.path.isdir(DISKS):
        roots += [os.path.join(DISKS, v, '_to_delete') for v in sorted(os.listdir(DISKS))]
    for root in roots:
        m = os.path.join(root, 'manifest.jsonl')
        if os.path.isfile(m):
            with open(m, encoding='utf-8') as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        pass


def restore(trashed):
    p = os.path.abspath(os.path.expanduser(trashed))
    rec = next((r for r in reversed(list(manifests())) if r.get('to') == p), None)
    if not rec or not os.path.lexists(p):
        print('trash: %s is not in any trash manifest' % trashed, file=sys.stderr)
        return 2
    if os.path.lexists(rec['from']):
        print('trash: %s exists again; move it aside first' % rec['from'], file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(rec['from']), exist_ok=True)
    shutil.move(p, rec['from'])
    log(trash_root(rec['from']), {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'restored': rec['from'], 'from_trash': p})
    print('restored %s' % rec['from'])
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--pattern', action='store_true', help='the list came from a glob or name pattern')
    ap.add_argument('--approved', action='store_true', help='the user said yes in the conversation')
    ap.add_argument('--force-missing', '-f', action='store_true', help='skip paths that do not exist (like rm -f)')
    ap.add_argument('--why', default='')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--restore')
    args = ap.parse_args()
    if args.list:
        for r in manifests():
            print(json.dumps(r, ensure_ascii=False))
        return 0
    if args.restore:
        return restore(args.restore)
    if not args.paths:
        ap.print_usage(sys.stderr)
        return 2
    return move(args.paths, args)


if __name__ == '__main__':
    sys.exit(main())
