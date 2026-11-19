"""bin/catch_up.py brings a checkout up to origin/main without losing local work, and says so when it cannot."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN  # noqa: E402

SCRIPT = os.path.join(BIN, 'catch_up.py')
ML = 'l1\nl2\nl3\nl4\nl5\n'
FAKE_INSTALL = '#!/bin/bash\necho "$@" > "$HOME/sync-marker"\n'


def clean_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(HOME=home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='t',
               GIT_AUTHOR_EMAIL='t@example.com', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.com')
    for name in ('AGENT_CORE_DIR', 'AGENT_CORE_LOCAL', 'AGENT_CORE_CATCH_UP'):
        env.pop(name, None)
    return env


class Fixture(unittest.TestCase):
    """A bare origin, a checkout of it at <home>/agent-core-kit, and a second clone that plays the part of GitHub pushes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.installed_hooks = os.path.join(self.home, '.claude', 'hooks', 'agent-core')
        for name in ('guard.py', '.core-dir'):  # what install.sh leaves behind: the kit is installed for this user
            self.write(self.installed_hooks, name, '')
        self.env = clean_env(self.home)
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.core = os.path.join(self.home, 'agent-core-kit')
        self.other = os.path.join(self.tmp, 'other')
        self.status = os.path.join(self.tmp, 'status.json')
        self.git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        self.git(self.tmp, 'clone', '-q', self.origin, self.other)
        self.write(self.other, 'a.txt', 'a1\n')
        self.write(self.other, 'b.txt', 'b1\n')
        self.write(self.other, 'c.txt', 'c1\n')
        self.write(self.other, 'install.sh', FAKE_INSTALL)
        self.git(self.other, 'checkout', '-q', '-b', 'main')
        self.commit(self.other, 'first')
        self.git(self.other, 'push', '-q', 'origin', 'main')
        self.git(self.tmp, 'clone', '-q', self.origin, self.core)

    def git(self, cwd, *args, check=True):
        out = subprocess.run(['git', '-C', cwd] + list(args), capture_output=True, text=True, env=self.env)
        if check and out.returncode != 0:
            self.fail('git %s failed: %s' % (' '.join(args), out.stderr))
        return out.stdout.strip()

    def write(self, root, rel, text, mode='w'):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as fh:
            fh.write(text)

    def read(self, root, rel):
        with open(os.path.join(root, rel), 'rb') as fh:
            return fh.read()

    def commit(self, root, message):
        self.git(root, 'add', '-A')
        self.git(root, 'commit', '-q', '-m', message)
        return self.git(root, 'rev-parse', 'HEAD')

    def push_update(self, files, message='update'):
        """A commit on GitHub that the checkout has not fetched."""
        self.git(self.other, 'pull', '-q', '--ff-only', 'origin', 'main')
        for rel, text in files.items():
            self.write(self.other, rel, text)
        sha = self.commit(self.other, message)
        self.git(self.other, 'push', '-q', 'origin', 'main')
        return sha

    def head(self):
        return self.git(self.core, 'rev-parse', 'HEAD')

    def catch_up(self, *args, script=SCRIPT, core=None, env=None, keep=False, status=True):
        """(exit code, stdout, status file content). The status file is removed unless keep is set; status=False
        leaves out --status-file so the script's own default location is used."""
        cmd = [sys.executable, script, '--core', core or self.core] + (['--status-file', self.status] if status else []) \
            + list(args)
        out = subprocess.run(cmd, capture_output=True, text=True, env=env or self.env, timeout=60)
        status = None
        if os.path.exists(self.status):
            with open(self.status, encoding='utf-8') as fh:
                status = json.load(fh)
            if not keep:
                os.unlink(self.status)
        return out.returncode, out.stdout, status


