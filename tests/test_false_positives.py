"""The guard judges real write targets only, never reads, text, heredoc bodies or a bare '/'; real damage still denies.

Two fixtures written for the kit drive most of it: tests/fixtures/guard_allow.txt (commands that must pass) and
tests/fixtures/guard_deny.txt (commands that must be denied, each labelled with the message key it must produce).
They run from a working folder that does not exist (so nothing counts as temp or as inside a repo) with the
helpers.py test config: protected_dirs lake-data and ~/vault-data, protected Supabase project
abcdefghijklmnopqrst, gated model fable. Every decision is a deny except a gated subagent, which asks.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CORE, HOOKS, LOCAL, TEST_CONFIG, bash, make_local, tool  # noqa: E402

os.environ['AGENT_CORE_DIR'] = CORE  # so the in-process import sees the same kit and local/ as the hook subprocesses
os.environ['AGENT_CORE_LOCAL'] = LOCAL
sys.path.insert(0, HOOKS)
import guard  # noqa: E402

FIXTURES = os.path.join(CORE, 'tests', 'fixtures')
REF = next(iter(TEST_CONFIG['protected_supabase']))
NOTE = TEST_CONFIG['protected_supabase'][REF]
# keys that cannot be triggered by a bare command: they have tests of their own
ELSEWHERE = {'review_wait': 'test_merge_gate', 'review_block': 'test_merge_gate', 'review_unread': 'test_merge_gate',
             'lane': 'test_lane', 'gated': 'GatedModels below'}
REPO = '/work/project'  # a working folder that does not exist: relative paths are neither temp nor inside a repo


def lines(name):
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as fh:
        return [l.rstrip('\n') for l in fh if l.strip() and not l.lstrip().startswith('#')]


def allowed():
    return lines('guard_allow.txt')


def denied():
    return [tuple(l.split('|', 1)) for l in lines('guard_deny.txt')]


def reason(out):
    return out['hookSpecificOutput']['permissionDecisionReason']


def expect_prefix(key):
    """The start of a message, before any value the guard fills in."""
    return 'agent-core: ' + guard.MSG[key].split('%s')[0][:40]


class Reads(unittest.TestCase):
    def test_readers_never_fire(self):
        for cmd in ('rg -n "git add -A" ~/notes', 'grep -rn "rm -rf" .', 'cat lake-data/manifest.json',
                    'sed -n 1,40p lake-data/notes.md', 'head -5 lake-data/x.csv | wc -l',
                    "ssh box 'cat /srv/app/.env.example; ls lake-data'",
                    'cat ~/.claude/projects/-work/memory/MEMORY.md',
                    'ls ~/.claude/plans/ | head -50; ls ~/.claude/projects/x/memory/',
                    'grep -l -i -E "tailscale|ssh" ~/.claude/projects/x/memory/*.md',
                    'sqlite3 -readonly lake-data/lake.db "select count(*) from t"',
                    'sqlite3 lake-data/h.db ".backup /tmp/x.bak"',
                    'D=lake-data/backups/h.bak; sqlite3 lake-data/h.db ".backup \'$D\'" && sqlite3 "$D" "PRAGMA journal_mode=DELETE;"',
                    'DB="file:lake-data/h.db?mode=ro"; sqlite3 -header "$DB" "select 1"',
                    'S=/private/tmp/x/probe; cd $S && rm -rf logins', 'rm -rf dist-installer-logo',
                    'mv out/gold/*__d3.run.json _archive/gold-failed-run-1/',
                    'mv repo/.claude/worktrees/feat/src/a.ts ~/Archive/x/'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(bash(cmd, cwd=REPO)[0])

    def test_text_that_mentions_danger_is_not_danger(self):
        for cmd in ('echo "never run git reset --hard"', "printf '%s\\n' 'git clean -fdx' >> notes.md",
                    'git commit -m "guard: block git add -A and rm -rf"',
                    'python3 -c "print(\'git push --force\')"', 'node -e "console.log(\'lake-data\')"',
                    "awk -F/ '{print $1}' paths.txt", "tr / _ < a > b", 'cd / && ls',
                    "sed -i 's#lake-data#$LAKE#' scripts/config.sh",
                    'ln -s lake-data/exports ~/lake-exports', 'echo "it\'s fine'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(bash(cmd, cwd=REPO)[0])

    def test_heredoc_bodies_are_ignored(self):
        cmd = ("cat >> universal.py <<'EOF'\nsubprocess.run(['git', 'reset', '--hard'])\n# rm -rf /\n"
               "open('lake-data/x','w')\nEOF")
        self.assertIsNone(bash(cmd, cwd=REPO)[0])
        cmd = "python3 - <<'PY'\nimport pathlib\npathlib.Path('~/.claude/projects/x/memory/a.md').write_text('git add -A')\nPY"
        self.assertIsNone(bash(cmd, cwd=REPO)[0])

    def test_memory_and_plan_tools_pass(self):
        for name, inp in (('Read', {'file_path': '/home/alex/.claude/projects/x/memory/MEMORY.md'}),
                          ('Read', {'file_path': '/home/alex/.claude/plans/plan.md'}),
                          ('Grep', {'pattern': 'lake', 'path': '/home/alex/.claude/projects/x/memory'})):
            self.assertIsNone(tool(name, inp)[0], inp)


class MovesInsideARepo(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp(prefix='akit-mv-'))
        subprocess.run(['git', 'init', '-q', self.repo], check=True, capture_output=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_pattern_moves_inside_the_repo_pass_and_out_of_it_deny(self):
        self.assertIsNone(bash('mv src/*.py lib/', cwd=self.repo)[0])
        self.assertIsNone(bash('mv a.txt b.txt c.txt d.txt e.txt docs/', cwd=self.repo)[0])
        self.assertEqual(bash('mv src/*.py ~/Downloads/', cwd=self.repo)[0], 'deny')
        self.assertEqual(bash('mv a.txt b.txt c.txt d.txt e.txt ~/Downloads/', cwd=self.repo)[0], 'deny')


class Fixtures(unittest.TestCase):
    def test_the_fixtures_are_big_enough(self):
        self.assertGreaterEqual(len(allowed()), 40)
        self.assertGreaterEqual(len(denied()), 40)

    def test_every_allowed_command_passes(self):
        wrong = []
        for cmd in allowed():
            decision, out = bash(cmd, cwd=REPO)
            if decision:
                wrong.append('%s -> %s: %s' % (cmd, decision, reason(out)[:100]))
        self.assertEqual(wrong, [], '\n' + '\n'.join(wrong))

    def test_every_denied_command_is_denied_with_its_alternative(self):
        wrong = []
        for key, cmd in denied():
            if key not in guard.MSG:
                wrong.append('unknown key %s: %s' % (key, cmd))
                continue
            decision, out = bash(cmd, cwd=REPO)
            if decision != 'deny':
                wrong.append('%s (%s) -> %s' % (cmd, key, decision))
            elif not reason(out).startswith(expect_prefix(key)):
                wrong.append('%s (%s) -> wrong message: %s' % (cmd, key, reason(out)[:100]))
        self.assertEqual(wrong, [], '\n' + '\n'.join(wrong))

    def test_every_message_has_a_denied_command(self):
        keys = {key for key, _ in denied()}
        self.assertEqual(keys - set(guard.MSG), set())
        self.assertEqual(set(guard.MSG) - keys, set(ELSEWHERE))


class RealWrites(unittest.TestCase):
    def test_protected_dir_writes_deny(self):
        for cmd in ('echo x > lake-data/a.txt', 'sed -i "" s/a/b/ lake-data/notes.md', 'cp a.csv lake-data/',
                    'tee -a lake-data/log.txt', 'ln -s ~/x lake-data/link', 'touch lake-data/flag',
                    'bash -c "echo x >> lake-data/a"', 'sqlite3 ~/h.db ".backup lake-data/b/h.bak"',
                    'D=lake-data/b/x.bak; sqlite3 ~/h.db ".backup \'$D\'"',
                    'sqlite3 lake-data/h.db ".restore /tmp/x.bak"',
                    'sqlite3 lake-data/h.db "PRAGMA journal_mode=DELETE;"', 'echo x > ~/vault-data/a'):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd, cwd=REPO)[0], 'deny')

    def test_message_names_the_user_and_the_config(self):
        _, out = bash('echo x > lake-data/a.txt', cwd=REPO)
        self.assertIn('Alex', reason(out))
        self.assertIn('protected_dirs', reason(out))

    def test_env_files_stay_private(self):
        self.assertEqual(bash('cat .env', cwd=REPO)[0], 'deny')
        self.assertIsNone(bash('cat .env.example', cwd=REPO)[0])


class ProtectedConfig(unittest.TestCase):
    LAKE_COMMANDS = ('echo x > lake-data/a.txt', 'cp a.csv lake-data/', 'tee -a lake-data/log.txt', 'touch lake-data/flag',
                     'sqlite3 lake-data/h.db "delete from t"', 'mv a.csv lake-data/', 'sed -i "" s/a/b/ lake-data/notes.md',
                     'echo x > ~/vault-data/a', 'chmod 777 vault-data/x')

    def test_empty_protected_dirs_denies_nothing_lake_related(self):
        local = make_local(config={'user_name': 'Alex', 'protected_dirs': []})
        for cmd in self.LAKE_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertIsNone(bash(cmd, cwd=REPO, local=local)[0])
        decision, out = bash('rm -rf lake-data/raw', cwd=REPO, local=local)  # a plain recursive delete: trash, not lake
        self.assertEqual(decision, 'allow')
        self.assertIn('trash.py', out['hookSpecificOutput']['updatedInput']['command'])

    def test_no_config_file_at_all_denies_nothing_lake_related(self):
        local = tempfile.mkdtemp(prefix='akit-bare-')
        for cmd in self.LAKE_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertIsNone(bash(cmd, cwd=REPO, local=local)[0])

    def test_only_the_configured_names_are_protected(self):
        local = make_local(config={'protected_dirs': ['vault-data/', '~/safe-data']})
        self.assertEqual(bash('echo x > vault-data/a', cwd=REPO, local=local)[0], 'deny')
        self.assertEqual(bash('echo x > /home/a/vault-data/b/c', cwd=REPO, local=local)[0], 'deny')  # a bare name: anywhere
        self.assertIsNone(bash('echo x > lake-data/a', cwd=REPO, local=local)[0])
        self.assertIsNone(bash('echo x > vault-data-old/a', cwd=REPO, local=local)[0])
        self.assertEqual(bash('echo x > ~/safe-data/a', cwd=REPO, local=local)[0], 'deny')
        # a path entry protects that path only, not every folder called safe-data (changed: it used to match the last name)
        self.assertIsNone(bash('echo x > safe-data/a', cwd=REPO, local=local)[0])
        self.assertIsNone(bash('echo x > /home/a/safe-data/b/c', cwd=REPO, local=local)[0])

    def test_backups_folder_is_exempt_only_for_a_configured_name(self):
        local = make_local(config={'protected_dirs': ['archive-data']})
        self.assertIsNone(bash('sqlite3 archive-data/backups/h.bak "insert into t values (1)"', cwd=REPO, local=local)[0])
        self.assertEqual(bash('sqlite3 archive-data/h.db "insert into t values (1)"', cwd=REPO, local=local)[0], 'deny')
        self.assertIsNone(bash('sqlite3 lake-data/backups/h.bak "insert into t values (1)"', cwd=REPO, local=local)[0])

    def test_user_name_comes_from_config(self):
        local = make_local(config={'user_name': 'Sam', 'protected_dirs': ['lake-data']})
        _, out = bash('echo x > lake-data/a', cwd=REPO, local=local)
        self.assertIn('Sam', reason(out))
        local = make_local(config={'protected_dirs': ['lake-data']})
        _, out = bash('echo x > lake-data/a', cwd=REPO, local=local)
        self.assertIn('the user', reason(out))
        self.assertNotIn('Alex', reason(out))


class Supabase(unittest.TestCase):
    def test_message_names_the_note_and_scripts_deploy(self):
        decision, out = bash('supabase db push --project-ref %s' % REF, cwd=REPO)
        self.assertEqual(decision, 'deny')
        self.assertIn(REF, reason(out))
        self.assertIn(NOTE, reason(out))
        self.assertIn('scripts/deploy', reason(out))

    def test_linked_project_is_protected_too(self):
        linked = tempfile.mkdtemp(prefix='akit-sb-')
        os.makedirs(os.path.join(linked, 'supabase', '.temp'))
        with open(os.path.join(linked, 'supabase', '.temp', 'project-ref'), 'w') as fh:
            fh.write(REF + '\n')
        self.assertEqual(bash('supabase db push', cwd=linked)[0], 'deny')
        self.assertEqual(bash('supabase migration up', cwd=linked)[0], 'deny')
        self.assertIsNone(bash('supabase functions deploy hello', cwd=linked)[0])

    def test_other_projects_pass_and_the_list_is_config(self):
        self.assertIsNone(bash('supabase db push --project-ref zzzzzzzzzzzzzzzzzzzz', cwd=REPO)[0])
        local = make_local(config={'protected_supabase': {'zzzzzzzzzzzzzzzzzzzz': 'staging copy'}})
        decision, out = bash('supabase db push --project-ref zzzzzzzzzzzzzzzzzzzz', cwd=REPO, local=local)
        self.assertEqual(decision, 'deny')
        self.assertIn('staging copy', reason(out))
        self.assertIsNone(bash('supabase db push --project-ref %s' % REF, cwd=REPO, local=local)[0])

    def test_empty_config_protects_nothing(self):
        local = make_local(config={'protected_supabase': {}})
        self.assertIsNone(bash('supabase db push --project-ref %s' % REF, cwd=REPO, local=local)[0])

    def test_a_ref_without_a_note_still_denies(self):
        local = make_local(config={'protected_supabase': {REF: ''}})
        decision, out = bash('supabase db push --project-ref %s' % REF, cwd=REPO, local=local)
        self.assertEqual(decision, 'deny')
        self.assertIn('scripts/deploy', reason(out))


class GatedModels(unittest.TestCase):
    def test_gated_model_and_advisor_ask_the_user(self):
        for name in ('Agent', 'Task'):
            for inp in ({'subagent_type': 'advisor', 'prompt': 'x'}, {'subagent_type': 'worker', 'model': 'fable'},
                        {'subagent_type': 'worker', 'model': 'claude-Fable-5-1'}, {'model': 'fable', 'prompt': 'x'}):
                with self.subTest(tool=name, input=inp):
                    decision, out = tool(name, inp)
                    self.assertEqual(decision, 'ask')  # changed from deny: Claude Code prompts the user instead
                    self.assertTrue(out['hookSpecificOutput']['permissionDecisionReason'].startswith(expect_prefix('gated')))
        reason_ = out['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn('asks you before spawning', reason_)
        self.assertIn('fable is gated in local/config.json', reason_)

    def test_ordinary_subagents_pass(self):
        for inp in ({'subagent_type': 'worker', 'model': 'sonnet'}, {'subagent_type': 'reviewer', 'model': 'opus'},
                    {'subagent_type': 'researcher'}, {'subagent_type': 'general-purpose', 'prompt': 'fable is a word'},
                    {'subagent_type': 'worker', 'model': 'haiku'}):
            with self.subTest(input=inp):
                self.assertIsNone(tool('Agent', inp)[0])

    def test_the_gated_list_is_config(self):
        local = make_local(config={'gated_models': ['fable', 'Opus']})
        decision, out = tool('Agent', {'subagent_type': 'worker', 'model': 'opus'}, local=local)
        self.assertEqual(decision, 'ask')
        self.assertIn('opus is gated', out['hookSpecificOutput']['permissionDecisionReason'])
        self.assertIsNone(tool('Agent', {'subagent_type': 'worker', 'model': 'sonnet'}, local=local)[0])
        local = make_local(config={'gated_models': []})
        self.assertIsNone(tool('Agent', {'subagent_type': 'worker', 'model': 'fable'}, local=local)[0])
        self.assertIsNone(tool('Agent', {'subagent_type': 'worker', 'model': ''}, local=local)[0])
        local = make_local(config={'gated_models': ['']})  # an empty name must not match every model
        self.assertIsNone(tool('Agent', {'subagent_type': 'worker', 'model': 'sonnet'}, local=local)[0])


if __name__ == '__main__':
    unittest.main()
