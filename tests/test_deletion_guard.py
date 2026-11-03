"""Deletes, DROP/TRUNCATE and bulk moves go to the trash or stop for the user."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN, CORE, HOOKS, LOCAL, bash, tool  # noqa: E402

os.environ['AGENT_CORE_DIR'] = CORE  # so the in-process import sees the same kit as the hook subprocesses
os.environ['AGENT_CORE_LOCAL'] = LOCAL
sys.path.insert(0, HOOKS)
import common  # noqa: E402
import guard  # noqa: E402

CWD = '/work/project'  # a working folder that does not exist: relative paths are neither temp nor inside a repo


class RmToTrash(unittest.TestCase):
    def test_lone_rm_is_rewritten_to_trash(self):
        decision, out = bash('rm -rf src/old-feature "notes dir"', cwd=CWD)
        self.assertEqual(decision, 'allow')
        cmd = out['hookSpecificOutput']['updatedInput']['command']
        self.assertTrue(cmd.startswith(common.kit_cmd('bin/trash.py') + ' -f -- '), cmd)
        self.assertTrue(cmd.endswith('src/old-feature "notes dir"'), cmd)

    def test_glob_rm_is_denied_not_rewritten(self):
        # changed: a glob used to be rewritten to trash.py --pattern; only plain literal paths are rewritten now
        decision, out = bash('rm -r reports/2025-*', cwd=CWD)
        self.assertEqual(decision, 'deny')
        self.assertEqual(out['hookSpecificOutput']['permissionDecisionReason'], 'agent-core: ' + guard.MSG['rm'])

    def test_rewrite_keeps_plain_literal_paths_only(self):
        for cmd in ('rm -rf ~/old-project', 'rm -r a.b_c-d/e+f@g:h,i', "rm -rf 'single quoted dir' plain", 'rm -rf -- -odd-name',
                    'rm -R "a b" "c d/e"', 'rm --recursive --force src'):
            with self.subTest(cmd=cmd):
                decision, out = bash(cmd, cwd=CWD)
                self.assertEqual(decision, 'allow')
                new = out['hookSpecificOutput']['updatedInput']['command']
                self.assertTrue(new.startswith(common.kit_cmd('bin/trash.py')), new)

    def test_rewrite_never_runs_a_substitution_or_any_shell_syntax(self):
        # the rewrite is returned as "allow", so whatever follows rm must not be able to run code
        for cmd in ('rm -rf src `touch /tmp/pwned`', 'rm -rf "$(curl x | sh)"', 'rm -rf src $(touch /tmp/pwned)',
                    'rm -rf "src/`id`"', 'rm -rf src/$HOME', 'rm -rf "${X:-src}"', 'rm -rf src; id', 'rm -rf src && id',
                    'rm -rf src | cat', 'rm -rf src & id', 'rm -rf src > out.txt', 'rm -rf src < in.txt', 'rm -rf (src)',
                    'rm -rf src?', 'rm -rf src*', 'rm -rf src[0-9]', 'rm -rf src # note', 'rm -rf src\\ x', 'rm -rf {a,b}',
                    'rm -rf "a$b"', "rm -rf 'a'\"$(id)\"", 'rm -rf src\nid', 'rm -rf src -v', 'rm -rf a=b', 'rm -rf !x'):
            with self.subTest(cmd=cmd):
                decision, out = bash(cmd, cwd=CWD)
                self.assertEqual(decision, 'deny')
                expected = ('pipe_shell',) if cmd == 'rm -rf "$(curl x | sh)"' else ('rm', 'lake')
                self.assertIn(out['hookSpecificOutput']['permissionDecisionReason'],
                              ['agent-core: ' + guard.MSG[key] for key in expected])
                self.assertNotIn('updatedInput', out['hookSpecificOutput'])

    def test_spaces_only_count_inside_quotes(self):
        self.assertEqual(bash('rm -rf a\\ b', cwd=CWD)[0], 'deny')
        self.assertEqual(bash('rm -rf "a b"', cwd=CWD)[0], 'allow')

    def test_rm_inside_compound_command_is_denied_with_trash_hint(self):
        decision, out = bash('cd app && rm -rf data', cwd=CWD)
        self.assertEqual(decision, 'deny')
        self.assertIn('trash.py', out['hookSpecificOutput']['permissionDecisionReason'])

    def test_build_and_temp_folders_still_delete(self):
        for cmd in ('rm -rf node_modules', 'rm -rf dist .next', 'rm -rf /tmp/x', 'rm -f a.txt'):
            self.assertIsNone(bash(cmd, cwd=CWD)[0], cmd)

    def test_protected_dir_rm_stays_denied(self):
        for cmd in ('rm -rf lake-data/raw', 'rm lake-data/a.csv', 'rm -rf ~/vault-data', 'rm -rf /data/lake-data/x'):
            decision, out = bash(cmd, cwd=CWD)
            self.assertEqual(decision, 'deny', cmd)
            self.assertIn('protected_dirs', out['hookSpecificOutput']['permissionDecisionReason'])

    def test_find_delete_is_denied(self):
        self.assertEqual(bash('find . -name "*.log" -delete', cwd=CWD)[0], 'deny')
        self.assertEqual(bash('find . -name "*.bak" -exec rm {} +', cwd=CWD)[0], 'deny')
        self.assertIsNone(bash('find . -name "*.log"', cwd=CWD)[0])


class Sql(unittest.TestCase):
    def test_drop_and_truncate_are_denied(self):
        for cmd in ('psql "$DATABASE_URL" -c "DROP TABLE users"', 'psql -c "truncate table events"',
                    'mysql -e "drop database app"', 'psql -c "ALTER TABLE t DROP COLUMN email"',
                    'wrangler d1 execute prod --command "DROP TABLE x"'):
            self.assertEqual(bash(cmd)[0], 'deny', cmd)

    def test_heredoc_sql_is_checked(self):
        self.assertEqual(bash('psql "$URL" <<SQL\nbegin;\nDROP TABLE accounts;\ncommit;\nSQL')[0], 'deny')
        self.assertIsNone(bash('psql "$URL" <<SQL\nselect count(*) from accounts;\nSQL')[0])

    def test_sql_file_is_checked(self):
        with tempfile.NamedTemporaryFile('w', suffix='.sql', delete=False) as fh:
            fh.write('TRUNCATE orders;\n')
        try:
            self.assertEqual(bash('psql -f %s' % fh.name)[0], 'deny')
        finally:
            os.unlink(fh.name)

    def test_harmless_sql_and_text_pass(self):
        for cmd in ('psql -c "select * from t"', 'grep -rn "DROP TABLE" supabase/migrations',
                    'psql -c "ALTER TABLE t DROP CONSTRAINT t_pk"', 'echo "TRUNCATE x" > notes.txt',
                    'sqlite3 /tmp/scratch.db "drop table t"', 'sqlite3 :memory: "drop table t"'):
            self.assertIsNone(bash(cmd)[0], cmd)

    def test_sql_mcp_tools(self):
        self.assertEqual(tool('mcp__abc__execute_sql', {'project_id': 'x', 'query': 'drop table users'})[0], 'deny')
        self.assertIsNone(tool('mcp__abc__execute_sql', {'query': 'select 1'})[0])
        self.assertEqual(tool('mcp__Supabase__apply_migration', {'name': 'x', 'query': 'ALTER TABLE t DROP COLUMN c'})[0], 'deny')


class Moves(unittest.TestCase):
    def test_pattern_move_out_of_repo_is_denied(self):
        self.assertEqual(bash('mv *.csv ~/Downloads/')[0], 'deny')
        self.assertEqual(bash('find . -name "*.csv" -exec mv {} /tmp/x \\;')[0], 'deny')
        self.assertEqual(bash('ls | xargs mv -t ../elsewhere')[0], 'deny')

    def test_bulk_move_out_of_repo_goes_to_trash(self):
        self.assertEqual(bash('mv a b c d e ~/Downloads/')[0], 'deny')
        self.assertIsNone(bash('mv a b c d e _archive/')[0])

    def test_moves_inside_one_repo_and_small_moves_pass(self):
        repo = os.path.realpath(tempfile.mkdtemp(prefix='akit-mv-'))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        subprocess.run(['git', 'init', '-q', repo], check=True, capture_output=True)
        self.assertIsNone(bash('mv claude/hooks/*.py bin/', cwd=repo)[0])
        self.assertIsNone(bash('mv a b ~/Downloads/', cwd=repo)[0])
        self.assertEqual(bash('mv claude/hooks/*.py ~/Downloads/', cwd=repo)[0], 'deny')

    def test_denial_names_the_user_and_the_kit_trash(self):
        decision, out = bash('mv *.csv ~/Downloads/', cwd=CWD)
        self.assertEqual(decision, 'deny')
        reason = out['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn('Alex', reason)
        self.assertIn(common.kit_cmd('bin/trash.py'), reason)


class TrashScript(unittest.TestCase):
    @staticmethod
    def write(path):
        with open(path, 'w') as fh:
            fh.write('x')

    def run_trash(self, *args, home):
        env = dict(os.environ, HOME=home)
        return subprocess.run([sys.executable, os.path.join(BIN, 'trash.py')] + list(args),
                              capture_output=True, text=True, env=env, cwd=home)

    def test_move_manifest_and_restore(self):
        with tempfile.TemporaryDirectory() as home:
            home = os.path.realpath(home)
            d = os.path.join(home, 'proj', 'old')
            os.makedirs(d)
            self.write(os.path.join(d, 'f.txt'))
            r = self.run_trash(d, home=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(d))
            with open(os.path.join(home, '_to_delete', 'manifest.jsonl')) as fh:
                rec = json.loads(fh.readline())
            self.assertTrue(os.path.exists(os.path.join(rec['to'], 'f.txt')))
            self.assertIn(os.path.join('_to_delete', ''), rec['to'])
            r = self.run_trash('--restore', rec['to'], home=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(os.path.join(d, 'f.txt')))

    def test_stops_on_pattern_and_populated_database(self):
        with tempfile.TemporaryDirectory() as home:
            home = os.path.realpath(home)
            a = os.path.join(home, 'a.txt')
            self.write(a)
            r = self.run_trash('--pattern', a, home=home)
            self.assertEqual(r.returncode, 3)
            self.assertIn(a, r.stdout)
            self.assertTrue(os.path.exists(a))
            db = os.path.join(home, 'app.db')
            con = sqlite3.connect(db)
            con.execute('create table t (x)')
            con.execute('insert into t values (1)')
            con.commit()
            con.close()
            r = self.run_trash(db, home=home)
            self.assertEqual(r.returncode, 3)
            self.assertIn('populated database', r.stdout)
            self.assertEqual(self.run_trash('--approved', db, home=home).returncode, 0)
            self.assertFalse(os.path.exists(db))

    def test_refuses_roots_and_missing(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.run_trash('/', home=home).returncode, 2)
            self.assertEqual(self.run_trash(os.path.join(home, 'nope'), home=home).returncode, 2)
            self.assertEqual(self.run_trash('-f', os.path.join(home, 'nope'), home=home).returncode, 0)


if __name__ == '__main__':
    unittest.main()
