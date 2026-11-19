#!/usr/bin/env python3
"""Bring a checkout of agent-core-kit up to origin/main by fast-forward, then run install.sh --sync.

    catch_up.py [--core DIR] [--quiet] [--no-sync] [--first-time] [--status-file FILE]

Standalone on purpose (standard library only, no import from the kit): the first run on a machine can be a copy of
this file outside the checkout, and the checkout it repairs may be too old or too broken to import from.

The checkout is --core, else $AGENT_CORE_DIR, else the folder above this script's bin/ when that is a git checkout,
else ~/agent-core-kit. Nothing under the kit's local/ folder is ever touched except the status file, which lives in
local/learning/. --first-time archives under <checkout>/backups/ (git-ignored).

Outcomes: current, offline, updated, blocked-dirty, blocked-diverged, other-branch, error.
Exit codes: 0 current, offline or updated; 2 usage or not a checkout; 3 blocked, other branch or error;
4 updated but install.sh --sync failed or was held back by uncommitted edits; 5 --first-time could not archive or move
safely. --first-time that cannot reach GitHub exits 3 (a background run that is offline exits 0).
"""
import argparse
import hashlib
import json
import os
import re
import filecmp
import shlex
import shutil
import signal
import subprocess
import sys
import time

BRANCH = 'main'
LOCK_NAME = 'agent-core-kit-catch-up.lock'
LOCK_MAX_AGE = 600
SYNC_TIMEOUT = 300
GIT_ENV_DROP = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_PREFIX', 'GIT_OBJECT_DIRECTORY')


class Stop(Exception):
    """End the run with a known outcome."""

    def __init__(self, code, outcome, line, **extra):
        Exception.__init__(self, line)
        self.code, self.outcome, self.line, self.extra = code, outcome, line, extra


def git_env(core=None):
    """No prompts of any kind: a session start must never sit waiting for a password or a passphrase. The batch-mode
    ssh is only a default: GIT_SSH_COMMAND, GIT_SSH and core.sshCommand are the owner's choice of transport and win."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    env['GIT_TERMINAL_PROMPT'] = '0'
    if not env.get('GIT_SSH_COMMAND') and not env.get('GIT_SSH'):
        configured = ''
        if core:
            try:
                configured = subprocess.run(['git', '-C', core, 'config', 'core.sshCommand'], capture_output=True,
                                            text=True, stdin=subprocess.DEVNULL, env=env, timeout=10).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        if not configured:
            env['GIT_SSH_COMMAND'] = 'ssh -o BatchMode=yes'
    return env


def run(cmd, cwd=None, timeout=None, env=None):
    """(returncode, stdout, stderr). A timeout kills the whole process group (git leaves helpers behind) and returns
    rc None. stdin is closed and the child gets its own session, so nothing can read a terminal."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env or git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors='replace', start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, err = proc.communicate()
        return None, out, err
    return proc.returncode, out, err


class Repo:
    def __init__(self, core):
        self.core = core
        self.env = git_env(core) if os.path.isdir(core) else git_env()

    def git(self, *args, timeout=60):
        return run(['git', '-C', self.core] + list(args), timeout=timeout, env=self.env)

    def out(self, *args):
        rc, out, err = self.git(*args)
        if rc != 0:
            last = ((err or out).strip().splitlines() or [''])[-1]
            raise Stop(3, 'error', 'agent-core-kit: git %s failed in %s: %s' % (args[0], tilde(self.core), last))
        return out.strip()


def tilde(path):
    home = os.path.expanduser('~')
    return '~' + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def shell_path(path):
    """A path for a command the user will paste: quoted, but a leading home folder stays as ~ so it still expands."""
    home = os.path.expanduser('~')
    if path == home:
        return '~'
    if path.startswith(home + os.sep):
        return '~/' + shlex.quote(path[len(home) + 1:])
    return shlex.quote(path)


def short(sha):
    return sha[:7]


def names(files):
    """Up to five file names for a one-line message."""
    shown = ', '.join(files[:5])
    return shown + (' and %d more' % (len(files) - 5) if len(files) > 5 else '')


