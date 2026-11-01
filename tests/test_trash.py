"""trash.py moves things into a dated _to_delete/ folder with a manifest, restores them, and stops for the user's
review on big batches, populated databases and name patterns. Nothing is ever deleted."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN, env_for, make_local  # noqa: E402

GB = 1 << 30


def slurp(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def spit(path, text='x' * 100):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        fh.write(text)


class Trash(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='akit-trash-')
        self.home = os.path.join(self.root, 'home')
        self.work = os.path.join(self.root, 'work')
        os.makedirs(self.home)
        os.makedirs(self.work)
        self.local = make_local()
        self.day = time.strftime('%Y-%m-%d')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.local, True)

    def trash(self, *args, local=None):
        env = env_for(local or self.local, HOME=self.home)
        return subprocess.run([sys.executable, os.path.join(BIN, 'trash.py')] + list(args), capture_output=True,
                              text=True, env=env, timeout=60)

    def trashed(self, path):
        return os.path.join(self.home, '_to_delete', self.day, path.lstrip(os.sep))

    def manifest(self):
        return [json.loads(l) for l in slurp(os.path.join(self.home, '_to_delete', 'manifest.jsonl')).splitlines() if l.strip()]

    def sparse(self, path, size):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.truncate(size)

    def test_move_goes_to_a_dated_trash_with_a_manifest(self):
        f = os.path.join(self.work, 'notes', 'a.txt')
        d = os.path.join(self.work, 'old-build')
        spit(f)
        spit(os.path.join(d, 'out', 'b.bin'), 'y' * 2000)
        out = self.trash('--why', 'cleanup after the migration', f, d)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(os.path.exists(f))
        self.assertFalse(os.path.exists(d))
        self.assertTrue(os.path.isfile(self.trashed(f)))
        self.assertTrue(os.path.isfile(os.path.join(self.trashed(d), 'out', 'b.bin')))
        rows = self.manifest()
        self.assertEqual([(r['from'], r['to'], r['bytes'], r['why'], r['approved']) for r in rows],
                         [(f, self.trashed(f), 100, 'cleanup after the migration', False),
                          (d, self.trashed(d), 2000, 'cleanup after the migration', False)])
        self.assertIn('Restore with: python3 ', out.stdout)
        self.assertIn('trash.py --restore <path>', out.stdout)

    def test_restore_puts_it_back_and_refuses_to_overwrite(self):
        f = os.path.join(self.work, 'a.txt')
        spit(f, 'precious')
        self.trash(f)
        out = self.trash('--restore', self.trashed(f))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(slurp(f), 'precious')
        self.assertFalse(os.path.exists(self.trashed(f)))
        self.assertEqual(self.manifest()[-1]['restored'], f)
        # trash it again, then recreate the original: restoring must not clobber the new file
        self.trash(f)
        spit(f, 'new file')
        out = self.trash('--restore', self.trashed(f))
        self.assertEqual(out.returncode, 2)
        self.assertIn('exists again', out.stderr)
        self.assertEqual(slurp(f), 'new file')
        self.assertTrue(os.path.exists(self.trashed(f)))

    def test_restore_of_an_unknown_path_fails(self):
        stray = os.path.join(self.home, '_to_delete', self.day, 'stray.txt')
        spit(stray)
        out = self.trash('--restore', stray)
        self.assertEqual(out.returncode, 2)
        self.assertIn('is not in any trash manifest', out.stderr)

    def test_same_name_twice_in_one_day_keeps_both(self):
        f = os.path.join(self.work, 'a.txt')
        spit(f, 'first')
        self.trash(f)
        spit(f, 'second')
        self.trash(f)
        self.assertEqual(slurp(self.trashed(f)), 'first')
        self.assertEqual(slurp(self.trashed(f) + '.1'), 'second')

    def test_list_prints_the_manifest(self):
        f = os.path.join(self.work, 'a.txt')
        spit(f)
        self.trash(f)
        rows = [json.loads(l) for l in self.trash('--list').stdout.splitlines()]
        self.assertEqual([r['from'] for r in rows], [f])

    def test_a_batch_over_1_gb_stops_until_approved(self):
        big = os.path.join(self.work, 'dump', 'huge.img')
        small = os.path.join(self.work, 'small.txt')
        self.sparse(big, GB + 1)
        spit(small)
        out = self.trash(os.path.dirname(big), small)
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn('STOPPED, nothing moved', out.stdout)
        self.assertIn('over 1 GB', out.stdout)
        self.assertIn(os.path.dirname(big), out.stdout)
        self.assertIn('rerun with --approved', out.stdout)
        self.assertTrue(os.path.exists(big) and os.path.exists(small), 'nothing may move on a stop')
        self.assertFalse(os.path.exists(os.path.join(self.home, '_to_delete')))
        out = self.trash('--approved', os.path.dirname(big), small)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse(os.path.exists(big))
        self.assertTrue(os.path.exists(self.trashed(big)))
        self.assertTrue(all(r['approved'] for r in self.manifest()))

    def test_a_populated_database_stops_but_an_empty_one_does_not(self):
        full = os.path.join(self.work, 'app', 'data.sqlite')
        empty = os.path.join(self.work, 'scratch.db')
        os.makedirs(os.path.dirname(full))
        for path, rows in ((full, 3), (empty, 0)):
            con = sqlite3.connect(path)
            con.execute('create table t (id integer)')
            con.executemany('insert into t values (?)', [(i,) for i in range(rows)])
            con.commit()
            con.close()
        out = self.trash(os.path.dirname(full))
        self.assertEqual(out.returncode, 3, out.stdout)
        self.assertIn('holds 1 populated database(s)', out.stdout)
        self.assertIn('SQLite, 3 rows', out.stdout)
        self.assertTrue(os.path.exists(full))
        self.assertEqual(self.trash(empty).returncode, 0)
        self.assertEqual(self.trash('--approved', os.path.dirname(full)).returncode, 0)
        self.assertFalse(os.path.exists(full))

    def test_a_name_pattern_stops_until_approved(self):
        a, b = os.path.join(self.work, 'a.log'), os.path.join(self.work, 'b.log')
        spit(a)
        spit(b)
        out = self.trash('--pattern', a, b)
        self.assertEqual(out.returncode, 3)
        self.assertIn('matched by a name pattern', out.stdout)
        self.assertTrue(os.path.exists(a) and os.path.exists(b))
        self.assertEqual(self.trash('--pattern', '--approved', a, b).returncode, 0)
        self.assertFalse(os.path.exists(a) or os.path.exists(b))

    def test_the_stop_addresses_the_configured_user(self):
        spit(os.path.join(self.work, 'a.log'))
        out = self.trash('--pattern', os.path.join(self.work, 'a.log'))
        self.assertIn('Show Alex this full list and wait for their yes', out.stdout)
        anonymous = make_local(config={})
        self.addCleanup(shutil.rmtree, anonymous, True)
        out = self.trash('--pattern', os.path.join(self.work, 'a.log'), local=anonymous)
        self.assertIn('Show the user this full list and wait for their yes', out.stdout)
        self.assertEqual(self.trash('--help').returncode, 0)

    def test_root_folders_and_the_trash_itself_are_refused(self):
        spit(os.path.join(self.work, 'ok.txt'))
        for target in (self.home, '/', os.path.join(self.home, '_to_delete')):
            os.makedirs(os.path.join(self.home, '_to_delete'), exist_ok=True)
            out = self.trash(os.path.join(self.work, 'ok.txt'), target)
            self.assertEqual(out.returncode, 2, target)
            self.assertIn('nothing was moved', out.stderr)
            self.assertTrue(os.path.exists(os.path.join(self.work, 'ok.txt')), 'one bad path stops the whole batch')
        stored = os.path.join(self.home, '_to_delete', self.day, 'x.txt')
        spit(stored)
        out = self.trash(stored)
        self.assertEqual(out.returncode, 2)
        self.assertIn('the user empties it themselves', out.stderr)

    def test_missing_paths_error_unless_forced(self):
        gone = os.path.join(self.work, 'gone.txt')
        out = self.trash(gone)
        self.assertEqual(out.returncode, 2)
        self.assertIn('no such file or folder', out.stderr)
        self.assertEqual(self.trash('-f', gone).returncode, 0)
        self.assertEqual(self.trash().returncode, 2)

    def test_symlinks_move_as_links_without_touching_their_target(self):
        target = os.path.join(self.work, 'real', 'data.txt')
        spit(target)
        link = os.path.join(self.work, 'link')
        os.symlink(os.path.dirname(target), link)
        self.assertEqual(self.trash(link).returncode, 0)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.islink(self.trashed(link)))
        self.assertTrue(os.path.isfile(target))


if __name__ == '__main__':
    unittest.main()