class Outcomes(Fixture):
    def test_current_changes_nothing_and_quiet_prints_nothing(self):
        before = self.head()
        rc, out, status = self.catch_up('--quiet', '--no-sync')
        self.assertEqual((rc, out), (0, ''))
        self.assertEqual(self.head(), before)
        self.assertEqual(status['outcome'], 'current')
        self.assertEqual(status['head'], before)
        self.assertIn('up to date', status['line'])
        rc, out, _ = self.catch_up('--no-sync')
        self.assertEqual(rc, 0)
        self.assertIn('up to date', out)

    def test_behind_and_clean_fast_forwards_and_runs_the_sync(self):
        new = self.push_update({'a.txt': 'a2\n'})
        old = self.head()
        rc, out, status = self.catch_up('--quiet')
        self.assertEqual((rc, out), (0, ''))
        self.assertEqual(self.head(), new)
        self.assertEqual(self.read(self.core, 'a.txt'), b'a2\n')
        self.assertEqual(self.read(self.home, 'sync-marker'), b'--sync\n')
        self.assertEqual((status['outcome'], status['head']), ('updated', new))
        self.assertIn('updated %s..%s and synced' % (old[:7], new[:7]), status['line'])

    def test_the_line_after_an_update_is_printed_without_quiet(self):
        self.push_update({'a.txt': 'a2\n'})
        rc, out, _ = self.catch_up()
        self.assertEqual(rc, 0)
        self.assertRegex(out, r'^agent-core-kit: updated [0-9a-f]{7}\.\.[0-9a-f]{7} and synced\n$')

    def test_a_failed_sync_exits_4_and_says_how_to_run_it(self):
        self.push_update({'a.txt': 'a2\n', 'install.sh': '#!/bin/bash\nexit 1\n'})
        rc, out, status = self.catch_up()
        self.assertEqual(rc, 4)
        self.assertIn('sync failed, run: bash', out)
        self.assertEqual((status['outcome'], status['sync']), ('updated', 'failed'))

    def test_behind_with_an_edit_in_an_untouched_file_updates_and_keeps_the_edit(self):
        new = self.push_update({'a.txt': 'a2\n'})
        self.write(self.core, 'b.txt', 'b-edited\n')
        rc, out, status = self.catch_up('--quiet', '--no-sync')
        self.assertEqual((rc, out), (0, ''))
        self.assertEqual(self.head(), new)
        self.assertEqual(self.read(self.core, 'b.txt'), b'b-edited\n')
        self.assertEqual(status['outcome'], 'updated')

    def test_behind_with_an_edit_in_a_file_the_update_changes_is_blocked(self):
        self.push_update({'a.txt': 'a2\n'})
        old = self.head()
        self.write(self.core, 'a.txt', 'a-mine\n')
        rc, out, status = self.catch_up('--quiet')
        self.assertEqual(rc, 3)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn('1 commit(s) behind GitHub and has uncommitted edits in a.txt; not updated.', out)
        self.assertEqual(self.head(), old)
        self.assertEqual(self.read(self.core, 'a.txt'), b'a-mine\n')
        self.assertFalse(os.path.exists(os.path.join(self.home, 'sync-marker')))
        self.assertEqual(status['outcome'], 'blocked-dirty')
        self.assertEqual(status['line'], out.strip())

    def test_diverged_is_reported_with_both_counts_and_nothing_changes(self):
        self.write(self.core, 'local.txt', 'mine\n')
        self.commit(self.core, 'local work')
        self.push_update({'a.txt': 'a2\n'})
        self.push_update({'b.txt': 'b2\n'}, 'update 2')
        old = self.head()
        rc, out, status = self.catch_up('--quiet')
        self.assertEqual(rc, 3)
        self.assertIn('has 1 local commit(s) GitHub lacks and is 2 behind; not updated.', out)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(self.head(), old)
        self.assertEqual(status['outcome'], 'blocked-diverged')
        self.assertEqual(status['line'], out.strip())

    def test_local_commits_alone_are_not_a_problem_while_github_has_nothing_new(self):
        self.write(self.core, 'local.txt', 'mine\n')
        self.commit(self.core, 'local work')
        rc, out, status = self.catch_up('--quiet')
        self.assertEqual((rc, out), (0, ''))
        self.assertEqual(status['outcome'], 'current')

    def test_another_branch_is_named_and_left_alone(self):
        self.git(self.core, 'checkout', '-q', '-b', 'feature')
        self.push_update({'a.txt': 'a2\n'})
        old = self.head()
        rc, out, status = self.catch_up('--quiet')
        self.assertEqual(rc, 3)
        self.assertIn('feature', out)
        self.assertEqual(self.head(), old)
        self.assertEqual(status['outcome'], 'other-branch')

    def test_a_refused_fast_forward_names_the_files_git_names(self):
        self.push_update({'new.txt': 'theirs\n', 'new2.txt': 'theirs\n'})
        old = self.head()
        self.write(self.core, 'new.txt', 'mine\n')
        self.write(self.core, 'new2.txt', 'mine\n')
        rc, out, status = self.catch_up('--quiet')
        self.assertEqual(rc, 3)
        self.assertEqual(status['outcome'], 'error')
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn('new.txt', out)
        self.assertIn('new2.txt', out)
        self.assertEqual(self.head(), old)
        self.assertEqual(self.read(self.core, 'new.txt'), b'mine\n')

    def test_a_checkout_without_origin_exits_2(self):
        self.git(self.core, 'remote', 'remove', 'origin')
        rc, out, _ = self.catch_up('--quiet')
        self.assertEqual(rc, 2)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_a_folder_that_is_not_a_checkout_exits_2(self):
        rc, out, _ = self.catch_up('--quiet', core=self.home)
        self.assertEqual(rc, 2)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_an_unreachable_origin_is_offline_and_quiet(self):
        self.push_update({'a.txt': 'a2\n'})
        old = self.head()
        self.git(self.core, 'remote', 'set-url', 'origin', os.path.join(self.tmp, 'does-not-exist.git'))
        rc, out, status = self.catch_up('--quiet')
        self.assertEqual((rc, out), (0, ''))
        self.assertEqual(self.head(), old)
        self.assertEqual(status['outcome'], 'offline')
        rc, out, _ = self.catch_up()
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)


class Lock(Fixture):
    def lock(self):
        return os.path.join(self.core, '.git', 'agent-core-kit-catch-up.lock')

    def test_a_fresh_lock_makes_a_second_run_exit_quietly_without_touching_anything(self):
        self.push_update({'a.txt': 'a2\n'})
        old = self.head()
        self.write(self.core, '.git/agent-core-kit-catch-up.lock', '1\n')
        rc, out, status = self.catch_up('--no-sync')
        self.assertEqual((rc, out, status), (0, '', None))
        self.assertEqual(self.head(), old)
        self.assertTrue(os.path.exists(self.lock()), 'the other run still owns its lock')

    def test_a_lock_older_than_ten_minutes_is_replaced_and_removed_afterwards(self):
        new = self.push_update({'a.txt': 'a2\n'})
        self.write(self.core, '.git/agent-core-kit-catch-up.lock', '1\n')
        old = time.time() - 11 * 60
        os.utime(self.lock(), (old, old))
        rc, _, status = self.catch_up('--quiet', '--no-sync')
        self.assertEqual(rc, 0)
        self.assertEqual(self.head(), new)
        self.assertEqual(status['outcome'], 'updated')
        self.assertFalse(os.path.exists(self.lock()))


    def test_a_lock_path_that_is_a_folder_is_reported_once_and_not_silent(self):
        os.makedirs(self.lock())
        rc, out, status = self.catch_up('--quiet', '--no-sync')
        self.assertEqual(rc, 3)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn('folder', out)
        self.assertEqual(status['outcome'], 'error')

    def test_first_time_says_so_when_another_catch_up_holds_the_lock(self):
        self.write(self.core, '.git/agent-core-kit-catch-up.lock', '1\n')
        rc, out, status = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 3)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn('another catch-up is running', out)
        self.assertIsNone(status, 'the other run owns the status')

    def test_a_terminated_run_removes_its_lock(self):
        self.push_update({'a.txt': 'a2\n', 'install.sh': '#!/bin/bash\ntouch "$HOME/started"\nsleep 5\n'})
        proc = subprocess.Popen([sys.executable, SCRIPT, '--core', self.core, '--status-file', self.status],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        end = time.time() + 20
        while not os.path.exists(os.path.join(self.home, 'started')) and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(self.lock()))
        proc.terminate()
        proc.wait(timeout=20)
        self.assertFalse(os.path.exists(self.lock()))

    def test_the_lock_is_gone_after_a_normal_run(self):
        self.catch_up('--quiet', '--no-sync')
        self.assertFalse(os.path.exists(self.lock()))