def read_status(path):
    """The previous run's status record, or {} when there is none or it cannot be read."""
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_json(path, data):
    """Atomic, so a session start reading the file never sees half of it."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def acquire_lock(path):
    """True when this run owns the lock. A lock younger than LOCK_MAX_AGE belongs to a live catch-up; an older one is a
    crash leftover and is replaced."""
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) < LOCK_MAX_AGE:
                    return False
                os.unlink(path)
            except OSError:
                pass
            continue
        except OSError:
            return False
        with os.fdopen(fd, 'w') as fh:
            fh.write('%d\n' % os.getpid())
        return True
    return False


def fetch_timeout():
    try:
        return float(os.environ.get('AGENT_CORE_FETCH_TIMEOUT') or 8)
    except ValueError:
        return 8.0


def check_checkout(repo):
    """Exit 2 unless core is the top of a git checkout that has a remote called origin."""
    shown = tilde(repo.core)
    if not os.path.isdir(repo.core):
        raise Stop(2, 'not-a-checkout', 'agent-core-kit: %s does not exist.' % shown)
    rc, top, _ = repo.git('rev-parse', '--show-toplevel')
    if rc != 0 or os.path.realpath(top.strip()) != repo.core:
        raise Stop(2, 'not-a-checkout', 'agent-core-kit: %s is not a git checkout.' % shown)
    if repo.git('remote', 'get-url', 'origin')[0] != 0:
        raise Stop(2, 'not-a-checkout', 'agent-core-kit: %s has no remote named origin.' % shown)


def tracked_changes(repo):
    """Tracked files that differ from HEAD in the index or the working tree. Renames count as two paths."""
    names_ = worktree_changes(repo)
    cached = repo.out('diff', '--cached', '--name-only', '-z', '--no-renames', 'HEAD')
    return names_ + [p for p in cached.split('\0') if p and p not in names_]


def worktree_changes(repo):
    """Tracked files whose working copy differs from HEAD (staged additions included)."""
    out = repo.out('diff', '--name-only', '-z', '--no-renames', 'HEAD')
    return [p for p in out.split('\0') if p]


def edited_files(repo):
    """Tracked files with a change in the index or the working tree, by git status (untracked files do not count).
    Renames and copies list both paths."""
    rc, out, err = repo.git('status', '--porcelain', '--untracked-files=no', '-z')
    if rc != 0:
        last = ((err or out).strip().splitlines() or [''])[-1]
        raise Stop(3, 'error', 'agent-core-kit: git status failed in %s: %s' % (tilde(repo.core), last))
    parts, files, i = out.split('\0'), [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        files.append(entry[3:])
        if (entry[0] in 'RC' or entry[1] in 'RC') and i < len(parts) and parts[i]:
            files.append(parts[i])
            i += 1
    return files


def held_text(repo, files):
    return 'has uncommitted edits in %d file(s) (%s)' % (len(files), names(files))


def update_names(repo):
    out = repo.out('diff', '--name-only', '-z', '--no-renames', 'HEAD', 'origin/' + BRANCH)
    return {p for p in out.split('\0') if p}


def sync_command(repo):
    return shell_path(os.path.join(repo.core, 'install.sh')) + ' --sync'


def installed():
    """True when install.sh has installed the kit for this user (the installer's own test: guard.py and .core-dir in
    the hooks folder). A catch-up updates an installed kit. It never performs the first install: install.sh --sync
    would do one, and that changes how every session on the machine behaves."""
    hooks = os.path.join(os.path.expanduser('~'), '.claude', 'hooks', 'agent-core')
    return os.path.isfile(os.path.join(hooks, 'guard.py')) and os.path.isfile(os.path.join(hooks, '.core-dir'))


def not_installed_note(repo):
    return ('the kit is not installed for this user, so nothing was synced; to install it run: bash %s'
            % shell_path(os.path.join(repo.core, 'install.sh')))


def sync_install(repo):
    """True when install.sh --sync exited 0."""
    script = os.path.join(repo.core, 'install.sh')
    if not os.path.isfile(script):
        return False
    rc, _, _ = run(['bash', script, '--sync'], cwd=repo.core, timeout=SYNC_TIMEOUT, env=dict(os.environ))
    return rc == 0


def untracked_files(repo):
    raw = repo.out('ls-files', '--others', '--exclude-standard', '-z')
    return {p for p in raw.split('\0') if p}


def ignored_additions(repo):
    """Paths GitHub adds that exist here as git-ignored files: a merge overwrites those without asking (an untracked
    one makes git refuse, which is safe)."""
    raw = repo.out('diff', '--name-only', '-z', '--no-renames', '--diff-filter=A', 'HEAD', 'origin/' + BRANCH)
    untracked = untracked_files(repo)
    return sorted(p for p in raw.split('\0')
                  if p and os.path.lexists(os.path.join(repo.core, p)) and p not in untracked)


def save_ignored(repo, paths, folder=None):
    """Copy those files into the backup folder and check the copies. Returns (count, folder)."""
    if not paths:
        return 0, folder
    folder = folder or os.path.join(repo.core, 'backups', 'catch-up-' + unique_stamp(repo))
    for rel in paths:
        src, dst = os.path.join(repo.core, rel), os.path.join(folder, 'ignored-overwritten', rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=False)
            same = os.path.islink(src) or filecmp.cmp(src, dst, shallow=False)
        except OSError:
            same = False
        if not same:
            raise Stop(3, 'error', 'agent-core-kit: %s was not updated: could not copy the ignored file %s that GitHub now '
                       'tracks into %s.' % (tilde(repo.core), rel, tilde(folder)))
    return len(paths), folder


def fast_forward(repo, args, old):
    """Merge origin/main into a checkout with no local commits. Git refuses when an uncommitted edit touches a
    file the update changes and leaves the tree alone, which is the blocked-dirty case."""
    behind = int(repo.out('rev-list', '--count', 'HEAD..origin/' + BRANCH))
    kept, kept_folder = save_ignored(repo, ignored_additions(repo))
    rc, out, err = repo.git('merge', '--ff-only', '-q', 'origin/' + BRANCH)
    if rc != 0:
        edits = tracked_changes(repo)
        if edits:
            hit = sorted(update_names(repo) & set(edits)) or sorted(edits)
            raise Stop(3, 'blocked-dirty', 'agent-core-kit: %s is %d commit(s) behind GitHub and has uncommitted edits in '
                       '%s; not updated.' % (tilde(repo.core), behind, names(hit)), behind=behind, head=old)
        lines = (err or out).strip().splitlines()
        header = lines[0].strip() if lines else 'git merge failed'
        paths = []
        for text in lines[1:]:
            if not text[:1].isspace():
                break
            paths.append(text.strip())
        raise Stop(3, 'error', 'agent-core-kit: %s could not be fast-forwarded (%s%s); not updated.'
                   % (tilde(repo.core), header, ' ' + names(paths) if paths else ''))
    new = repo.out('rev-parse', 'HEAD')
    span = '%s..%s' % (short(old), short(new))
    extra = {}
    if kept:
        extra = dict(attention=True)
    note = (' %d local ignored file(s) GitHub now tracks were copied to %s/ignored-overwritten first.'
            % (kept, tilde(kept_folder))) if kept else ''
    if args.no_sync:
        why = ': ' + not_installed_note(repo) if args.not_installed else ''
        return dict(outcome='updated', sync='skipped', head=new, line='agent-core-kit: updated %s (not synced%s).%s'
                    % (span, why, note), **extra)
    edits = [] if args.first_time else edited_files(repo)
    if edits:  # the sync installs the working tree as it is: a half-finished edit must not reach the running tools
        return dict(outcome='updated', sync='held', head=new, attention=True,
                    line='agent-core-kit: updated %s; not installed: %s %s. Commit or discard them, then run: bash %s%s'
                    % (span, tilde(repo.core), held_text(repo, edits), sync_command(repo), note))
    if sync_install(repo):
        return dict(outcome='updated', sync='ok', head=new, line='agent-core-kit: updated %s and synced%s' % (span, note),
                    **extra)
    return dict(outcome='updated', sync='failed', head=new, line='agent-core-kit: updated %s, sync failed, run: bash %s%s'
                % (span, sync_command(repo), note), **extra)


def classify_and_update(repo, args):
    """The normal run: returns a result dict, or raises Stop for a blocked state."""
    shown = tilde(repo.core)
    head = repo.out('rev-parse', 'HEAD')
    branch = repo.git('symbolic-ref', '--short', '-q', 'HEAD')[1].strip()
    if branch != BRANCH:
        raise Stop(3, 'other-branch', 'agent-core-kit: %s is on %s, not %s; not updated.'
                   % (shown, branch or 'a detached HEAD', BRANCH), head=head)
    if run(['git', '-C', repo.core, 'fetch', '-q', 'origin', BRANCH], timeout=fetch_timeout(),
            env=repo.env)[0] != 0 \
            or repo.git('rev-parse', '--verify', '-q', 'origin/' + BRANCH + '^{commit}')[0] != 0:
        if args.first_time:
            raise Stop(3, 'offline', 'agent-core-kit: could not reach GitHub; %s not updated; nothing was changed.'
                       % shown, head=head)
        return dict(outcome='offline', head=head, line='agent-core-kit: could not reach GitHub; %s not updated.' % shown)
    ahead, behind = (int(n) for n in repo.out('rev-list', '--left-right', '--count', 'HEAD...origin/' + BRANCH).split())
    # Commits GitHub lacks do not matter while nothing new waits on GitHub: the rules already here are the newest.
    if behind == 0:
        return dict(outcome='current', head=head, line='agent-core-kit: %s is up to date.' % shown)
    if ahead:
        raise Stop(3, 'blocked-diverged', 'agent-core-kit: %s has %d local commit(s) GitHub lacks and is %d behind; not '
                   'updated.' % (shown, ahead, behind), head=head, ahead=ahead, behind=behind)
    return fast_forward(repo, args, head)


def unique_stamp(repo):
    """A timestamp no backup folder and no backup branch uses yet."""
    base = time.strftime('%Y%m%d-%H%M%S')
    stamp, n = base, 1
    while os.path.exists(os.path.join(repo.core, 'backups', 'catch-up-' + stamp)) \
            or repo.git('rev-parse', '--verify', '-q', 'refs/heads/backup/local-' + stamp)[0] == 0:
        n += 1
        stamp = '%s-%d' % (base, n)
    return stamp


def save_patch(repo, path, args, paths=()):
    """git diff --binary with these arguments into a file. Bytes: an edit in a non-UTF-8 file must survive."""
    cmd = ['git', '--literal-pathspecs', '-C', repo.core, 'diff', '--binary'] + list(args)
    if paths:
        cmd += ['--'] + list(paths)
    with open(path, 'wb') as fh:
        done = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=repo.env,
                              timeout=60)
    if done.returncode != 0:
        raise Stop(5, 'error', 'agent-core-kit: could not save the uncommitted edits (%s); nothing was changed.'
                   % done.stderr.decode('utf-8', 'replace').strip())


def archive(repo, old, parts, folder, stamp):
    """Save everything first-time is about to move off: the tracked edits as a stash-style commit (kept alive by a
    branch; it records the index and the working tree), patches and file copies as a second copy, the local commits
    and a branch at the old HEAD. Untracked files stay where they are. Returns (local commits, stash commit)."""
    os.makedirs(folder)
    if parts['patch']:
        save_patch(repo, os.path.join(folder, 'uncommitted.patch'), ['HEAD'], parts['patch'])
    else:
        open(os.path.join(folder, 'uncommitted.patch'), 'wb').close()
    if parts['added']:
        save_patch(repo, os.path.join(folder, 'added-files.patch'), ['HEAD'], parts['added'])
    if parts['cached']:
        save_patch(repo, os.path.join(folder, 'staged.patch'), ['--cached', 'HEAD'])
    for rel in parts['files']:
        src = os.path.join(repo.core, rel)
        if os.path.islink(src) or os.path.isfile(src):
            dst = os.path.join(folder, 'files', rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=False)
    stash = ''
    if parts['files']:
        rc, out, err = repo.git('stash', 'create', 'catch-up ' + stamp)
        stash = out.strip()
        if rc != 0 or not stash:
            raise Stop(5, 'error', 'agent-core-kit: could not save the uncommitted edits as a commit (%s); nothing was '
                       'changed.' % (err.strip() or 'git stash create printed nothing'))
        rc, _, err = repo.git('branch', 'backup/edits-' + stamp, stash)
        if rc != 0:
            raise Stop(5, 'error', 'agent-core-kit: could not create the backup branch for the edits (%s); nothing was '
                       'changed.' % err.strip())
    log = repo.out('log', '--format=%H %s', 'origin/%s..HEAD' % BRANCH)
    with open(os.path.join(folder, 'local-commits.txt'), 'w', encoding='utf-8') as fh:
        fh.write(log + '\n' if log else '')
    commits = [tuple(line.split(' ', 1)) if ' ' in line else (line, '') for line in log.splitlines()]
    if commits:
        rc, _, err = repo.git('format-patch', '-q', '-o', os.path.join(folder, 'patches'), 'origin/%s..HEAD' % BRANCH)
        if rc != 0:
            raise Stop(5, 'error', 'agent-core-kit: could not save the local commits as patches (%s); nothing was moved.'
                       % err.strip())
    rc, _, err = repo.git('branch', 'backup/local-' + stamp, old)
    if rc != 0:
        raise Stop(5, 'error', 'agent-core-kit: could not create the backup branch (%s); nothing was moved.' % err.strip())
    return commits, stash


def verify_archive(repo, old, parts, stash, folder, stamp):
    """Stop before anything changes unless the archive can bring everything back."""
    problems = []
    for name, wanted in (('uncommitted.patch', parts['patch']), ('added-files.patch', parts['added']),
                         ('staged.patch', parts['cached'])):
        patch = os.path.join(folder, name)
        present = os.path.isfile(patch) and os.path.getsize(patch) > 0
        if bool(wanted) != present and (wanted or os.path.exists(patch)):
            problems.append('%s is %s' % (name, 'empty' if wanted else 'not empty'))
    for rel in parts['files']:
        src, dst = os.path.join(repo.core, rel), os.path.join(folder, 'files', rel)
        if os.path.islink(src) or os.path.isfile(src):
            try:
                if os.lstat(dst).st_size != os.lstat(src).st_size:
                    problems.append('files/%s differs in size' % rel)
            except OSError:
                problems.append('files/%s is missing' % rel)
    if stash and repo.git('rev-parse', 'refs/heads/backup/edits-' + stamp)[1].strip() != stash:
        problems.append('backup branch for the edits does not point at the saved commit')
    if repo.git('rev-parse', 'refs/heads/backup/local-' + stamp)[1].strip() != old:
        problems.append('backup branch does not point at the old HEAD')
    if problems:
        raise Stop(5, 'error', 'agent-core-kit: the backup in %s did not verify (%s); nothing was changed.'
                   % (tilde(folder), '; '.join(problems)))


def collisions(repo, in_head, added):
    """Untracked paths that exist here and that origin/main wants to create: git reset --keep refuses for them,
    after the edits were already put back to their committed state."""
    return sorted(update_names(repo) & untracked_files(repo))


def file_state(core, rel):
    """(mode, content hash) of a path, or None when it does not exist. A symlink counts by its target."""
    path = os.path.join(core, rel)
    try:
        st = os.lstat(path)
        if os.path.islink(path):
            return (st.st_mode, os.readlink(path))
        with open(path, 'rb') as fh:
            return (st.st_mode, hashlib.sha256(fh.read()).hexdigest())
    except OSError:
        return None


def snapshot(repo, files):
    """What the checkout looks like before the first change: git's own status plus mode and hash of each edited file."""
    status = repo.git('status', '--porcelain=v1', '-z')[1]
    return status, {rel: file_state(repo.core, rel) for rel in files}


