"""Skills install into both tools without touching folders the kit does not own; Codex agents, rules, hooks and the
memories pin are built from the kit's own files and the user's config."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN, CORE, HOOKS, env_for, make_local  # noqa: E402

sys.path.insert(0, HOOKS)
sys.path.insert(0, BIN)
import common  # noqa: E402
import guard  # noqa: E402,F401  (imported before any config patching, so its module-level settings come from the real config)
import build  # noqa: E402


KIT = None  # a throwaway copy of the kit: build.py writes build/ next to itself, never into the real checkout


def setUpModule():
    global KIT
    KIT = os.path.join(tempfile.mkdtemp(prefix='akit-kit-'), 'kit')
    shutil.copytree(CORE, KIT, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', 'build', 'local', 'tests', '__pycache__', '*.pyc'))


def tearDownModule():
    shutil.rmtree(os.path.dirname(KIT), ignore_errors=True)


def run_build(home, *args, **extra):
    """bin/build.py (of the kit copy) as a subprocess with HOME pointing at a throwaway folder and its own local/ and
    backup folders. extra may hold interpreter=<python> to run it with another Python."""
    local = extra.pop('local', None) or make_local({})
    interpreter = extra.pop('interpreter', sys.executable)
    extra.setdefault('AGENT_CORE_BACKUP', os.path.join(local, 'backups', 'test'))
    extra.setdefault('AGENT_CORE_CODEX_RUNNING', '0')
    env = env_for(local, HOME=home, **extra)
    env['AGENT_CORE_DIR'] = KIT
    return subprocess.run([interpreter, os.path.join(KIT, 'bin', 'build.py')] + list(args), capture_output=True,
                          text=True, env=env, timeout=60)


def parse_toml(text):
    return tomllib.loads(text)


class SkillInstall(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.agents = os.path.join(self.home, '.agents', 'skills')
        self.claude = os.path.join(self.home, '.claude', 'skills')

    def test_installs_into_both_and_keeps_other_folders(self):
        os.makedirs(os.path.join(self.agents, 'codex-gateway'))
        with open(os.path.join(self.agents, 'codex-gateway', 'SKILL.md'), 'w') as fh:
            fh.write('third party\n')
        out = run_build(self.home, '--install-skills')
        self.assertEqual(out.returncode, 0, out.stderr)
        for dest in (self.agents, self.claude):
            self.assertTrue(os.path.isfile(os.path.join(dest, 'handoff', 'SKILL.md')))
            with open(os.path.join(dest, '.agent-core-manifest.json')) as fh:
                managed = json.load(fh)['managed']
            self.assertIn('handoff', managed)
            self.assertNotIn('codex-gateway', managed)
        with open(os.path.join(self.agents, 'codex-gateway', 'SKILL.md')) as fh:
            self.assertEqual(fh.read(), 'third party\n')

    def test_placeholders_are_filled_in_installed_skills(self):
        run_build(self.home, '--install-skills')
        for dest in (self.agents, self.claude):
            for root, _, files in os.walk(dest):
                for f in files:
                    with open(os.path.join(root, f), encoding='utf-8') as fh:
                        text = fh.read()
                    self.assertNotIn('{{KIT}}', text, os.path.join(root, f))
                    self.assertNotIn('{{USER}}', text, os.path.join(root, f))

    def test_foreign_folder_with_same_name_is_left_alone(self):
        os.makedirs(os.path.join(self.agents, 'handoff'))
        with open(os.path.join(self.agents, 'handoff', 'SKILL.md'), 'w') as fh:
            fh.write('someone else\n')
        out = run_build(self.home, '--install-skills')
        self.assertIn('not agent-core', out.stdout)
        with open(os.path.join(self.agents, 'handoff', 'SKILL.md')) as fh:
            self.assertEqual(fh.read(), 'someone else\n')
        with open(os.path.join(self.agents, '.agent-core-manifest.json')) as fh:
            self.assertNotIn('handoff', json.load(fh)['managed'])

    def test_managed_folder_is_updated_and_dropped_skill_is_kept(self):
        run_build(self.home, '--install-skills')
        with open(os.path.join(self.agents, 'handoff', 'SKILL.md'), 'w') as fh:
            fh.write('stale\n')
        manifest = os.path.join(self.agents, '.agent-core-manifest.json')
        with open(manifest) as fh:
            data = json.load(fh)
        data['managed'].append('retired-skill')
        with open(manifest, 'w') as fh:
            json.dump(data, fh)
        os.makedirs(os.path.join(self.agents, 'retired-skill'))
        out = run_build(self.home, '--install-skills')
        with open(os.path.join(self.agents, 'handoff', 'SKILL.md')) as fh:
            self.assertNotEqual(fh.read(), 'stale\n')
        self.assertTrue(os.path.isdir(os.path.join(self.agents, 'retired-skill')))
        self.assertIn('retired-skill is no longer in agent-core-kit', out.stdout)


class Isolated(unittest.TestCase):
    """build.py's functions run in this process against a throwaway local/ folder and build/ folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.local = make_local({})
        self.addCleanup(shutil.rmtree, self.local, True)
        self.addCleanup(common.config, True)  # runs last: reload the real config once the patches are gone
        for patch in (mock.patch.multiple(common, LOCAL=self.local, RULEBOOK=os.path.join(self.local, 'AGENTS.md'),
                                          CONFIG_PATH=os.path.join(self.local, 'config.json')),
                      mock.patch.object(build, 'BUILD', os.path.join(self.tmp, 'build'))):
            patch.start()
            self.addCleanup(patch.stop)
        common.config(reload=True)

    def config(self, cfg):
        with open(os.path.join(self.local, 'config.json'), 'w') as fh:
            json.dump(cfg, fh)
        common.config(reload=True)

    def agents(self):
        build.build_codex()
        folder = os.path.join(build.BUILD, 'codex', 'agents')
        out = {}
        for f in sorted(os.listdir(folder)):
            with open(os.path.join(folder, f), encoding='utf-8') as fh:
                out[f] = fh.read()
        return out