class FirstTime(Fixture):
    def test_reconciles_a_diverged_dirty_checkout_without_losing_anything(self):
        self.write(self.core, 'local1.txt', 'one\n')
        c1 = self.commit(self.core, 'local one')
        self.write(self.core, 'local2.txt', 'two\n')
        c2 = self.commit(self.core, 'local two')
        old = self.head()
        upstream = self.push_update({'a.txt': 'a-up\n', 'b.txt': 'b-up\n'})
        edited_a, edited_b = b'a-mine \xe9\n', b'b-mine\n'
        self.write(self.core, 'a.txt', edited_a, 'wb')
        self.write(self.core, 'b.txt', edited_b, 'wb')
        self.write(self.core, 'notes.txt', 'untracked\n')

        rc, out, status = self.catch_up('--first-time', '--no-sync')

        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), upstream)
        self.assertEqual(self.head(), self.git(self.core, 'rev-parse', 'origin/main'))
        self.assertEqual(self.read(self.core, 'a.txt'), b'a-up\n')
        self.assertEqual(self.read(self.core, 'b.txt'), b'b-up\n')
        self.assertEqual(self.read(self.core, 'notes.txt'), b'untracked\n')
        self.assertEqual(self.git(self.core, 'status', '--porcelain', '--untracked-files=no'), '')
        folders = [d for d in os.listdir(os.path.join(self.core, 'backups')) if d.startswith('catch-up-')]
        self.assertEqual(len(folders), 1)
        folder = os.path.join(self.core, 'backups', folders[0])
        self.assertEqual(self.read(folder, 'files/a.txt'), edited_a)
        self.assertEqual(self.read(folder, 'files/b.txt'), edited_b)
        listed = self.read(folder, 'local-commits.txt').decode()
        self.assertIn(c1 + ' local one', listed)
        self.assertIn(c2 + ' local two', listed)
        self.assertEqual(len(os.listdir(os.path.join(folder, 'patches'))), 2)
        branches = self.git(self.core, 'branch', '--list', 'backup/local-*').split()
        self.assertEqual(len(branches), 1)
        self.assertEqual(self.git(self.core, 'rev-parse', branches[0]), old)
        # the patch rebuilds the edits byte for byte on the old HEAD
        replay = os.path.join(self.tmp, 'replay')
        self.git(self.tmp, 'clone', '-q', self.core, replay)
        self.git(replay, 'checkout', '-q', old)
        self.git(replay, 'apply', os.path.join(folder, 'uncommitted.patch'))
        self.assertEqual(self.read(replay, 'a.txt'), edited_a)
        self.assertEqual(self.read(replay, 'b.txt'), edited_b)
        # the report names all of it
        for text in (old[:7], upstream[:7], folder.replace(self.home, '~'), branches[0], 'a.txt', 'b.txt', 'local one',
                     'local two', 'git stash apply --index', 'git cherry-pick', 'git checkout ' + branches[0]):
            self.assertIn(text, out)
        self.assertEqual(status['outcome'], 'updated')
        self.assertEqual(status['head'], upstream)

    def prepare_diverged_dirty(self, merge='conflict'):
        """Two local commits, GitHub ahead, edits in a.txt and b.txt, a staged new file and an untracked file.
        merge says how GitHub's change meets the edits: conflict (same line), apart (other lines), delete, rename."""
        if merge != 'conflict':
            self.push_update({'a.txt': ML, 'b.txt': ML})
            self.git(self.core, 'pull', '-q', '--ff-only', 'origin', 'main')
        self.write(self.core, 'local1.txt', 'one\n')
        self.commit(self.core, 'local one')
        self.write(self.core, 'local2.txt', 'two\n')
        self.commit(self.core, 'local two')
        old = self.head()
        if merge == 'conflict':
            self.push_update({'a.txt': 'a-up\n', 'b.txt': 'b-up\n'})
            self.edited = {'a.txt': b'a-mine \xe9\n', 'b.txt': b'b-mine\n'}
        elif merge == 'apart':
            self.push_update({'a.txt': ML.replace('l5', 'l5-up'), 'b.txt': ML.replace('l5', 'l5-up')})
            self.edited = {'a.txt': ML.replace('l1', 'l1-mine').encode(), 'b.txt': ML.replace('l1', 'l1-mine').encode()}
        else:
            self.git(self.other, 'pull', '-q', '--ff-only', 'origin', 'main')
            if merge == 'delete':
                self.git(self.other, 'rm', '-q', 'a.txt')
            else:
                self.git(self.other, 'mv', 'a.txt', 'a-renamed.txt')
            self.commit(self.other, 'gone')
            self.git(self.other, 'push', '-q', 'origin', 'main')
            self.edited = {'a.txt': ML.replace('l1', 'l1-mine').encode(), 'b.txt': ML.replace('l1', 'l1-mine').encode()}
        self.edited['staged.txt'] = b'staged\n'
        for rel, data in self.edited.items():
            self.write(self.core, rel, data, 'wb')
        self.git(self.core, 'add', 'staged.txt')
        self.write(self.core, 'notes.txt', 'untracked\n')
        return old

    def printed_command(self, out, which='to undo (only if you want the old edits back): ', needle='git stash apply'):
        for line in out.splitlines():
            if which in line and needle in line:
                return line.split(which, 1)[1].split('   or: ')[0]
        self.fail('no printed command with %r in:\n%s' % (needle, out))

    def paste(self, command):
        return subprocess.run(['bash', '-c', command], capture_output=True, text=True, env=self.env)

    def backup_folder(self):
        return os.path.join(self.core, 'backups', sorted(os.listdir(os.path.join(self.core, 'backups')))[0])

    def test_the_printed_command_restores_the_edits_in_the_state_the_run_leaves(self):
        self.prepare_diverged_dirty('apart')
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.core, 'staged.txt')), 'removed as git stash does')
        self.assertEqual(self.read(self.backup_folder(), 'files/staged.txt'), b'staged\n')
        done = self.paste(self.printed_command(out))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.read(self.core, 'a.txt'), ML.replace('l1', 'l1-mine').replace('l5', 'l5-up').encode())
        self.assertEqual(self.read(self.core, 'staged.txt'), b'staged\n')
        self.assertEqual(self.read(self.core, 'notes.txt'), b'untracked\n')

    def test_edits_that_meet_the_same_lines_come_back_as_ordinary_conflicts(self):
        self.prepare_diverged_dirty('conflict')
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        done = self.paste(self.printed_command(out))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(b'<<<<<<<', self.read(self.core, 'a.txt'))
        self.assertIn(b'a-mine', self.read(self.core, 'a.txt'))
        self.assertEqual(self.read(self.backup_folder(), 'files/a.txt'), self.edited['a.txt'])

    def fallback_restores(self, merge):
        self.prepare_diverged_dirty(merge)
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        done = self.paste(self.printed_command(out, 'if that conflicts: ', 'git switch'))
        self.assertEqual(done.returncode, 0, done.stderr)
        for rel, data in self.edited.items():
            self.assertEqual(self.read(self.core, rel), data)
        self.assertTrue(self.git(self.core, 'rev-parse', '--abbrev-ref', 'HEAD').startswith('backup/local-'))

    def test_the_fallback_command_restores_edits_to_a_file_github_deleted(self):
        self.fallback_restores('delete')

    def test_the_fallback_command_restores_edits_to_a_file_github_renamed(self):
        self.fallback_restores('rename')

    def test_staged_content_whose_working_copy_equals_head_is_archived_too(self):
        self.write(self.core, 'local1.txt', 'one\n')
        self.commit(self.core, 'local one')
        self.push_update({'b.txt': 'b-up\n'})
        self.write(self.core, 'c.txt', 'c-staged\n')
        self.git(self.core, 'add', 'c.txt')
        self.write(self.core, 'c.txt', 'c1\n')  # the working copy is back at HEAD; only the index differs
        self.assertEqual(self.git(self.core, 'diff', '--name-only', 'HEAD'), '')
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        self.assertIn('c.txt', out)
        self.assertIn(b'c-staged', self.read(self.backup_folder(), 'staged.patch'))
        done = self.paste(self.printed_command(out))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.git(self.core, 'diff', '--cached', '--name-only'), 'c.txt')
        self.assertIn('c-staged', self.git(self.core, 'diff', '--cached'))

    def test_printed_commands_work_when_the_checkout_path_has_a_space(self):
        spaced = os.path.join(self.home, 'my core')
        shutil.move(self.core, spaced)
        self.core = spaced
        old = self.prepare_diverged_dirty('apart')
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        done = self.paste(self.printed_command(out))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.read(self.core, 'staged.txt'), b'staged\n')
        self.git(self.core, 'reset', '-q', '--hard', 'origin/main')
        done = self.paste(self.printed_command(out, 'to undo (only if you want the old commits back): ', 'git cherry-pick'))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.read(self.core, 'local2.txt'), b'two\n')
        self.assertTrue(old)

    def snapshot(self):
        names = ['a.txt', 'b.txt', 'staged.txt', 'notes.txt']
        return (self.head(), self.git(self.core, 'diff', '--cached', '--binary'),
                self.git(self.core, 'status', '--porcelain', '--', ':!backups'),
                {n: self.read(self.core, n) for n in names if os.path.exists(os.path.join(self.core, n))})

    def test_a_local_untracked_file_that_github_now_tracks_stops_the_run_before_anything_changes(self):
        self.prepare_diverged_dirty()
        self.push_update({'notes.txt': 'theirs\n'})
        self.git(self.core, 'fetch', '-q', 'origin')
        before = self.snapshot()
        rc, out, status = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 5, out)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn('notes.txt', out)
        self.assertIn('run the same command again', out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(status['outcome'], 'error')
        self.assertTrue(os.path.isdir(os.path.join(self.core, 'backups')))

    def test_a_reset_that_fails_leaves_the_edits_as_they_were(self):
        self.prepare_diverged_dirty()
        before = self.snapshot()
        fake = os.path.join(self.tmp, 'fakebin')
        os.makedirs(fake)
        real = shutil.which('git')
        self.write(fake, 'git', '#!/bin/bash\nfor a in "$@"; do [ "$a" = "--keep" ] && { echo "fatal: injected" >&2; '
                   'exit 1; }; done\nexec %s "$@"\n' % shlex.quote(real))
        os.chmod(os.path.join(fake, 'git'), 0o755)
        env = dict(self.env, PATH=fake + os.pathsep + self.env['PATH'])
        rc, out, _ = self.catch_up('--first-time', '--no-sync', env=env)
        self.assertEqual(rc, 5, out)
        self.assertIn('as it was', out)
        self.assertEqual(self.head(), before[0])
        for rel, data in self.edited.items():
            self.assertEqual(self.read(self.core, rel), data)
        self.assertEqual(self.read(self.core, 'notes.txt'), b'untracked\n')
        self.assertEqual(self.git(self.core, 'diff', '--name-only', 'HEAD').split(), ['a.txt', 'b.txt', 'staged.txt'])

    def test_an_ignored_file_that_github_now_tracks_is_copied_into_the_backup(self):
        self.write(self.core, '.git/info/exclude', 'ign.txt\n')
        self.write(self.core, 'ign.txt', 'mine\n')
        self.push_update({'a.txt': 'a-up\n', 'ign.txt': 'theirs\n'})
        self.write(self.core, 'a.txt', 'a-mine\n')
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        folder = os.path.join(self.core, 'backups', os.listdir(os.path.join(self.core, 'backups'))[0])
        self.assertEqual(self.read(folder, 'ignored-overwritten/ign.txt'), b'mine\n')
        self.assertIn('ignored-overwritten', out)

    def fake_git(self, body):
        """A git first on PATH: body is bash that may call "$REAL" and see "$@"."""
        fake = os.path.join(self.tmp, 'fakebin')
        os.makedirs(fake, exist_ok=True)
        self.write(fake, 'git', '#!/bin/bash\nREAL=%s\n%s\nexec "$REAL" "$@"\n' % (shlex.quote(shutil.which('git')), body))
        os.chmod(os.path.join(fake, 'git'), 0o755)
        return dict(self.env, PATH=fake + os.pathsep + self.env['PATH'])

    PARTIAL_RESET = ('case " $* " in *" reset --keep "*) "$REAL" "$@"; echo "fatal: injected after a partial reset" >&2; '
                     'exit 1;; esac')

    def test_a_move_that_fails_after_changing_files_is_undone_and_checked(self):
        self.prepare_diverged_dirty()
        self.write(self.core, 'c.txt', 'c1\n')
        os.chmod(os.path.join(self.core, 'c.txt'), 0o755)  # a mode-only edit
        before = self.snapshot()
        before_mode = os.stat(os.path.join(self.core, 'c.txt')).st_mode
        rc, out, _ = self.catch_up('--first-time', '--no-sync', env=self.fake_git(self.PARTIAL_RESET))
        self.assertEqual(rc, 5, out)
        lines = out.strip().splitlines()
        self.assertIn('backup folder', lines[0])
        self.assertIn('backup branches', lines[1])
        self.assertIn('the checkout is as it was', lines[-1])
        self.assertEqual(self.snapshot()[:3], before[:3])
        self.assertEqual(os.stat(os.path.join(self.core, 'c.txt')).st_mode, before_mode)
        self.assertEqual(self.read(self.core, 'staged.txt'), b'staged\n')
        self.assertIn('reconciled', self.read(self.backup_folder(), 'REPORT.txt').decode())

    @unittest.skipIf(os.geteuid() == 0, 'root can write into a read-only folder')
    def test_a_read_only_folder_makes_a_real_partial_failure_that_is_undone(self):
        self.push_update({'sub/x.txt': 'x1\n'})
        self.git(self.core, 'pull', '-q', '--ff-only', 'origin', 'main')
        self.push_update({'sub/x.txt': 'x2\n', 'a.txt': 'a-up\n'})
        self.write(self.core, 'local1.txt', 'one\n')
        self.commit(self.core, 'local one')
        self.write(self.core, 'a.txt', 'a-mine\n')
        before = self.head()
        os.chmod(os.path.join(self.core, 'sub'), 0o555)
        self.addCleanup(os.chmod, os.path.join(self.core, 'sub'), 0o755)
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 5, out)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.read(self.core, 'a.txt'), b'a-mine\n')

    def test_the_report_is_printed_before_the_sync_starts_and_the_sync_result_comes_last(self):
        blocking = ('#!/bin/bash\ntouch "$HOME/sync-started"\nfor i in $(seq 200); do [ -f "$HOME/go" ] && exit 0; '
                    'sleep 0.05; done\nexit 1\n')
        self.push_update({'install.sh': blocking})
        self.prepare_diverged_dirty()
        proc = subprocess.Popen([sys.executable, SCRIPT, '--core', self.core, '--first-time'], env=self.env,
                                stdout=subprocess.PIPE, text=True)
        seen = []
        for line in proc.stdout:
            seen.append(line.rstrip('\n'))
            if 'Nothing above needs doing' in line:
                break
        self.assertFalse(os.path.exists(os.path.join(self.home, 'go')))
        self.assertTrue(any('backup folder' in x for x in seen[:2]))
        self.assertTrue(any('git stash apply --index' in x for x in seen))
        self.write(self.home, 'go', '')
        rest = proc.stdout.read().strip().splitlines()
        proc.wait(timeout=30)
        self.assertEqual(rest, ['install.sh --sync ran.'])
        self.assertEqual(proc.returncode, 0)

    def test_a_failed_sync_after_first_time_is_remembered_and_retried_by_the_next_run(self):
        self.push_update({'install.sh': CONDITIONAL_INSTALL})
        self.prepare_diverged_dirty()
        os.makedirs(os.path.join(self.core, 'local'))  # the installer made the personal folder
        rc, out, _ = self.catch_up('--first-time', status=False)
        self.assertEqual(rc, 4, out)
        default = os.path.join(self.core, 'local', 'learning', '.catch_up.json')
        with open(default) as fh:
            self.assertEqual(json.load(fh)['sync'], 'failed')
        self.write(self.home, 'sync-ok', '')
        rc, _, _ = self.catch_up('--quiet', status=False)
        self.assertEqual(rc, 0)
        with open(default) as fh:
            self.assertEqual(json.load(fh)['sync'], 'ok')
        self.assertEqual(self.read(self.home, 'sync-marker'), b'--sync\n')

    def test_first_time_runs_the_sync_after_moving(self):
        self.write(self.core, 'local1.txt', 'one\n')
        self.commit(self.core, 'local one')
        self.push_update({'a.txt': 'a-up\n'})
        rc, out, _ = self.catch_up('--first-time')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(self.home, 'sync-marker'), b'--sync\n')

    def test_a_dirty_checkout_that_only_needs_the_files_it_has_is_archived_too(self):
        upstream = self.push_update({'a.txt': 'a-up\n'})
        self.write(self.core, 'a.txt', 'a-mine\n')
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), upstream)
        backups = os.path.join(self.core, 'backups')
        folder = os.path.join(backups, os.listdir(backups)[0])
        self.assertEqual(self.read(folder, 'files/a.txt'), b'a-mine\n')

    def test_current_and_plain_behind_make_no_backup(self):
        rc, _, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.core, 'backups')))
        new = self.push_update({'a.txt': 'a2\n'})
        rc, _, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0)
        self.assertEqual(self.head(), new)
        self.assertFalse(os.path.exists(os.path.join(self.core, 'backups')))
        self.assertEqual(self.git(self.core, 'branch', '--list', 'backup/*'), '')

    def test_the_report_labels_its_commands_as_undo_and_says_nothing_needs_doing(self):
        self.prepare_diverged_dirty('apart')
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        report = self.read(self.backup_folder(), 'REPORT.txt').decode()
        for text in (out, report):
            self.assertIn('to undo (only if you want the old edits back): cd ', text)
            self.assertIn('to undo (only if you want the old commits back): cd ', text)
            self.assertIn('if that conflicts: ', text)
            self.assertIn("  Nothing above needs doing: GitHub's version is now in place.", text)
            self.assertNotIn('bring them back:', text)
        lines = out.strip().splitlines()
        self.assertEqual(lines[-1], "  Nothing above needs doing: GitHub's version is now in place.")

    def test_the_nothing_to_do_line_comes_before_the_sync_result(self):
        self.prepare_diverged_dirty('apart')
        rc, out, _ = self.catch_up('--first-time')
        self.assertEqual(rc, 0, out)
        lines = out.strip().splitlines()
        self.assertEqual(lines[-2:], ["  Nothing above needs doing: GitHub's version is now in place.",
                                      '  install.sh --sync ran.'])