def put_back(repo, old, stash, github_adds, kept, folder):
    """After a failed move: back to the old HEAD, remove only what GitHub's update added that was not here before,
    re-apply the saved edits with their index. Returns when done; the caller compares with the snapshot."""
    repo.git('reset', '-q', '--hard', old)
    for rel in github_adds:
        path = os.path.join(repo.core, rel)
        try:
            if rel in kept:
                shutil.copy2(os.path.join(folder, 'ignored-overwritten', rel), path, follow_symlinks=False)
            elif os.path.lexists(path) and not os.path.isdir(path):
                os.unlink(path)
        except OSError:
            pass
    if stash:
        repo.git('stash', 'apply', '-q', '--index', stash)


def way_back(repo, stamp, stash):
    """The commands that bring the edits back: in place, and (always clean) on the old commit."""
    core = shell_path(repo.core)
    return ('cd %s && git stash apply --index %s' % (core, stash),
            'cd %s && git switch backup/local-%s && git stash apply --index %s' % (core, stamp, stash))


def report_lines(repo, old, new, why, stamp, folder, parts, stash, commits, kept):
    lines = ['agent-core-kit: %s reconciled with GitHub (%s).' % (tilde(repo.core), why),
             '  was %s, now %s (origin/%s)' % (short(old), short(new), BRANCH),
             '  backup folder: %s' % tilde(folder),
             '  backup branch: backup/local-%s (at %s)' % (stamp, short(old))]
    if stash:
        lines.append('  backup branch for the edits: backup/edits-%s (at %s)' % (stamp, short(stash)))
        lines.append('  uncommitted edits archived (%d): %s' % (len(parts['files']), ', '.join(parts['files'])))
        here, fallback = way_back(repo, stamp, stash)
        lines.append('    to undo (only if you want the old edits back): ' + here)
        lines.append('    if that conflicts: ' + fallback)
        lines.append('    copies of every edited file: %s/files; patches next to it' % tilde(folder))
    if parts['added']:
        lines.append('    staged new files (%s) were removed from the checkout, as git stash does; the commands above '
                     'bring them back staged' % ', '.join(parts['added']))
    if kept:
        lines.append('    ignored files GitHub now tracks were overwritten; the old copies are in %s/ignored-overwritten'
                     % tilde(folder))
    if commits:
        lines.append('  local commits archived (%d):' % len(commits))
        lines.extend('    %s %s' % (short(sha), subject) for sha, subject in commits)
        lines.append('    to undo (only if you want the old commits back): cd %s && git cherry-pick %s   or: cd %s && '
                     'git checkout backup/local-%s'
                     % (shell_path(repo.core), ' '.join(short(sha) for sha, _ in reversed(commits)),
                        shell_path(repo.core), stamp))
    lines.append("  Nothing above needs doing: GitHub's version is now in place.")
    return lines