class CodexAgents(Isolated):
    def test_roles_build_as_valid_toml_without_a_model_line(self):
        texts = self.agents()
        self.assertEqual(sorted(texts), ['explorer.toml', 'reviewer.toml', 'verifier.toml', 'worker.toml'])
        for f, text in texts.items():
            self.assertNotIn('\nmodel = ', text, f)
            self.assertIn('model_reasoning_effort = ', text, f)
            self.assertNotIn('{{', text, f)
            if tomllib:
                data = parse_toml(text)
                for key in ('name', 'description', 'developer_instructions', 'model_reasoning_effort'):
                    self.assertTrue(data.get(key), '%s lacks %s' % (f, key))
                self.assertNotIn('model', data)
        self.assertIn("developer_instructions = '''\nRead-only", texts['reviewer.toml'])
        self.assertNotIn("developer_instructions = '''\nRead-only", texts['worker.toml'])

    def test_model_line_only_for_roles_the_config_names(self):
        self.config({'codex': {'models': {'worker': 'model-w', 'Explore': 'model-e', 'REVIEWER': 'model-r', 'verifier': ' '}}})
        texts = self.agents()
        self.assertIn('\nmodel = "model-w"\n', texts['worker.toml'])
        self.assertIn('\nmodel = "model-e"\n', texts['explorer.toml'], 'the Claude role name works too')
        self.assertIn('\nmodel = "model-r"\n', texts['reviewer.toml'], 'role names are not case sensitive')
        self.assertNotIn('\nmodel = ', texts['verifier.toml'], 'a blank model means none')
        if tomllib:
            self.assertEqual(parse_toml(texts['worker.toml'])['model'], 'model-w')

    def test_user_name_fills_the_placeholder_in_role_text(self):
        self.config({'user_name': 'Alex'})
        text = '\n'.join(self.agents().values())
        self.assertNotIn('{{USER}}', text)

    def test_without_tomllib_the_build_still_works_and_says_so(self):
        buf = io.StringIO()
        with mock.patch.object(build, 'tomllib', None), contextlib.redirect_stdout(buf):
            build.build_codex(quiet=False)
        self.assertIn('no tomllib', buf.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(build.BUILD, 'codex', 'agents', 'worker.toml')))

    def test_a_broken_role_file_is_caught_when_tomllib_exists(self):
        if not tomllib:
            self.skipTest('needs tomllib')
        with mock.patch.object(build, 'codex_agents', return_value={'bad': 'name = = 1\n'}):
            with self.assertRaises(ValueError):
                build.build_codex()

    def test_rules_follow_the_guard_table(self):
        text = build.codex_rules()
        self.assertEqual(text.count('prefix_rule('), len(guard.CODEX_RULES))
        self.assertIn('decision="forbidden"', text)
        self.assertNotIn('{{', text)