CONDITIONAL_INSTALL = '#!/bin/bash\n[ -f "$HOME/sync-ok" ] && echo "$@" > "$HOME/sync-marker" && exit 0\nexit 1\n'


class HeldInstall(Fixture):
    """A background run never installs a checkout that has uncommitted edits to tracked files."""

    def marker(self):
        return os.path.join(self.home, 'sync-marker')

    def held_cycle(self, stage):
        new = self.push_update({'a.txt': 'a2\n'})
        self.write(self.core, 'b.txt', 'b-edited\n')
        stage()
        rc, out, status = self.catch_up(keep=True)
        self.assertEqual(rc, 4, out)
        self.assertEqual(self.head(), new)
        self.assertFalse(os.path.exists(self.marker()))
        self.assertEqual((status['outcome'], status['sync'], status['attention']), ('updated', 'held', True))
        self.assertIn('not installed: ~/agent-core-kit has uncommitted edits in 1 file(s) (b.txt). Commit or discard them, '
                      'then run: bash ~/agent-core-kit/install.sh --sync', out)
        self.assertRegex(out, r'^agent-core-kit: updated [0-9a-f]{7}\.\.[0-9a-f]{7}; not installed: ')
        rc, out, status = self.catch_up(keep=True)
        self.assertEqual(rc, 4, out)
        self.assertFalse(os.path.exists(self.marker()))
        self.assertEqual((status['outcome'], status['sync'], status['attention']), ('current', 'held', True))
        self.assertTrue(out.startswith('agent-core-kit: ~/agent-core-kit is up to date; not installed: '), out)
        self.assertIn('(b.txt)', out)
        self.git(self.core, 'checkout', '-q', 'HEAD', '--', 'b.txt')
        rc, out, status = self.catch_up()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(self.home, 'sync-marker'), b'--sync\n')
        self.assertEqual(status['sync'], 'ok')
        self.assertIn('install.sh --sync ran.', out)

    def test_an_unstaged_edit_holds_the_install_until_it_is_gone(self):
        self.held_cycle(lambda: None)

    def test_a_staged_only_edit_holds_the_install_too(self):
        self.held_cycle(lambda: self.git(self.core, 'add', 'b.txt'))

    def test_an_untracked_file_alone_does_not_hold_the_install(self):
        self.push_update({'a.txt': 'a2\n'})
        self.write(self.core, 'notes.txt', 'untracked\n')
        rc, out, status = self.catch_up(keep=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(status['sync'], 'ok')
        self.assertEqual(self.read(self.home, 'sync-marker'), b'--sync\n')

    def test_a_failed_sync_is_not_retried_over_uncommitted_edits(self):
        self.push_update({'a.txt': 'a2\n', 'install.sh': CONDITIONAL_INSTALL})
        rc, _, status = self.catch_up('--quiet', keep=True)
        self.assertEqual((rc, status['sync']), (4, 'failed'))
        self.write(self.home, 'sync-ok', '')
        self.write(self.core, 'b.txt', 'b-edited\n')
        rc, out, status = self.catch_up(keep=True)
        self.assertEqual((rc, status['sync'], status['attention']), (4, 'held', True))
        self.assertFalse(os.path.exists(self.marker()))
        self.assertIn('not installed', out)

    def test_first_time_still_archives_and_installs_with_the_same_edits(self):
        new = self.push_update({'a.txt': 'a2\n'})
        self.write(self.core, 'a.txt', 'a-mine\n')
        self.write(self.core, 'b.txt', 'b-edited\n')
        rc, out, status = self.catch_up('--first-time')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), new)
        self.assertTrue(os.path.isdir(os.path.join(self.core, 'backups')))
        self.assertEqual(self.read(self.home, 'sync-marker'), b'--sync\n')
        self.assertEqual(status['sync'], 'ok')

    def test_first_time_that_only_fast_forwards_still_installs_with_an_edit_elsewhere(self):
        self.push_update({'a.txt': 'a2\n'})
        self.write(self.core, 'b.txt', 'b-edited\n')
        rc, out, status = self.catch_up('--first-time')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(self.home, 'sync-marker'), b'--sync\n')
        self.assertEqual(status['sync'], 'ok')