class Shield:
    """Ignore interrupts while the checkout is half moved: the report must reach the screen first."""
    signals = [getattr(signal, n) for n in ('SIGINT', 'SIGTERM', 'SIGHUP') if hasattr(signal, n)]

    def __enter__(self):
        self.saved = {sig: signal.getsignal(sig) for sig in self.signals}
        for sig in self.signals:
            signal.signal(sig, signal.SIG_IGN)
        return self

    def __exit__(self, *exc):
        self.restore()

    def restore(self):
        for sig, handler in self.saved.items():
            signal.signal(sig, handler)


def refuse_unfinished(repo):
    """Stop a --first-time run while a merge, cherry-pick or revert is in progress, before anything is archived or moved:
    the put-back's reset --hard would drop that state, and a fast-forward does not check for a revert."""
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD'):
        if repo.git('rev-parse', '-q', '--verify', name)[0] == 0:
            raise Stop(5, 'error', 'agent-core-kit: %s has a merge, cherry-pick or revert in progress; finish or abort it '
                       'first. Nothing was changed.' % tilde(repo.core), head=repo.git('rev-parse', 'HEAD')[1].strip())


def first_time(repo, args, why):
    """Move a checkout that cannot fast-forward onto origin/main, archiving what it holds first."""
    old = repo.out('rev-parse', 'HEAD')
    if repo.out('ls-files', '-u'):
        raise Stop(5, 'error', 'agent-core-kit: %s has an unfinished merge; finish or abort it first. Nothing was changed.'
                   % tilde(repo.core))
    refuse_unfinished(repo)
    files = tracked_changes(repo)
    in_head = [p for p in files if repo.git('cat-file', '-e', 'HEAD:' + p)[0] == 0]
    added = [p for p in files if p not in in_head]
    work = worktree_changes(repo)
    cached = repo.out('diff', '--cached', '--name-only', '-z', '--no-renames', 'HEAD')
    parts = dict(files=files, added=added, cached=[p for p in cached.split('\0') if p],
                 patch=[p for p in work if p in in_head])
    stamp = unique_stamp(repo)
    folder = os.path.join(repo.core, 'backups', 'catch-up-' + stamp)
    commits, stash = archive(repo, old, parts, folder, stamp)
    verify_archive(repo, old, parts, stash, folder, stamp)
    hit = collisions(repo, in_head, added)
    if hit:
        raise Stop(5, 'error', 'agent-core-kit: %s was not changed: GitHub now has files that exist here untracked (%s). '
                   'Move them aside and run the same command again. The backup in %s was kept.'
                   % (tilde(repo.core), names(hit), tilde(folder)))
    ignored = ignored_additions(repo)
    kept, _ = save_ignored(repo, ignored, folder)  # git overwrites an ignored file without asking
    github_adds = sorted(p for p in update_names(repo) if repo.git('cat-file', '-e', 'HEAD:' + p)[0] != 0)
    new_expected = repo.out('rev-parse', 'origin/' + BRANCH)
    lines = report_lines(repo, old, new_expected, why, stamp, folder, parts, stash, commits, kept)
    with open(os.path.join(folder, 'REPORT.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    before = snapshot(repo, files)
    print('agent-core-kit: backup folder %s' % tilde(folder), flush=True)
    print('agent-core-kit: backup branches backup/local-%s%s; the full report is in %s/REPORT.txt'
          % (stamp, (' and backup/edits-' + stamp) if stash else '', tilde(folder)), flush=True)
    shield = Shield().__enter__()
    try:
        steps = []
        if in_head:
            steps.append(('--literal-pathspecs', 'checkout', 'HEAD', '--') + tuple(in_head))
        if added:  # as git stash does: a file that is staged but not in HEAD would block both reset and stash apply
            steps.append(('--literal-pathspecs', 'rm', '-q', '-f', '--') + tuple(added))
        steps.append(('reset', '--keep', 'origin/' + BRANCH))
        for step in steps:
            rc, out, err = repo.git(*step)
            if rc != 0:
                reason = ((err or out).strip().splitlines() or ['git failed'])[0]
                put_back(repo, old, stash, github_adds, set(ignored), folder)
                if snapshot(repo, files) == before:
                    raise Stop(5, 'error', 'agent-core-kit: %s could not be moved (%s). It was put back and compared with '
                               'its state before the run; the checkout is as it was.' % (tilde(repo.core), reason))
                raise Stop(5, 'error', 'agent-core-kit: %s could not be moved (%s) and the checkout is now in a mixed state; '
                           'nothing is lost. Restore it with: cd %s && git reset --hard backup/local-%s%s'
                           % (tilde(repo.core), reason, shell_path(repo.core), stamp,
                              (' && git stash apply --index ' + stash) if stash else ''))
        new = repo.out('rev-parse', 'HEAD')
        print('\n'.join(report_lines(repo, old, new, why, stamp, folder, parts, stash, commits, kept)), flush=True)
    finally:
        shield.restore()
    synced = 'skipped' if args.no_sync else ('ok' if sync_install(repo) else 'failed')
    if synced == 'failed':
        print('  install.sh --sync failed; run: bash %s' % sync_command(repo), flush=True)
    elif synced == 'ok':
        print('  install.sh --sync ran.', flush=True)
    elif args.not_installed:
        print('  ' + not_installed_note(repo) + '.', flush=True)
    return dict(outcome='updated', sync=synced, head=new, line=lines[0], printed=True)


def first_time_current(repo, args, result):
    """--first-time on a checkout that is already at origin/main: the checkout may have been pulled by hand, so its
    installed hooks may be old. Install it unless it has uncommitted edits. A kit that was never installed
    (args.no_sync is set for it) keeps the plain up-to-date line: a catch-up never performs the first install."""
    if not (args.first_time and not args.no_sync and result['outcome'] == 'current'):
        return result
    shown = tilde(repo.core)
    edits = edited_files(repo)
    if edits:
        result.update(sync='held', attention=True,
                      line='agent-core-kit: %s is up to date with GitHub but %s; nothing was installed. Commit or '
                      'discard them, then run: bash %s' % (shown, held_text(repo, edits), sync_command(repo)))
    elif sync_install(repo):
        result.update(sync='ok', line='agent-core-kit: %s is up to date; install.sh --sync ran.' % shown)
    else:
        result.update(sync='failed', line='agent-core-kit: %s is up to date; install.sh --sync failed, run: bash %s'
                      % (shown, sync_command(repo)))
    return result


def same_line(repo, earlier, head):
    """True when head is the commit a pending sync was recorded for, or a commit made on top of it: the owner who
    commits a held edit instead of discarding it must not lose the install."""
    if not isinstance(earlier, str) or not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', earlier):
        return False
    return earlier == head or repo.git('merge-base', '--is-ancestor', earlier, head)[0] == 0


def retry_pending_sync(repo, args, result):
    """A sync that failed, or was held back for uncommitted edits, stays pending until one succeeds. The next run finds
    HEAD equal to origin/main and would call it current, so the previous status decides: same head and a pending sync
    means the installed hooks and rules are still stale and the sync runs again, unless the checkout is still dirty."""
    prev = read_status(args.status_file) if args.status_file else {}
    if prev.get('sync') not in ('failed', 'held') or 'sync' in result \
            or not same_line(repo, prev.get('head'), result.get('head')):
        return result
    if args.no_sync:
        result['sync'] = prev['sync']
        return result
    edits = [] if args.first_time else edited_files(repo)
    if edits:
        held = 'not installed: %s %s. Commit or discard them, then run: bash %s' % (tilde(repo.core),
                                                                                   held_text(repo, edits),
                                                                                   sync_command(repo))
        if result['outcome'] == 'current':
            result['line'] = 'agent-core-kit: %s is up to date; %s' % (tilde(repo.core), held)
        else:
            result['line'] += ' ' + held[0].upper() + held[1:]
        result.update(sync='held', attention=True)
    elif sync_install(repo):
        result['sync'] = 'ok'
        result['line'] += ' install.sh --sync ran.'
    else:
        result['sync'] = 'failed'
        result['line'] += ' install.sh --sync failed again, run: bash %s' % sync_command(repo)
    return result


def stop_on_signal(signum, frame):
    """A terminated run still leaves through its finally blocks, so the lock goes away."""
    raise SystemExit(128 + signum)


def execute(args, core):
    """(exit code, result dict). Everything that ends a run early is a Stop."""
    repo = Repo(core)
    check_checkout(repo)
    git_dir = repo.out('rev-parse', '--absolute-git-dir')
    lock = os.path.join(git_dir, LOCK_NAME)
    if os.path.isdir(lock):
        raise Stop(3, 'error', 'agent-core-kit: %s is a folder, not a lock file, so no catch-up can run; remove it.'
                   % tilde(lock))
    if not acquire_lock(lock):
        if args.first_time:
            raise Stop(3, 'locked', 'agent-core-kit: another catch-up is running on %s; wait a minute and run the same '
                       'command again.' % tilde(core))
        return 0, None
    saved = {}
    for name in ('SIGTERM', 'SIGHUP'):
        if hasattr(signal, name):
            saved[getattr(signal, name)] = signal.signal(getattr(signal, name), stop_on_signal)
    try:
        try:
            if args.first_time:
                refuse_unfinished(repo)
            result = classify_and_update(repo, args)
        except Stop as stop:
            if not (args.first_time and stop.outcome in ('blocked-dirty', 'blocked-diverged')):
                raise
            why = 'local commits' if stop.outcome == 'blocked-diverged' else 'uncommitted edits'
            result = first_time(repo, args, why)
        result = first_time_current(repo, args, result)
        result = retry_pending_sync(repo, args, result)
        code = 4 if result.get('sync') in ('failed', 'held') else 0
        return code, result
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def parse(argv):
    ap = argparse.ArgumentParser(prog='catch_up.py', description='Bring the agent-core-kit checkout up to GitHub and sync it.')
    ap.add_argument('--core', default=None, help='checkout to update (default $AGENT_CORE_DIR, else the checkout this script is in, else ~/agent-core-kit)')
    ap.add_argument('--quiet', action='store_true', help='print only what needs attention')
    ap.add_argument('--no-sync', action='store_true', help='do not run install.sh --sync after an update')
    ap.add_argument('--first-time', action='store_true',
                    help='archive uncommitted edits and local commits, then move to origin/%s' % BRANCH)
    ap.add_argument('--status-file', default=None, help='write the outcome here as JSON')
    return ap.parse_args(argv)


def local_dir(core):
    """The kit's personal folder, found the way install.sh finds it: $AGENT_CORE_LOCAL, else the folder install.sh
    recorded for the hooks, else <checkout>/local."""
    home = os.path.expanduser('~')
    found = os.environ.get('AGENT_CORE_LOCAL')
    if not found:
        try:
            with open(os.path.join(home, '.claude', 'hooks', 'agent-core', '.local-dir'), encoding='utf-8') as fh:
                found = fh.read().strip()
        except OSError:
            found = ''
    return os.path.abspath(os.path.expanduser(found)) if found else os.path.join(core, 'local')


def default_status_file(core):
    return os.path.join(local_dir(core), 'learning', '.catch_up.json')


def default_core():
    """$AGENT_CORE_DIR, else the checkout this script sits in, else ~/agent-core-kit."""
    env = os.environ.get('AGENT_CORE_DIR')
    if env:
        return env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(here, '.git')) and os.path.isfile(os.path.join(here, 'install.sh')):
        return here
    return os.path.join(os.path.expanduser('~'), 'agent-core-kit')


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    core = os.path.realpath(os.path.expanduser(args.core or default_core()))
    # the default status file is written only where the installer already made the personal folder
    explicit_status = bool(args.status_file)
    own_folder = not explicit_status and os.path.isdir(local_dir(core))
    args.status_file = args.status_file or default_status_file(core)
    args.not_installed = not args.no_sync and not installed()
    args.no_sync = args.no_sync or args.not_installed
    try:
        code, result = execute(args, core)
    except Stop as stop:
        code, result = stop.code, dict(outcome=stop.outcome, line=stop.line, **stop.extra)
        prev = read_status(args.status_file)
        if prev.get('sync') in ('failed', 'held') and prev.get('head') and prev.get('head') == result.get('head'):
            result['sync'] = prev['sync']  # a blocked run must not forget it
    except KeyboardInterrupt:
        return 130
    if result is None:
        return 0  # another catch-up holds the lock
    quiet_ok = result['outcome'] in ('current', 'offline', 'updated') and not args.first_time \
        and not result.get('attention')
    # a --first-time report is several lines and is the point of a run made by hand, so --quiet does not hide it
    if result['line'] and not result.get('printed') and (not args.quiet or not quiet_ok or '\n' in result['line']):
        print(result['line'])
    if result['outcome'] not in ('not-a-checkout', 'locked') and (explicit_status or own_folder):
        record = {'at': int(time.time()), 'outcome': result['outcome'], 'line': result['line'].splitlines()[0],
                  'head': result.get('head', '')}
        for key in ('sync', 'attention'):
            if key in result:
                record[key] = result[key]
        if result['outcome'] == 'offline':
            before = read_status(args.status_file)
            since = before.get('offline_since')
            usable = before.get('outcome') == 'offline' and isinstance(since, (int, float)) \
                and not isinstance(since, bool) and 0 < since <= record['at']
            record['offline_since'] = int(since) if usable else record['at']
        try:
            write_json(args.status_file, record)
        except OSError:
            pass
    return code


if __name__ == '__main__':
    sys.exit(main())