class CodexHooks(Isolated):
    def test_user_hooks_stay_and_the_kits_are_replaced_not_duplicated(self):
        user = {'hooks': [{'type': 'command', 'command': 'echo mine'}]}
        current = {'other': 1, 'hooks': {'Stop': [user]}}
        once = build.merged_hooks(current)
        self.assertEqual(once['other'], 1)
        self.assertEqual(once['hooks']['Stop'][0], user)
        self.assertEqual(json.dumps(once).count('codex_hook.py stop'), 1)
        self.assertEqual(build.merged_hooks(once), once, 'a second merge changes nothing')
        self.assertEqual(sorted(once['hooks']), ['PreToolUse', 'SessionStart', 'Stop', 'UserPromptSubmit'])

    def test_a_user_command_in_the_same_entry_as_the_kits_survives(self):
        mixed = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo mine'},
                                                {'type': 'command', 'command': build.HOOK_PYTHON + ' ' +
                                                 build.CODEX_HOOK_SCRIPT + ' stop'}]}]}}
        merged = build.merged_hooks(mixed)['hooks']['Stop']
        self.assertEqual(merged[0], {'hooks': [{'type': 'command', 'command': 'echo mine'}]})
        self.assertEqual(len(merged), 2)

    def test_hooks_json_is_built_with_the_installed_script_path(self):
        build.build_codex()
        with open(os.path.join(build.BUILD, 'codex', 'hooks.json')) as fh:
            data = json.load(fh)
        self.assertIn(os.path.join('.claude', 'hooks', 'agent-core', 'codex_hook.py'), json.dumps(data))