class FirstTimeWhenCurrent(Fixture):
    def marker(self):
        return os.path.join(self.home, 'sync-marker')

    def test_an_up_to_date_clean_checkout_is_installed(self):
        rc, out, status = self.catch_up('--first-time')
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), 'agent-core-kit: ~/agent-core-kit is up to date; install.sh --sync ran.')
        self.assertEqual(self.read(self.home, 'sync-marker'), b'--sync\n')
        self.assertEqual((status['outcome'], status['sync']), ('current', 'ok'))

    def test_a_failing_install_exits_4(self):
        self.push_update({'install.sh': '#!/bin/bash\nexit 1\n'})
        self.git(self.core, 'pull', '-q', '--ff-only', 'origin', 'main')
        rc, out, status = self.catch_up('--first-time')
        self.assertEqual(rc, 4, out)
        self.assertIn('is up to date; install.sh --sync failed, run: bash ~/agent-core-kit/install.sh --sync', out)
        self.assertEqual((status['outcome'], status['sync']), ('current', 'failed'))

    def test_an_up_to_date_checkout_with_an_edit_installs_nothing(self):
        self.write(self.core, 'a.txt', 'a-mine\n')
        rc, out, status = self.catch_up('--first-time')
        self.assertEqual(rc, 4, out)
        self.assertEqual(out.strip(), 'agent-core-kit: ~/agent-core-kit is up to date with GitHub but has uncommitted edits in '
                         '1 file(s) (a.txt); nothing was installed. Commit or discard them, then run: bash '
                         '~/agent-core-kit/install.sh --sync')
        self.assertFalse(os.path.exists(self.marker()))
        self.assertEqual((status['outcome'], status['sync'], status['attention']), ('current', 'held', True))
        self.assertEqual(self.read(self.core, 'a.txt'), b'a-mine\n')

    def test_no_sync_leaves_an_up_to_date_checkout_alone(self):
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.marker()))

    def test_a_background_run_that_is_current_and_clean_runs_nothing(self):
        rc, out, status = self.catch_up('--quiet')
        self.assertEqual((rc, out), (0, ''))
        self.assertFalse(os.path.exists(self.marker()))
        self.assertNotIn('sync', status)


