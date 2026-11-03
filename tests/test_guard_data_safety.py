"""Data and config regressions from the live review of PR13 head086233f."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[1]
GUARD = CORE / 'claude' / 'hooks' / 'guard.py'


def hook(command, cwd, config=None):
    with tempfile.TemporaryDirectory() as scratch:
        config_path = Path(scratch) / 'config.json'
        config_path.write_text(json.dumps(config or {'protected_dirs': ['lake-data']}))
        out = subprocess.run([sys.executable, str(GUARD)], input=json.dumps({
            'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': str(cwd),
        }), capture_output=True, text=True, timeout=20,
            env=dict(os.environ, AGENT_CORE_DIR=str(CORE), AGENT_CORE_LOCAL=str(scratch)))
        if out.returncode:
            raise AssertionError(out.stderr)
        return json.loads(out.stdout).get('hookSpecificOutput', {}) if out.stdout.strip() else {}


class RecoverableDeletes(unittest.TestCase):
    def test_database_and_large_file_deletes_stay_recoverable(self):
        with tempfile.TemporaryDirectory(prefix='guard-files-', dir=CORE) as scratch:
            folder = Path(scratch)
            db = folder / 'client.sqlite'
            with sqlite3.connect(db) as connection:
                connection.execute('create table records (value text)')
                connection.execute("insert into records values ('keep')")
            big = folder / 'large.bin'
            with big.open('wb') as stream:
                stream.truncate((1 << 30) + 1)
            for target in (db, big):
                for flag in ('-rf', '-f'):
                    command = 'rm %s %s' % (flag, target.name)
                    with self.subTest(command=command):
                        result = hook(command, folder)
                        self.assertEqual(result.get('permissionDecision'), 'allow')
                        self.assertIn('trash.py', result.get('updatedInput', {}).get('command', ''))
                        self.assertTrue(target.exists())
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute('select value from records').fetchall(), [('keep',)])

    def test_file_batch_size_cannot_bypass_the_trash_review(self):
        with tempfile.TemporaryDirectory(prefix='guard-batch-', dir=CORE) as scratch:
            folder = Path(scratch)
            for name in ('one.bin', 'two.bin'):
                with (folder / name).open('wb') as stream:
                    stream.truncate(600 << 20)
            result = hook('rm -rf one.bin two.bin', folder)
            self.assertEqual(result.get('permissionDecision'), 'allow')
            self.assertIn('trash.py', result.get('updatedInput', {}).get('command', ''))
            (folder / 'small.txt').write_text('ordinary')
            self.assertEqual(hook('rm -rf small.txt', folder), {})


class MalformedAnsiQuotes(unittest.TestCase):
    def test_bad_escapes_cannot_disable_the_hook(self):
        for escape in (r'\x', r'\u', r'\U', r'\uD800', r'\U00110000'):
            command = ": $'%s'; git reset --hard" % escape
            with self.subTest(command=command):
                self.assertEqual(hook(command, CORE).get('permissionDecision'), 'deny')
        for command in (r"printf $'ok\x21'", r"printf $'ok\u0021'", r"printf $'ok\U00000021'"):
            with self.subTest(command=command):
                self.assertEqual(hook(command, CORE), {})


class ProtectedNameGlobs(unittest.TestCase):
    def test_wildcard_components_match_configured_directory_names(self):
        config = {'protected_dirs': ['lake-data', 'clients/acme/data']}
        with tempfile.TemporaryDirectory(prefix='guard-glob-', dir=CORE) as scratch:
            folder = Path(scratch)
            for name in ('lake-data', 'clients/acme/data'):
                (folder / name).mkdir(parents=True)
                (folder / name / 'records.db').write_text('existing data')
            for command in ('rm -f lake-dat?/records.db', 'touch lake-dat?/records.db',
                            'cp input lake-dat?/records.db', 'rm -rf clien*',
                            'touch clients/acm?/data/records.db'):
                with self.subTest(command=command):
                    self.assertEqual(hook(command, folder, config).get('permissionDecision'), 'deny')
            self.assertEqual(hook('touch ordinary-dat?/notes.txt', folder, config), {})


class SearchFlagMeanings(unittest.TestCase):
    def test_attached_search_patterns_do_not_consume_the_input_file(self):
        for program in ('grep', 'rg'):
            for flag in ('-eTOKEN', '-neTOKEN'):
                command = '%s %s .env' % (program, flag)
                with self.subTest(command=command):
                    self.assertEqual(hook(command, CORE).get('permissionDecision'), 'deny')
                self.assertEqual(hook('%s %s README.md' % (program, flag), CORE), {})
        self.assertEqual(hook("P='TOKEN .env'; grep -e $P README.md", CORE).get('permissionDecision'), 'deny')

    def test_sed_scripts_cannot_masquerade_as_in_place_flags(self):
        for command in ("sed -e's/missing/nope/' -e p .env", "sed -ne's/missing/nope/p' .env",
                        "sed -i 's/missing/nope/w /dev/stdout' .env"):
            with self.subTest(command=command):
                self.assertEqual(hook(command, CORE).get('permissionDecision'), 'deny')
        for command in ("sed -i 's/missing/nope/' .env", "sed -i.bak -e's/missing/nope/g' .env",
                        "sed -e's/=.*//' .env"):
            with self.subTest(command=command):
                self.assertEqual(hook(command, CORE), {})

    def test_ripgrep_follow_does_not_exempt_env_contents(self):
        for command in ('rg -L TOKEN .env', 'rg --follow TOKEN .env'):
            with self.subTest(command=command):
                self.assertEqual(hook(command, CORE).get('permissionDecision'), 'deny')
        for command in ('grep -L TOKEN .env', 'grep -l TOKEN .env', 'rg -l TOKEN .env',
                        'rg -c TOKEN .env', 'rg -q TOKEN .env'):
            with self.subTest(command=command):
                self.assertEqual(hook(command, CORE), {})


class ArgumentRoles(unittest.TestCase):
    def test_end_of_options_keeps_dash_named_copy_sources_positional(self):
        for command in ('cp -- -template /srv/lake-data/records',
                        'cp -- -t /srv/lake-data/records', 'mv -- -template /srv/lake-data/records'):
            with self.subTest(command=command):
                self.assertEqual(hook(command, CORE).get('permissionDecision'), 'deny')
        self.assertEqual(hook('cp -- -template ordinary.txt', CORE), {})

    def test_structured_value_options_do_not_hide_stdin_files(self):
        for command in ('jq -R --args . < .env', 'jq --jsonargs . < .env',
                        'yq --expression . < .env'):
            with self.subTest(command=command):
                self.assertEqual(hook(command, CORE).get('permissionDecision'), 'deny')
        self.assertEqual(hook('jq --args . < README.md', CORE), {})
        self.assertEqual(hook('jq --args . <<< ".env"', CORE), {})


class ConfigListMembers(unittest.TestCase):
    def config(self, value):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'config.json'
            path.write_text(json.dumps(value))
            script = ('import json,sys;sys.path.insert(0,sys.argv[1]);import common;'
                      'print(json.dumps({"config":common.config(),"defaults":common.DEFAULTS}))')
            out = subprocess.run([sys.executable, '-c', script, str(CORE / 'claude' / 'hooks')],
                                 capture_output=True, text=True, check=True, timeout=20,
                                 env=dict(os.environ, AGENT_CORE_DIR=str(CORE), AGENT_CORE_LOCAL=str(scratch)))
            return json.loads(out.stdout)

    def test_malformed_lists_keep_defaults_and_valid_protected_paths(self):
        for value in ([None], [4], [False], [{}], [['lake-data']], [''], ['lake-data', None]):
            with self.subTest(value=value):
                output = self.config({'protected_dirs': value, 'gated_models': value,
                                      'pr_gate': {'reviewers': value}})
                expected_dirs = list(output['defaults']['protected_dirs'])
                expected_dirs.extend(item for item in value if isinstance(item, str) and item.strip())
                self.assertEqual(set(output['config']['protected_dirs']), set(expected_dirs))
                self.assertEqual(output['config']['gated_models'], output['defaults']['gated_models'])
                self.assertEqual(output['config']['pr_gate']['reviewers'], output['defaults']['pr_gate']['reviewers'])
        output = self.config({'protected_dirs': [], 'gated_models': [], 'pr_gate': {'reviewers': []}})
        self.assertEqual(output['config']['protected_dirs'], [])
        self.assertEqual(output['config']['gated_models'], [])
        self.assertEqual(output['config']['pr_gate']['reviewers'], [])


if __name__ == '__main__':
    unittest.main()