class CodexInstall(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.codex = os.path.join(self.home, '.codex')
        os.makedirs(self.codex)
        self.config = os.path.join(self.codex, 'config.toml')
        with open(self.config, 'w') as fh:
            fh.write('model = "mine"\n\n[features]\napps = true\n')
        self.local = make_local({})
        self.addCleanup(shutil.rmtree, self.local, True)

    def write_config(self, cfg):
        with open(os.path.join(self.local, 'config.json'), 'w') as fh:
            json.dump(cfg, fh)

    def test_config_toml_is_left_alone_unless_the_pin_is_asked_for(self):
        out = run_build(self.home, '--install-codex', local=self.local)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(self.config) as fh:
            self.assertEqual(fh.read(), 'model = "mine"\n\n[features]\napps = true\n')
        for rel in (('AGENTS.md',), ('rules', 'agent-core.rules'), ('agents', 'worker.toml'), ('hooks.json',)):
            self.assertTrue(os.path.isfile(os.path.join(self.codex, *rel)), rel)
        self.assertIn('/hooks', out.stdout)

    def test_pin_memories_off_edits_one_line_and_keeps_a_backup(self):
        self.write_config({'codex': {'models': {}, 'pin_memories_off': True}})
        out = run_build(self.home, '--install-codex', local=self.local)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(self.config) as fh:
            self.assertEqual(fh.read(), 'model = "mine"\n\n[features]\nmemories = false\napps = true\n')
        backup = os.path.join(self.local, 'backups', 'test', 'codex', 'config.toml')
        with open(backup) as fh:
            self.assertEqual(fh.read(), 'model = "mine"\n\n[features]\napps = true\n')
        before = os.stat(self.config).st_mtime_ns
        run_build(self.home, '--install-codex', local=self.local)
        self.assertEqual(os.stat(self.config).st_mtime_ns, before, 'already pinned: not rewritten')

    def test_nothing_in_codex_changes_while_codex_runs_except_the_rulebook(self):
        out = run_build(self.home, '--install-codex', local=self.local, AGENT_CORE_CODEX_RUNNING='1')
        self.assertIn('wait until no Codex process runs', out.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.codex, 'AGENTS.md')))
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'agents')))
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'hooks.json')))

    def test_no_codex_folder_is_not_an_error(self):
        shutil.rmtree(self.codex)
        out = run_build(self.home, '--install-codex', local=self.local)
        self.assertEqual(out.returncode, 0)
        self.assertIn('Codex files were skipped', out.stdout)
        self.assertFalse(os.path.exists(self.codex))


class MemoriesPin(unittest.TestCase):
    def check_both(self, fn):
        fn()
        with mock.patch.object(build, 'tomllib', None):
            fn()

    def test_pin_keeps_everything_else(self):
        def run():
            text = 'model = "m"\n\n[features]\napps = true\nmemories = true\n\n[mcp_servers.x]\ncommand = "y"\n'
            out = build.pin_memories(text)
            self.assertEqual(out, text.replace('memories = true', 'memories = false'))
            self.assertTrue(build.memories_pinned(out))
        self.check_both(run)

    def test_pin_adds_the_table_or_the_key(self):
        def run():
            self.assertEqual(build.pin_memories('model = "m"\n'), 'model = "m"\n\n[features]\nmemories = false\n')
            self.assertEqual(build.pin_memories(''), '[features]\nmemories = false\n')
            self.assertEqual(build.pin_memories('[features]\napps = true\n'), '[features]\nmemories = false\napps = true\n')
            self.assertFalse(build.memories_pinned('[features]\napps = true\n'))
            self.assertFalse(build.memories_pinned('[other]\nmemories = false\n'))
            self.assertFalse(build.memories_pinned('[features]\nmemories = true\n'))
        self.check_both(run)