class OfflineRuns(Fixture):
    def go_offline(self):
        self.git(self.core, 'remote', 'set-url', 'origin', os.path.join(self.tmp, 'does-not-exist.git'))

    def test_first_time_offline_is_a_failure_and_the_background_run_is_not(self):
        self.go_offline()
        old = self.head()
        rc, out, status = self.catch_up('--first-time')
        self.assertEqual(rc, 3, out)
        self.assertTrue(out.strip().endswith('; nothing was changed.'), out)
        self.assertIn('could not reach GitHub', out)
        self.assertEqual(status['outcome'], 'offline')
        self.assertEqual(self.head(), old)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'sync-marker')))
        rc, out, status = self.catch_up('--quiet')
        self.assertEqual((rc, out), (0, ''))
        self.assertEqual(status['outcome'], 'offline')

    def test_two_offline_runs_keep_the_first_offline_since_and_a_reached_run_drops_it(self):
        self.go_offline()
        rc, _, first = self.catch_up('--quiet', keep=True)
        self.assertEqual((rc, first['outcome']), (0, 'offline'))
        self.assertAlmostEqual(first['offline_since'], time.time(), delta=30)
        time.sleep(1.1)
        _, _, second = self.catch_up('--quiet', keep=True)
        self.assertEqual(second['offline_since'], first['offline_since'])
        self.assertGreater(second['at'], first['at'])
        self.git(self.core, 'remote', 'set-url', 'origin', self.origin)
        _, _, third = self.catch_up('--quiet', keep=True)
        self.assertEqual(third['outcome'], 'current')
        self.assertNotIn('offline_since', third)
        self.go_offline()
        _, _, fourth = self.catch_up('--quiet')
        self.assertGreaterEqual(fourth['offline_since'], third['at'])

    def test_an_offline_record_without_a_usable_since_starts_a_new_one(self):
        self.go_offline()
        with open(self.status, 'w') as fh:
            json.dump({'at': 1, 'outcome': 'offline', 'line': 'x', 'head': '', 'offline_since': 'yesterday'}, fh)
        _, _, status = self.catch_up('--quiet')
        self.assertAlmostEqual(status['offline_since'], time.time(), delta=30)


class UnfinishedOperations(Fixture):
    def test_first_time_refuses_while_a_merge_cherry_pick_or_revert_is_in_progress(self):
        self.write(self.core, 'local1.txt', 'one\n')
        self.commit(self.core, 'local one')
        self.push_update({'a.txt': 'a-up\n'})
        self.write(self.core, 'b.txt', 'b-mine\n')
        old = self.head()
        for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD'):
            with self.subTest(name):
                marker = os.path.join(self.core, '.git', name)
                self.write(self.core, '.git/' + name, old + '\n')
                self.assertEqual(self.git(self.core, 'ls-files', '-u'), '')
                rc, out, status = self.catch_up('--first-time', '--no-sync')
                self.assertEqual(rc, 5, out)
                self.assertEqual(out.strip(), 'agent-core-kit: ~/agent-core-kit has a merge, cherry-pick or revert in progress; '
                                 'finish or abort it first. Nothing was changed.')
                self.assertEqual(status['outcome'], 'error')
                self.assertEqual(self.git(self.core, 'rev-parse', '-q', '--verify', name), old)
                self.assertEqual(self.head(), old)
                self.assertEqual(self.read(self.core, 'b.txt'), b'b-mine\n')
                self.assertFalse(os.path.exists(os.path.join(self.core, 'backups')))
                self.assertEqual(self.git(self.core, 'branch', '--list', 'backup/*'), '')
                os.unlink(marker)


class Transport(Fixture):
    def test_the_owners_core_ssh_command_is_used_for_the_fetch(self):
        record = os.path.join(self.tmp, 'ssh-used')
        wrapper = os.path.join(self.tmp, 'my-ssh')
        self.write(self.tmp, 'my-ssh', '#!/bin/bash\necho "$@" > %s\nexit 1\n' % shlex.quote(record))
        os.chmod(wrapper, 0o755)
        self.git(self.core, 'remote', 'set-url', 'origin', 'ssh://example.invalid/repo.git')
        self.git(self.core, 'config', 'core.sshCommand', wrapper)
        rc, out, status = self.catch_up('--quiet', '--no-sync')
        self.assertTrue(os.path.exists(record), 'core.sshCommand was overridden')
        self.assertEqual((rc, status['outcome']), (0, 'offline'))


class FailedSyncIsRetried(Fixture):
    def test_a_failed_sync_stays_failed_until_one_succeeds(self):
        self.push_update({'a.txt': 'a2\n', 'install.sh': CONDITIONAL_INSTALL})
        rc, _, status = self.catch_up('--quiet', keep=True)
        self.assertEqual((rc, status['outcome'], status['sync']), (4, 'updated', 'failed'))
        rc, out, status = self.catch_up(keep=True)  # nothing new on GitHub: HEAD equals origin/main
        self.assertEqual((rc, status['outcome'], status['sync']), (4, 'current', 'failed'))
        self.assertIn('run: bash', out)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'sync-marker')))
        self.write(self.home, 'sync-ok', '')
        rc, _, status = self.catch_up('--quiet', keep=True)
        self.assertEqual((rc, status['outcome'], status['sync']), (0, 'current', 'ok'))
        self.assertEqual(self.read(self.home, 'sync-marker'), b'--sync\n')
        os.unlink(os.path.join(self.home, 'sync-marker'))
        rc, _, status = self.catch_up('--quiet')
        self.assertEqual((rc, status.get('sync')), (0, None))
        self.assertFalse(os.path.exists(os.path.join(self.home, 'sync-marker')), 'a good sync is not repeated')


class IgnoredFiles(Fixture):
    def check(self, how):
        new = self.push_update({'a.txt': 'a2\n', 'ign.txt': 'theirs\n'})
        if how == 'gitignore':
            self.write(self.core, '.gitignore', 'ign.txt\n')
        else:
            self.write(self.core, '.git/info/exclude', 'ign.txt\n')
        self.write(self.core, 'ign.txt', 'mine\n')
        rc, out, status = self.catch_up('--quiet', '--no-sync')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), new)
        self.assertIn('1 local ignored file(s) GitHub now tracks were copied to', out)
        self.assertEqual(status['line'], out.strip())
        self.assertTrue(status['attention'])
        folder = os.path.join(self.core, 'backups', os.listdir(os.path.join(self.core, 'backups'))[0])
        self.assertEqual(self.read(folder, 'ignored-overwritten/ign.txt'), b'mine\n')
        self.assertEqual(self.read(self.core, 'ign.txt'), b'theirs\n')

    def test_a_file_ignored_by_gitignore_is_copied_before_the_update_replaces_it(self):
        self.check('gitignore')

    def test_a_file_ignored_by_info_exclude_is_copied_before_the_update_replaces_it(self):
        self.check('exclude')

    def test_nothing_is_copied_when_no_ignored_file_is_in_the_way(self):
        self.push_update({'ign.txt': 'theirs\n'})
        rc, out, _ = self.catch_up('--quiet', '--no-sync')
        self.assertEqual((rc, out), (0, ''))
        self.assertFalse(os.path.exists(os.path.join(self.core, 'backups')))


class OutsideTheCheckout(Fixture):
    def test_the_script_runs_from_a_copy_elsewhere(self):
        copy = os.path.join(self.tmp, 'elsewhere', 'x.py')
        os.makedirs(os.path.dirname(copy))
        shutil.copyfile(SCRIPT, copy)
        new = self.push_update({'a.txt': 'a2\n'})
        rc, out, status = self.catch_up('--first-time', '--no-sync', script=copy)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), new)
        self.assertEqual(status['outcome'], 'updated')