class PinWithoutTomllib(unittest.TestCase):
    """Python 3.9 and 3.10 have no tomllib, so the edit cannot be checked afterwards: it is made only on simple files and
    never adds a second [features] table."""

    def pin(self, text):
        with mock.patch.object(build, 'tomllib', None):
            return build.pin_memories(text)

    def test_a_file_with_any_other_mention_of_features_is_not_pinned(self):
        for text in ('features.memories = true\n',                            # dotted key at the top
                     'features = { apps = true }\n',                           # inline table
                     '[features]\napps = true\n\n[features.sub]\nx = 1\n',     # a sub-table next to the header
                     '[features]\napps = true\n[features]\n',                 # the header twice
                     '# features I like\nmodel = "m"\n',                       # even a comment
                     '[[features]]\nname = "x"\n',
                     '["features"]\napps = true\n',
                     'model = "m"\n[other]\nfeatures.apps = true\n'):
            self.assertIsNone(self.pin(text), text)

    def test_a_file_with_no_mention_or_one_plain_header_is_pinned_with_one_features_table(self):
        out = self.pin('model = "m"\n')
        self.assertEqual(out, 'model = "m"\n\n[features]\nmemories = false\n')
        self.assertEqual(out.count('[features]'), 1)
        self.assertEqual(self.pin('[features]\napps = true\n[other]\nx = 1\n'),
                         '[features]\nmemories = false\napps = true\n[other]\nx = 1\n')
        self.assertEqual(self.pin('[features] # mine\nmemories = true\n'), '[features] # mine\nmemories = false\n')
        self.assertEqual(self.pin(''), '[features]\nmemories = false\n')

    def test_with_tomllib_the_result_is_parsed_so_a_second_table_is_refused_too(self):
        if tomllib is None:
            self.skipTest('needs tomllib')
        self.assertIsNone(build.pin_memories('features.memories = true\n'))
        self.assertIsNone(build.pin_memories('features = { apps = true }\n'))

    def test_install_prints_that_it_did_not_pin_and_how_to_set_it_by_hand(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        os.makedirs(os.path.join(home, '.codex'))
        config = os.path.join(home, '.codex', 'config.toml')
        with open(config, 'w') as fh:
            fh.write('features.apps = true\n')
        local = make_local({'codex': {'models': {}, 'pin_memories_off': True}})
        self.addCleanup(shutil.rmtree, local, True)
        out = run_build(home, '--install-codex', local=local)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('did not pin', out.stdout)
        self.assertIn('memories = false', out.stdout)
        self.assertIn('[features]', out.stdout)
        with open(config) as fh:
            self.assertEqual(fh.read(), 'features.apps = true\n')

    def test_an_interpreter_without_tomllib_leaves_such_a_file_alone(self):
        old = [p for p in (shutil.which(n) for n in ('python3.9', 'python3.10')) if p]
        if not old:
            self.skipTest('no python3.9 or python3.10 on PATH')
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        os.makedirs(os.path.join(home, '.codex'))
        config = os.path.join(home, '.codex', 'config.toml')
        with open(config, 'w') as fh:
            fh.write('features.apps = true\n')
        local = make_local({'codex': {'models': {}, 'pin_memories_off': True}})
        self.addCleanup(shutil.rmtree, local, True)
        out = run_build(home, '--install-codex', local=local, interpreter=old[0])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('did not pin', out.stdout)
        with open(config) as fh:
            self.assertEqual(fh.read(), 'features.apps = true\n', 'no second [features] table')


class PluginCheck(unittest.TestCase):
    def setUp(self):
        self.saved = build.PLUGINS_DIR
        build.PLUGINS_DIR = self.plugins = tempfile.mkdtemp()
        self.addCleanup(setattr, build, 'PLUGINS_DIR', self.saved)
        self.addCleanup(shutil.rmtree, self.plugins, True)

    def write(self, rel, data):
        path = os.path.join(self.plugins, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            json.dump(data, fh)

    def test_clean_install_has_no_problems_and_drift_is_reported(self):
        installed = os.path.join(self.plugins, 'cache', 'openai-codex', 'codex', '1.0.6')
        shutil.copytree(os.path.join(build.PLUGIN_SRC, 'plugins', 'codex'), installed)
        self.write('known_marketplaces.json', {'openai-codex': {'source': {}, 'autoUpdate': False}})
        self.write('installed_plugins.json', {'plugins': {'codex@openai-codex': [{'installPath': installed}]}})
        self.write('data/codex-openai-codex/state/repo-1/state.json', {'config': {'stopReviewGate': False}})
        self.assertEqual(build.plugin_problems(), [])
        with open(os.path.join(installed, 'commands', 'rescue.md'), 'a') as fh:
            fh.write('drift\n')
        self.write('known_marketplaces.json', {'openai-codex': {'source': {}}})
        self.write('data/codex-openai-codex/state/repo-2/state.json', {'config': {'stopReviewGate': True}})
        problems = '\n'.join(build.plugin_problems())
        for want in ('auto-update on', 'differs from vendor/ at commands/rescue.md', 'stop review gate is on'):
            self.assertIn(want, problems)


if __name__ == '__main__':
    unittest.main()