class KitLayout(Fixture):
    def test_the_personal_folder_is_never_touched_and_never_archived(self):
        self.push_update({'.gitignore': '/local/\n/backups/\n'})
        self.git(self.core, 'pull', '-q', '--ff-only', 'origin', 'main')
        self.write(self.core, 'local/AGENTS.md', 'my rules\n')
        self.write(self.core, 'local/config.json', '{"user_name": "Alex"}\n')
        self.write(self.core, 'local1.txt', 'one\n')
        self.commit(self.core, 'local one')
        self.push_update({'a.txt': 'a-up\n'})
        self.write(self.core, 'a.txt', 'a-mine\n')
        rc, out, _ = self.catch_up('--first-time', '--no-sync')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(self.core, 'local/AGENTS.md'), b'my rules\n')
        self.assertEqual(self.read(self.core, 'local/config.json'), b'{"user_name": "Alex"}\n')
        backups = os.path.join(self.core, 'backups')
        (folder,) = os.listdir(backups)
        self.assertFalse(os.path.exists(os.path.join(backups, folder, 'files', 'local')))
        self.assertEqual(self.git(self.core, 'status', '--porcelain'), '', 'backups/ and local/ are git-ignored')

    def test_the_default_status_file_lives_in_local_learning_once_local_exists(self):
        rc, _, _ = self.catch_up('--quiet', '--no-sync', status=False)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.core, 'local')), 'local/ is the installer\'s to create')
        os.makedirs(os.path.join(self.core, 'local'))
        rc, _, _ = self.catch_up('--quiet', '--no-sync', status=False)
        with open(os.path.join(self.core, 'local', 'learning', '.catch_up.json')) as fh:
            self.assertEqual(json.load(fh)['outcome'], 'current')

    def test_the_default_status_file_follows_agent_core_local(self):
        mine = os.path.join(self.tmp, 'my-local')
        os.makedirs(mine)
        rc, _, _ = self.catch_up('--quiet', '--no-sync', status=False, env=dict(self.env, AGENT_CORE_LOCAL=mine))
        self.assertEqual(rc, 0)
        with open(os.path.join(mine, 'learning', '.catch_up.json')) as fh:
            self.assertEqual(json.load(fh)['outcome'], 'current')

    def test_the_checkout_defaults_to_agent_core_dir_then_the_folder_the_script_is_in_then_the_home_folder(self):
        new = self.push_update({'a.txt': 'a2\n'})
        elsewhere = os.path.join(self.tmp, 'elsewhere', 'x.py')
        os.makedirs(os.path.dirname(elsewhere))
        shutil.copyfile(SCRIPT, elsewhere)
        # no --core, script outside any checkout: ~/agent-core-kit
        out = subprocess.run([sys.executable, elsewhere, '--no-sync', '--status-file', self.status],
                             capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), new)
        self.assertIn('agent-core-kit: updated', out.stdout)
        # the script inside a checkout: that checkout
        inside = os.path.join(self.tmp, 'inside')
        self.git(self.tmp, 'clone', '-q', self.origin, inside)
        self.write(inside, 'install.sh', FAKE_INSTALL)
        os.makedirs(os.path.join(inside, 'bin'))
        shutil.copyfile(SCRIPT, os.path.join(inside, 'bin', 'catch_up.py'))
        self.push_update({'b.txt': 'b2\n'})
        out = subprocess.run([sys.executable, os.path.join(inside, 'bin', 'catch_up.py'), '--no-sync',
                              '--status-file', self.status], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.read(inside, 'b.txt'), b'b2\n')
        self.assertEqual(self.read(self.core, 'b.txt'), b'b1\n')
        # AGENT_CORE_DIR wins over both
        out = subprocess.run([sys.executable, os.path.join(inside, 'bin', 'catch_up.py'), '--no-sync',
                              '--status-file', self.status], capture_output=True, text=True, timeout=60,
                             env=dict(self.env, AGENT_CORE_DIR=self.core))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.read(self.core, 'b.txt'), b'b2\n')

    def test_messages_name_the_kit_and_the_branch_is_main(self):
        self.git(self.core, 'checkout', '-q', '-b', 'feature')
        rc, out, _ = self.catch_up('--quiet')
        self.assertEqual(rc, 3)
        self.assertRegex(out, r'^agent-core-kit: ~?/.*agent-core-kit is on feature, not main; not updated\.\n$')


class NeverInstalled(Fixture):
    """A clone the installer never ran for: the catch-up updates the files and leaves the first install to the user."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(os.path.join(self.home, '.claude'))

    def test_an_update_does_not_run_the_installer_and_says_how_to_install(self):
        new = self.push_update({'a.txt': 'a2\n'})
        rc, out, status = self.catch_up()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), new)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'sync-marker')))
        self.assertIn('not synced: the kit is not installed for this user', out)
        self.assertIn('install.sh', out)
        self.assertNotIn('--sync', out)
        self.assertEqual((status['outcome'], status['sync']), ('updated', 'skipped'))

    def test_first_time_does_not_run_the_installer_either(self):
        self.write(self.core, 'a.txt', 'mine\n')
        new = self.push_update({'a.txt': 'a2\n'})
        rc, out, status = self.catch_up('--first-time')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), new)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'sync-marker')))
        self.assertIn('the kit is not installed for this user', out)
        self.assertEqual(status['sync'], 'skipped')

    def test_a_background_run_over_an_edit_is_not_held_because_nothing_would_be_installed(self):
        new = self.push_update({'a.txt': 'a2\n'})
        self.write(self.core, 'b.txt', 'b-edited\n')
        rc, out, status = self.catch_up()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), new)
        self.assertEqual((status['sync'], status.get('attention')), ('skipped', None))
        self.assertNotIn('not installed: ~', out)

    def test_first_time_on_an_up_to_date_checkout_skips_the_sync_with_the_plain_line(self):
        self.write(self.core, 'a.txt', 'a-mine\n')
        rc, out, status = self.catch_up('--first-time')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'sync-marker')))
        self.assertIn('is up to date.', out)
        self.assertNotIn('uncommitted', out)
        self.assertEqual(status['outcome'], 'current')
        self.assertNotIn('sync', status)

    def test_half_an_install_does_not_count(self):
        self.write(self.installed_hooks, 'guard.py', '')  # guard.py without .core-dir, as the installer tests it
        self.push_update({'a.txt': 'a2\n'})
        rc, out, _ = self.catch_up()
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'sync-marker')))


if __name__ == '__main__':
    unittest.main()
