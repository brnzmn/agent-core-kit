"""install.sh end to end in a sandbox: a temp HOME that already holds someone's own Claude Code and Codex setup, and a
temp copy of the kit (so local/ is created inside the copy, never in the real checkout)."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CORE  # noqa: E402

MARK = 'hooks/agent-core/'


def sandbox_path(hide=('claude', 'codex')):
    """PATH for the sandbox with the claude and codex CLIs hidden: a folder that holds one is replaced by a folder of
    links to everything else in it. Returns (PATH, shim folder to delete)."""
    shim = tempfile.mkdtemp(prefix='akit-path-')
    dirs = []
    for d in os.environ.get('PATH', '').split(os.pathsep):
        if not d or not os.path.isdir(d):
            continue
        if not any(os.path.exists(os.path.join(d, n)) for n in hide):
            dirs.append(d)
            continue
        sub = os.path.join(shim, str(len(dirs)))
        os.makedirs(sub)
        for f in os.listdir(d):
            if f not in hide:
                try:
                    os.symlink(os.path.join(d, f), os.path.join(sub, f))
                except OSError:
                    pass
        dirs.append(sub)
    return os.pathsep.join(dirs), shim


def copy_kit(dst):
    shutil.copytree(CORE, dst, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', 'build', 'local', '__pycache__', 'tests', '*.pyc'))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        fh.write(text)


def read(path):
    with open(path) as fh:
        return fh.read()


def snapshot(root):
    out = {}
    for r, _, files in os.walk(root):
        for f in files:
            p = os.path.join(r, f)
            with open(p, 'rb') as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


class Sandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path, cls.shim = sandbox_path()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shim, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='akit-install-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        self.kit = os.path.join(self.tmp, 'kit')
        self.claude = os.path.join(self.home, '.claude')
        self.codex = os.path.join(self.home, '.codex')
        self.local = os.path.join(self.kit, 'local')
        copy_kit(self.kit)
        self.seed()

    def seed(self):
        write(os.path.join(self.claude, 'skills', 'my-skill', 'SKILL.md'), '---\nname: my-skill\n---\nmine\n')
        write(os.path.join(self.claude, 'agents', 'my-agent.md'), '---\nname: my-agent\n---\nmine\n')
        write(os.path.join(self.claude, 'agents', 'worker.md'), '---\nname: worker\n---\nmy own worker\n')
        write(os.path.join(self.claude, 'CLAUDE.md'), 'my own global rules\n')
        self.own = {'allow': ['Bash(ls)'], 'ask': ['Bash(rm *)'], 'deny': ['Read(~/private/**)']}
        write(os.path.join(self.claude, 'settings.json'), json.dumps({
            'permissions': dict(self.own),
            'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo mine'}]}]},
            'theme': 'dark'}))
        write(os.path.join(self.codex, 'config.toml'), 'model = "my-model"\n')
        write(os.path.join(self.codex, 'hooks.json'), json.dumps(
            {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo my codex stop'}]}]}}))

    def env(self, **extra):
        env = dict((k, v) for k, v in os.environ.items() if not k.startswith(('AGENT_CORE', 'GIT_')) and k != 'XDG_CONFIG_HOME')
        env.update(HOME=self.home, PATH=self.path, AGENT_CORE_CODEX_RUNNING='0', GIT_CONFIG_NOSYSTEM='1')
        env.update(extra)
        return env

    def install(self, *args, **extra):
        return subprocess.run(['bash', os.path.join(self.kit, 'install.sh')] + list(args), capture_output=True,
                              text=True, env=self.env(**extra), cwd=self.tmp, timeout=120)

    def ok(self, *args, **extra):
        out = self.install(*args, **extra)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out

    def configure(self, **cfg):
        write(os.path.join(self.local, 'config.json'), json.dumps(cfg))

    def settings(self):
        return json.loads(read(os.path.join(self.claude, 'settings.json')))

    def backups(self):
        root = os.path.join(self.local, 'backups')
        return sorted(os.listdir(root)) if os.path.isdir(root) else []

    def own_commands(self, settings, event):
        return [h['command'] for e in settings['hooks'].get(event, []) for h in e['hooks'] if MARK not in h['command']]


class FirstInstall(Sandbox):
    def test_installs_beside_what_the_user_has(self):
        out = self.ok()
        self.assertIn('/setup', out.stdout)
        self.assertIn('new Claude Code session', out.stdout)
        self.assertNotIn(CORE + os.sep + 'local', out.stdout, 'local/ lives in the kit copy, not the real checkout')
        # the user's own files survive
        self.assertEqual(read(os.path.join(self.claude, 'skills', 'my-skill', 'SKILL.md')), '---\nname: my-skill\n---\nmine\n')
        self.assertEqual(read(os.path.join(self.claude, 'agents', 'my-agent.md')), '---\nname: my-agent\n---\nmine\n')
        s = self.settings()
        for key, rules in self.own.items():
            for rule in rules:
                self.assertIn(rule, s['permissions'][key])
        self.assertEqual(self.own_commands(s, 'PreToolUse'), ['echo mine'])
        self.assertEqual(s['theme'], 'dark')
        self.assertEqual(read(os.path.join(self.codex, 'config.toml')), 'model = "my-model"\n', 'config.toml untouched by default')
        codex_hooks = json.loads(read(os.path.join(self.codex, 'hooks.json')))['hooks']
        self.assertIn('echo my codex stop', json.dumps(codex_hooks['Stop']))
        self.assertIn(MARK, json.dumps(codex_hooks['Stop']))
        # the kit's files are in
        hooks = os.path.join(self.claude, 'hooks', 'agent-core')
        for f in ('guard.py', 'budget.py', 'capture.py', 'session_start.py', 'stop_steps.py', 'common.py', 'codex_hook.py'):
            self.assertTrue(os.access(os.path.join(hooks, f), os.X_OK), f)
        self.assertEqual(read(os.path.join(hooks, '.core-dir')).strip(), self.kit)
        claude_md = read(os.path.join(self.claude, 'CLAUDE.md'))
        self.assertIn('Generated by', claude_md)
        self.assertNotIn('{{', claude_md)
        self.assertNotEqual(claude_md, 'my own global rules\n')
        self.assertIn('Generated by', read(os.path.join(self.codex, 'AGENTS.md')))
        for skills in (os.path.join(self.claude, 'skills'), os.path.join(self.home, '.agents', 'skills')):
            self.assertTrue(os.path.isfile(os.path.join(skills, 'handoff', 'SKILL.md')), skills)
            self.assertNotIn('{{', read(os.path.join(skills, 'handoff', 'SKILL.md')))
        self.assertTrue(os.path.isfile(os.path.join(self.claude, 'skills', 'setup', 'SKILL.md')))
        self.assertIn(self.kit, read(os.path.join(self.claude, 'skills', 'setup', 'SKILL.md')), '{{KIT}} is filled in')
        self.assertTrue(os.path.isfile(os.path.join(self.claude, 'agents', 'reviewer.md')))
        for f in ('explorer', 'worker', 'verifier', 'reviewer'):
            self.assertTrue(os.path.isfile(os.path.join(self.codex, 'agents', f + '.toml')), f)
        self.assertTrue(os.path.isfile(os.path.join(self.codex, 'rules', 'agent-core.rules')))
        # every kit hook points at the installed scripts
        self.assertEqual(json.dumps(s['hooks']).count(MARK), 6)
        self.assertEqual(s['model'], 'opus')
        self.assertEqual(s['env']['CLAUDE_CODE_SUBAGENT_MODEL'], 'sonnet')
        self.assertNotIn('switchModelsOnFlag', s)
        self.assertNotIn('autoMode', s)
        self.assertNotIn('defaultMode', s['permissions'], 'defaultMode stays the user\'s unless permission_mode is set')
        self.assertEqual(s['permissions']['ask'], ['Bash(rm *)'])
        self.assertIn('Bash(sudo *)', s['permissions']['deny'])
        self.assertIn('Bash(python3 %s/bin/learn.py *)' % self.kit, s['permissions']['allow'])

    def test_local_folder_is_created_in_the_copy_and_committed(self):
        self.ok()
        for rel in ('AGENTS.md', 'config.json', 'learning', '.git'):
            self.assertTrue(os.path.exists(os.path.join(self.local, rel)), rel)
        log = subprocess.run(['git', '-C', self.local, 'log', '--oneline'], capture_output=True, text=True, env=self.env())
        self.assertEqual(log.returncode, 0, log.stderr)
        self.assertEqual(len(log.stdout.strip().splitlines()), 1)
        tracked = subprocess.run(['git', '-C', self.local, 'ls-files'], capture_output=True, text=True, env=self.env()).stdout
        self.assertIn('AGENTS.md', tracked)
        self.assertIn('config.json', tracked)
        self.assertNotIn('backups', tracked)
        self.assertEqual(json.loads(read(os.path.join(self.local, 'config.json')))['stop_hook'], True)

    def test_replaced_files_are_backed_up(self):
        self.ok()
        (name,) = self.backups()
        bk = os.path.join(self.local, 'backups', name)
        self.assertEqual(read(os.path.join(bk, 'claude', 'CLAUDE.md')), 'my own global rules\n')
        # changed with the agents fix: a same-named agent of the user's is left alone, so there is nothing to back up
        self.assertEqual(read(os.path.join(self.claude, 'agents', 'worker.md')), '---\nname: worker\n---\nmy own worker\n')
        self.assertFalse(os.path.exists(os.path.join(bk, 'claude', 'agents', 'worker.md')))
        self.assertEqual(json.loads(read(os.path.join(bk, 'claude', 'settings.json')))['theme'], 'dark')
        self.assertEqual(read(os.path.join(bk, 'codex', 'config.toml')), 'model = "my-model"\n')
        self.assertIn('echo my codex stop', read(os.path.join(bk, 'codex', 'hooks.json')))
        self.assertFalse(os.path.exists(os.path.join(bk, 'ARCHIVED')))

    def test_existing_local_files_are_kept(self):
        write(os.path.join(self.local, 'AGENTS.md'), '# Working with Sam\n\n- my rule\n')
        self.configure(user_name='Sam')
        self.ok()
        self.assertEqual(read(os.path.join(self.local, 'AGENTS.md')), '# Working with Sam\n\n- my rule\n')
        self.assertIn('my rule', read(os.path.join(self.claude, 'CLAUDE.md')))
        self.assertEqual(json.loads(read(os.path.join(self.local, 'config.json')))['user_name'], 'Sam')

    def test_no_cli_in_path_skips_the_plugin_and_says_so(self):
        out = self.ok()
        self.assertIn('plugin: skipped (no claude CLI)', out.stdout)
        self.assertIn('claude CLI not found', out.stderr)

    def test_bad_settings_json_stops_before_changing_anything(self):
        write(os.path.join(self.claude, 'settings.json'), '{ not json')
        out = self.install()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('not valid JSON', out.stdout + out.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'hooks')))
        self.assertEqual(read(os.path.join(self.claude, 'settings.json')), '{ not json')

    def test_unknown_flag_and_help(self):
        out = self.install('--bogus')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('unknown option', out.stderr)
        out = self.ok('--help')
        self.assertIn('--uninstall', out.stdout)

    def test_fake_claude_cli_gets_the_plugin_commands(self):
        fake = os.path.join(self.tmp, 'fakebin')
        log = os.path.join(self.tmp, 'claude.log')
        write(os.path.join(fake, 'claude'), '#!/bin/sh\necho "$@" >> "%s"\n'
              'case "$*" in "plugin marketplace add"*) mkdir -p "$HOME/.claude/plugins"\n'
              '  echo \'{"openai-codex": {"source": {}, "autoUpdate": true}}\' > "$HOME/.claude/plugins/known_marketplaces.json";;\n'
              'esac\nexit 0\n' % log)
        os.chmod(os.path.join(fake, 'claude'), 0o755)
        out = self.ok(PATH=fake + os.pathsep + self.path)
        calls = read(log)
        market = json.loads(read(os.path.join(self.claude, 'plugins', 'known_marketplaces.json')))
        self.assertIs(market['openai-codex']['autoUpdate'], False, out.stdout)
        self.assertIn('plugin marketplace add %s --scope user' % os.path.join(self.kit, 'vendor', 'codex-plugin-cc'), calls)
        self.assertIn('plugin install codex@openai-codex --scope user', calls)


class Sync(Sandbox):
    def test_moving_the_kit_folder_repoints_hooks_and_rules_on_the_next_sync(self):
        self.ok()
        old = self.kit
        self.kit = os.path.join(self.tmp, 'moved', 'kit')
        os.makedirs(os.path.dirname(self.kit))
        shutil.move(old, self.kit)
        self.local = os.path.join(self.kit, 'local')
        self.assertEqual(self.install('--check').returncode, 1)
        out = self.ok('--sync')
        self.assertIn('now point at ' + self.kit, out.stdout)
        self.assertEqual(read(os.path.join(self.claude, 'hooks', 'agent-core', '.core-dir')).strip(), self.kit)
        allow = self.settings()['permissions']['allow']
        self.assertIn('Bash(python3 %s/bin/learn.py *)' % self.kit, allow)
        self.assertNotIn('Bash(python3 %s/bin/learn.py *)' % old, allow)
        self.assertIn(self.kit, read(os.path.join(self.claude, 'CLAUDE.md')))
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_a_symlinked_settings_file_stays_a_link_and_its_target_is_updated(self):
        target = os.path.join(self.home, 'dotfiles', 'settings.json')
        write(target, read(os.path.join(self.claude, 'settings.json')))
        os.remove(os.path.join(self.claude, 'settings.json'))
        os.symlink(target, os.path.join(self.claude, 'settings.json'))
        self.ok()
        self.assertTrue(os.path.islink(os.path.join(self.claude, 'settings.json')))
        self.assertIn(MARK, read(target))
        self.assertEqual(self.own_commands(json.loads(read(target)), 'PreToolUse'), ['echo mine'])
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_check_says_in_sync_and_second_run_is_an_idempotent_sync(self):
        self.ok()
        out = self.ok('--check')
        self.assertIn('in sync', out.stdout)
        before, backups = snapshot(self.home), self.backups()
        out = self.ok()
        self.assertIn('already installed', out.stdout)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.backups(), backups, 'a sync that changes nothing makes no backup')
        out = self.ok('--sync')
        self.assertEqual(snapshot(self.home), before)
        self.assertIn('in sync', out.stdout)

    def test_check_reports_drift_and_sync_repairs_it(self):
        self.ok()
        guard = os.path.join(self.claude, 'hooks', 'agent-core', 'guard.py')
        with open(guard, 'a') as fh:
            fh.write('# edited\n')
        os.remove(os.path.join(self.claude, 'agents', 'reviewer.md'))
        out = self.install('--check')
        self.assertEqual(out.returncode, 1)
        self.assertIn('differs  ' + guard, out.stdout)
        self.assertIn('missing  ' + os.path.join(self.claude, 'agents', 'reviewer.md'), out.stdout)
        self.assertIn('drift: 2 item(s)', out.stdout)
        self.ok('--sync')
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_check_before_install_asks_for_an_install(self):
        out = self.install('--check')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('not installed', out.stderr)

    def test_edited_rulebook_reaches_both_tools_on_sync(self):
        self.ok()
        with open(os.path.join(self.local, 'AGENTS.md'), 'a') as fh:
            fh.write('- Always greet the reviewer.\n')
        self.assertEqual(self.install('--check').returncode, 1)
        self.ok('--sync')
        self.assertIn('greet the reviewer', read(os.path.join(self.claude, 'CLAUDE.md')))
        self.assertIn('greet the reviewer', read(os.path.join(self.codex, 'AGENTS.md')))
        self.assertIn('in sync', self.ok('--check').stdout)
        self.assertEqual(len(self.backups()), 1, 'regenerated files that the kit wrote itself are not backed up again')

    def test_user_edit_of_an_installed_agent_is_backed_up_before_sync_overwrites_it(self):
        self.ok()
        path = os.path.join(self.claude, 'agents', 'reviewer.md')
        write(path, 'my tweak\n')
        self.ok('--sync')
        newest = self.backups()[-1]
        self.assertEqual(read(os.path.join(self.local, 'backups', newest, 'claude', 'agents', 'reviewer.md')), 'my tweak\n')
        self.assertNotEqual(read(path), 'my tweak\n')

    def test_foreign_skill_with_a_kit_name_is_left_alone(self):
        write(os.path.join(self.claude, 'skills', 'handoff', 'SKILL.md'), 'someone else\n')
        out = self.ok()
        self.assertIn("handoff exists", out.stdout)
        self.assertEqual(read(os.path.join(self.claude, 'skills', 'handoff', 'SKILL.md')), 'someone else\n')
        self.assertIn('in sync', self.ok('--check').stdout, 'a folder that is not the kit\'s is not drift')
        self.ok('--sync')
        self.assertEqual(read(os.path.join(self.claude, 'skills', 'handoff', 'SKILL.md')), 'someone else\n')


class ConfigDrivenSettings(Sandbox):
    def test_protected_dirs_become_deny_rules_and_go_away_when_removed(self):
        self.configure(protected_dirs=['lake-data', '~/vault-data', '/srv/raw-data'])
        self.ok()
        deny = self.settings()['permissions']['deny']
        for rule in ('Edit(**/lake-data/**)', 'Edit(~/vault-data/**)', 'Edit(//srv/raw-data/**)', 'Read(~/private/**)'):
            self.assertIn(rule, deny)
        self.assertIn('in sync', self.ok('--check').stdout)
        self.configure(protected_dirs=['lake-data'])
        self.ok('--sync')
        deny = self.settings()['permissions']['deny']
        self.assertIn('Edit(**/lake-data/**)', deny)
        self.assertNotIn('Edit(~/vault-data/**)', deny)
        self.assertNotIn('Edit(//srv/raw-data/**)', deny)
        self.assertIn('Read(~/private/**)', deny, 'the user\'s own rule is never touched')
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_a_rule_the_user_already_had_is_not_removed_with_the_kits(self):
        write(os.path.join(self.claude, 'settings.json'), json.dumps({'permissions': {'deny': ['Edit(**/lake-data/**)']}}))
        self.configure(protected_dirs=['lake-data'])
        self.ok()
        self.configure(protected_dirs=[])
        self.ok('--sync')
        self.assertIn('Edit(**/lake-data/**)', self.settings()['permissions']['deny'])

    def test_stop_hook_false_leaves_no_stop_entry_and_true_brings_it_back(self):
        write(os.path.join(self.claude, 'settings.json'), json.dumps(
            {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo my stop'}]}]}}))
        self.configure(stop_hook=False)
        self.ok()
        s = self.settings()
        self.assertEqual(json.dumps(s['hooks']['Stop']).count(MARK), 0)
        self.assertEqual(self.own_commands(s, 'Stop'), ['echo my stop'])
        self.assertIn('in sync', self.ok('--check').stdout)
        self.configure(stop_hook=True)
        self.ok('--sync')
        s = self.settings()
        self.assertEqual(json.dumps(s['hooks']['Stop']).count('stop_steps.py'), 1)
        self.assertEqual(self.own_commands(s, 'Stop'), ['echo my stop'])
        self.configure(stop_hook=False)
        self.ok('--sync')
        s = self.settings()
        self.assertEqual(json.dumps(s['hooks']['Stop']).count(MARK), 0)
        self.assertEqual(self.own_commands(s, 'Stop'), ['echo my stop'])
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_stop_hook_false_without_a_user_stop_hook_has_no_stop_key(self):
        write(os.path.join(self.claude, 'settings.json'), '{}')
        self.configure(stop_hook=False)
        self.ok()
        self.assertNotIn('Stop', self.settings()['hooks'])

    def test_permission_mode_sets_default_mode_and_bypass_drops_ask_rules(self):
        self.configure(permission_mode='bypassPermissions')
        out = self.ok()
        perms = self.settings()['permissions']
        self.assertEqual(perms['defaultMode'], 'bypassPermissions')
        self.assertEqual(perms['ask'], [])
        self.assertIn('Bash(rm *)', out.stdout, 'the dropped rule is named')
        self.assertIn('Bash(rm *)', read(os.path.join(self.local, 'backups', self.backups()[0], 'claude', 'settings.json')))
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_other_permission_modes_keep_ask_rules(self):
        self.configure(permission_mode='acceptEdits')
        self.ok()
        perms = self.settings()['permissions']
        self.assertEqual(perms['defaultMode'], 'acceptEdits')
        self.assertEqual(perms['ask'], ['Bash(rm *)'])

    def test_auto_mode_environment(self):
        self.configure(auto_mode_environment=['Source control: GitHub org example-org is trusted.'])
        self.ok()
        self.assertEqual(self.settings()['autoMode']['environment'],
                         ['$defaults', 'Source control: GitHub org example-org is trusted.'])
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_model_is_set_on_install_and_left_alone_on_sync(self):
        self.ok()
        s = self.settings()
        s['model'] = 'sonnet'
        s['env']['MCP_TIMEOUT'] = '1234'
        write(os.path.join(self.claude, 'settings.json'), json.dumps(s))
        out = self.ok('--sync')
        s = self.settings()
        self.assertEqual(s['model'], 'sonnet')
        self.assertEqual(s['env']['MCP_TIMEOUT'], '1234')
        self.assertIn('your choice', out.stdout)

    def test_pin_memories_only_when_asked(self):
        self.ok()
        self.assertEqual(read(os.path.join(self.codex, 'config.toml')), 'model = "my-model"\n')
        self.configure(codex={'models': {}, 'pin_memories_off': True})
        self.assertEqual(self.install('--check').returncode, 1)
        self.ok('--sync')
        self.assertIn('memories = false', read(os.path.join(self.codex, 'config.toml')))
        self.assertIn('model = "my-model"', read(os.path.join(self.codex, 'config.toml')))
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_codex_files_wait_while_codex_runs(self):
        write(os.path.join(self.codex, 'hooks.json'), '{}')
        out = self.ok(AGENT_CORE_CODEX_RUNNING='1')
        self.assertIn('wait until no Codex process runs', out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'agents')))
        self.assertTrue(os.path.isfile(os.path.join(self.codex, 'AGENTS.md')), 'the rulebook is always installed')
        self.ok('--sync')
        self.assertTrue(os.path.isfile(os.path.join(self.codex, 'agents', 'worker.toml')))


class Undo(Sandbox):
    def test_uninstall_removes_the_kits_hooks_and_moves_its_files_into_a_backup(self):
        self.ok()
        out = self.ok('--uninstall')
        self.assertIn('Nothing was deleted', out.stdout)
        self.assertIn('--restore', out.stdout)
        s = self.settings()
        self.assertNotIn(MARK, json.dumps(s))
        self.assertEqual(self.own_commands(s, 'PreToolUse'), ['echo mine'])
        self.assertEqual(s['permissions']['allow'], ['Bash(ls)'])
        self.assertEqual(s['permissions']['deny'], ['Read(~/private/**)'])
        self.assertEqual(s['permissions']['ask'], ['Bash(rm *)'])
        codex_hooks = read(os.path.join(self.codex, 'hooks.json'))
        self.assertNotIn(MARK, codex_hooks)
        self.assertIn('echo my codex stop', codex_hooks)
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'hooks', 'agent-core')))
        # changed with the restore fix: the user's own CLAUDE.md comes back; the kit's generated one is in the backup
        self.assertEqual(read(os.path.join(self.claude, 'CLAUDE.md')), 'my own global rules\n')
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'AGENTS.md')), 'there was none before the install')
        for rel in (('agents', 'reviewer.md'), ('skills', 'handoff'), ('skills', '.agent-core-manifest.json'),
                    ('agents', '.agent-core-manifest.json')):
            self.assertFalse(os.path.exists(os.path.join(self.claude, *rel)), rel)
        self.assertIn('my own worker', read(os.path.join(self.claude, 'agents', 'worker.md')), 'a same-named agent of the user is theirs')
        self.assertFalse(os.path.exists(os.path.join(self.home, '.agents', 'skills', 'handoff')))
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'agents', 'worker.toml')))
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'rules', 'agent-core.rules')))
        # the user's own things stay
        self.assertTrue(os.path.isfile(os.path.join(self.claude, 'skills', 'my-skill', 'SKILL.md')))
        self.assertTrue(os.path.isfile(os.path.join(self.claude, 'agents', 'my-agent.md')))
        self.assertEqual(read(os.path.join(self.codex, 'config.toml')), 'model = "my-model"\n')
        self.assertTrue(os.path.isdir(os.path.join(self.local, '.git')), 'local/ is yours and stays')
        # everything moved is in the uninstall backup
        names = [b for b in self.backups() if b.endswith('-uninstall')]
        self.assertEqual(len(names), 1)
        bk = os.path.join(self.local, 'backups', names[0])
        for rel in (('claude', 'hooks', 'agent-core', 'guard.py'), ('claude', 'CLAUDE.md'), ('claude', 'agents', 'reviewer.md'),
                    ('claude', 'skills', 'handoff', 'SKILL.md'), ('codex', 'AGENTS.md'), ('codex', 'agents', 'worker.toml'),
                    ('home', '.agents', 'skills', 'handoff', 'SKILL.md'), ('claude', 'settings.json')):
            self.assertTrue(os.path.exists(os.path.join(bk, *rel)), rel)
        self.assertIn(MARK, read(os.path.join(bk, 'claude', 'settings.json')), 'the backup holds the settings from before')

    def test_restore_after_uninstall_puts_everything_back(self):
        self.ok()
        before = snapshot(self.home)
        self.ok('--uninstall')
        (name,) = [b for b in self.backups() if b.endswith('-uninstall')]
        self.ok('--restore', name)
        self.assertEqual(snapshot(self.home), before)
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_restore_of_the_install_backup_brings_back_the_old_files(self):
        self.ok()
        first = self.backups()[0]
        out = self.ok('--restore', first)
        self.assertIn('Restored', out.stdout)
        self.assertEqual(read(os.path.join(self.claude, 'CLAUDE.md')), 'my own global rules\n')
        self.assertIn('my own worker', read(os.path.join(self.claude, 'agents', 'worker.md')))
        s = self.settings()
        self.assertNotIn(MARK, json.dumps(s))
        self.assertEqual(s['theme'], 'dark')
        self.assertEqual(read(os.path.join(self.codex, 'config.toml')), 'model = "my-model"\n')
        before = [b for b in self.backups() if b.endswith('before-restore')]
        self.assertEqual(len(before), 1, 'what the restore replaced is kept too')
        self.assertIn('Generated by', read(os.path.join(self.local, 'backups', before[0], 'claude', 'CLAUDE.md')))
        self.ok('--sync')
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_restore_rejects_unknown_names_and_paths(self):
        self.ok()
        out = self.install('--restore', 'nope')
        self.assertEqual(out.returncode, 1)
        self.assertIn('Available:', out.stdout)
        out = self.install('--restore', '../x')
        self.assertEqual(out.returncode, 1)
        out = self.install('--restore')
        self.assertEqual(out.returncode, 1)
        self.assertIn('usage', out.stderr)

    def test_clean_slate_archives_the_old_folders_and_restore_brings_them_back(self):
        out = self.ok('--clean-slate')
        self.assertIn('--clean-slate', out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'skills', 'my-skill')))
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'agents', 'my-agent.md')))
        self.assertTrue(os.path.isfile(os.path.join(self.claude, 'skills', 'handoff', 'SKILL.md')))
        bk = os.path.join(self.local, 'backups', self.backups()[0])
        self.assertTrue(os.path.isfile(os.path.join(bk, 'claude', 'skills', 'my-skill', 'SKILL.md')))
        self.assertTrue(os.path.isfile(os.path.join(bk, 'claude', 'agents', 'my-agent.md')))
        self.assertIn('agents', read(os.path.join(bk, 'ARCHIVED')))
        self.ok('--restore', self.backups()[0])
        self.assertTrue(os.path.isfile(os.path.join(self.claude, 'skills', 'my-skill', 'SKILL.md')))
        self.assertTrue(os.path.isfile(os.path.join(self.claude, 'agents', 'my-agent.md')))
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'skills', 'handoff')), 'restore swaps the folder back as a whole')
        self.assertTrue(any(b.endswith('before-restore') for b in self.backups()))

    def test_clean_slate_drops_user_hooks_whose_scripts_were_archived(self):
        write(os.path.join(self.claude, 'hooks', 'mine.sh'), '#!/bin/sh\n')
        s = self.settings()
        s['hooks']['Stop'] = [{'hooks': [{'type': 'command', 'command': 'sh ~/.claude/hooks/mine.sh'}]}]
        write(os.path.join(self.claude, 'settings.json'), json.dumps(s))
        out = self.ok('--clean-slate')
        self.assertIn('dropped 1 of your hooks', out.stdout)
        self.assertNotIn('mine.sh', json.dumps(self.settings()))
        self.assertIn('mine.sh', read(os.path.join(self.local, 'backups', self.backups()[0], 'claude', 'settings.json')))


class NoCodex(Sandbox):
    def seed(self):
        super(NoCodex, self).seed()
        shutil.rmtree(self.codex)

    def test_installs_claude_side_and_says_how_to_add_codex_later(self):
        out = self.ok()
        self.assertIn('Codex files were skipped', out.stdout)
        self.assertFalse(os.path.exists(self.codex))
        self.assertIn('in sync', self.ok('--check').stdout)


class InstallState(Sandbox):
    """The record of what the kit added lives in local/, so --clean-slate (which archives ~/.claude/hooks) cannot take it."""

    def test_install_clean_slate_uninstall_leaves_none_of_the_kits_rules(self):
        self.ok()
        self.ok('--clean-slate')
        self.assertTrue(os.path.isfile(os.path.join(self.local, 'install-state.json')))
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'hooks', 'agent-core', '.install-state.json')))
        self.ok('--uninstall')
        s = self.settings()
        self.assertEqual(s['permissions']['allow'], ['Bash(ls)'])
        self.assertEqual(s['permissions']['deny'], ['Read(~/private/**)'])
        self.assertNotIn(MARK, json.dumps(s))
        self.assertNotIn('learn.py', json.dumps(s))

    def test_state_is_git_ignored_in_a_new_local_and_added_to_an_old_one(self):
        self.ok()
        ignore = os.path.join(self.local, '.gitignore')
        self.assertIn('install-state.json', read(ignore).splitlines())
        status = subprocess.run(['git', '-C', self.local, 'status', '--porcelain', '--ignored'], capture_output=True,
                                text=True, env=self.env()).stdout
        self.assertIn('!! install-state.json', status)
        write(ignore, 'backups/\nlearning/inbox/')  # a local/ from an earlier kit: no entry, no final newline
        self.ok('--sync')
        self.assertEqual(read(ignore).splitlines(), ['backups/', 'learning/inbox/', 'install-state.json'])
        self.ok('--sync')
        self.assertEqual(read(ignore).count('install-state.json'), 1)

    def test_a_state_file_left_by_an_older_kit_is_moved_into_local_on_sync(self):
        self.ok()
        new = os.path.join(self.local, 'install-state.json')
        old = os.path.join(self.claude, 'hooks', 'agent-core', '.install-state.json')
        shutil.move(new, old)
        self.ok('--sync')
        self.assertTrue(os.path.isfile(new))
        self.assertFalse(os.path.exists(old))
        self.assertIn('learn.py', json.dumps(json.loads(read(new))['settings']), 'what the old file recorded came along')
        self.ok('--uninstall')
        self.assertEqual(self.settings()['permissions']['allow'], ['Bash(ls)'])


    def test_undoing_an_uninstall_brings_the_record_back_so_a_second_uninstall_still_knows_the_rules(self):
        self.ok()
        self.ok('--uninstall')
        self.assertFalse(os.path.exists(os.path.join(self.local, 'install-state.json')), 'moved into the uninstall backup')
        (down,) = [b for b in self.backups() if b.endswith('-uninstall')]
        self.assertTrue(os.path.isfile(os.path.join(self.local, 'backups', down, 'local', 'install-state.json')))
        self.ok('--restore', down)
        self.assertTrue(os.path.isfile(os.path.join(self.local, 'install-state.json')))
        self.assertIn('learn.py', json.dumps(self.settings()))
        self.ok('--uninstall')
        self.assertNotIn('learn.py', json.dumps(self.settings()))
        self.assertEqual(self.settings()['permissions']['allow'], ['Bash(ls)'])


class PersonalFolder(Sandbox):
    def setUp(self):
        super(PersonalFolder, self).setUp()
        self.custom = os.path.join(self.tmp, 'my-personal-folder')

    def hooks_dir(self):
        return os.path.join(self.claude, 'hooks', 'agent-core')

    def test_the_folder_is_recorded_and_hooks_find_it_without_the_variable(self):
        self.ok(AGENT_CORE_LOCAL=self.custom)
        self.assertEqual(read(os.path.join(self.hooks_dir(), '.local-dir')).strip(), self.custom)
        out = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, %r); import common; print(common.LOCAL)'
                              % self.hooks_dir()], capture_output=True, text=True, env=self.env(), cwd=self.tmp)
        self.assertEqual(out.stdout.strip(), self.custom, out.stderr)
        self.assertFalse(os.path.exists(self.local), 'nothing was made inside the kit')

    def test_the_default_folder_is_recorded_too(self):
        self.ok()
        self.assertEqual(read(os.path.join(self.hooks_dir(), '.local-dir')).strip(), self.local)
        self.assertIn('in sync', self.ok('--check').stdout)

    def test_a_later_sync_without_the_variable_uses_the_recorded_folder(self):
        self.ok(AGENT_CORE_LOCAL=self.custom)
        with open(os.path.join(self.custom, 'AGENTS.md'), 'a') as fh:
            fh.write('- Greet the reviewer.\n')
        out = self.ok('--sync')
        self.assertFalse(os.path.exists(self.local), 'no fresh <kit>/local')
        self.assertIn('Greet the reviewer', read(os.path.join(self.claude, 'CLAUDE.md')))
        self.assertEqual(read(os.path.join(self.hooks_dir(), '.local-dir')).strip(), self.custom)
        self.assertIn('in sync', self.ok('--check').stdout)
        self.assertNotIn('now point at', out.stdout)

    def test_uninstall_without_the_variable_uses_the_recorded_folder(self):
        self.ok(AGENT_CORE_LOCAL=self.custom)
        self.ok('--uninstall')
        self.assertFalse(os.path.exists(self.local))
        self.assertTrue([b for b in os.listdir(os.path.join(self.custom, 'backups')) if b.endswith('-uninstall')])
        self.assertEqual(self.settings()['permissions']['allow'], ['Bash(ls)'])

    def test_a_different_variable_on_a_later_sync_moves_the_record_and_says_so(self):
        self.ok(AGENT_CORE_LOCAL=self.custom)
        other = os.path.join(self.tmp, 'other-folder')
        out = self.ok('--sync', AGENT_CORE_LOCAL=other)
        self.assertIn('personal folder %s; they now point at %s' % (self.custom, other), out.stdout)
        self.assertEqual(read(os.path.join(self.hooks_dir(), '.local-dir')).strip(), other)


class UninstallRestores(Sandbox):
    def seed(self):
        super(UninstallRestores, self).seed()
        write(os.path.join(self.codex, 'AGENTS.md'), 'my own codex rules\n')

    def test_default_mode_and_ask_rules_come_back(self):
        s = self.settings()
        s['permissions']['defaultMode'] = 'plan'
        write(os.path.join(self.claude, 'settings.json'), json.dumps(s))
        self.configure(permission_mode='bypassPermissions')
        self.ok()
        perms = self.settings()['permissions']
        self.assertEqual((perms['defaultMode'], perms['ask']), ('bypassPermissions', []))
        out = self.ok('--uninstall')
        perms = self.settings()['permissions']
        self.assertEqual(perms['defaultMode'], 'plan')
        self.assertEqual(perms['ask'], ['Bash(rm *)'])
        self.assertIn('restored:', out.stdout)
        self.assertIn('Bash(rm *)', out.stdout)
        self.assertIn('permissions.defaultMode back to "plan"', out.stdout)

    def test_a_default_mode_the_user_did_not_have_is_removed(self):
        self.configure(permission_mode='acceptEdits')
        self.ok()
        self.assertEqual(self.settings()['permissions']['defaultMode'], 'acceptEdits')
        self.ok('--uninstall')
        self.assertNotIn('defaultMode', self.settings()['permissions'])

    def test_a_default_mode_changed_since_the_install_is_left_alone(self):
        self.configure(permission_mode='acceptEdits')
        self.ok()
        s = self.settings()
        s['permissions']['defaultMode'] = 'plan'
        write(os.path.join(self.claude, 'settings.json'), json.dumps(s))
        out = self.ok('--uninstall')
        self.assertEqual(self.settings()['permissions']['defaultMode'], 'plan')
        self.assertIn('permissions.defaultMode', out.stdout)
        self.assertIn('you changed', out.stdout)

    def test_the_home_matches_what_it_was_before_the_install(self):
        self.configure(permission_mode='bypassPermissions', codex={'models': {}, 'pin_memories_off': True},
                       auto_mode_environment=['Source control: GitHub org example-org is trusted.'], protected_dirs=['lake-data'])
        before = snapshot(self.home)
        before_settings = self.settings()
        before_hooks = json.loads(read(os.path.join(self.codex, 'hooks.json')))
        out = self.ok()
        self.assertNotEqual(snapshot(self.home), before)
        out = self.ok('--uninstall')
        after = snapshot(self.home)
        for rel in ('.claude/settings.json', '.codex/hooks.json'):  # rewritten as JSON, compared as JSON below
            before.pop(rel)
            after.pop(rel)
        self.assertEqual(after, before)
        self.assertEqual(self.settings(), before_settings)
        self.assertEqual(json.loads(read(os.path.join(self.codex, 'hooks.json'))), before_hooks)
        self.assertEqual(read(os.path.join(self.claude, 'CLAUDE.md')), 'my own global rules\n')
        self.assertEqual(read(os.path.join(self.codex, 'AGENTS.md')), 'my own codex rules\n')
        self.assertEqual(read(os.path.join(self.codex, 'config.toml')), 'model = "my-model"\n', 'the memories pin is undone')
        for what in ('CLAUDE.md', 'AGENTS.md', 'config.toml', 'model', 'cleanupPeriodDays'):
            self.assertIn(what, out.stdout)
        self.assertIn('restored:', out.stdout)

    def test_files_the_user_changed_since_the_install_are_not_overwritten(self):
        self.ok()
        s = self.settings()
        s['model'] = 'sonnet'
        write(os.path.join(self.claude, 'settings.json'), json.dumps(s))
        write(os.path.join(self.codex, 'AGENTS.md'), 'rewritten by hand, no kit header\n')
        out = self.ok('--uninstall')
        self.assertEqual(self.settings()['model'], 'sonnet')
        self.assertNotIn('cleanupPeriodDays', self.settings())
        self.assertEqual(read(os.path.join(self.codex, 'AGENTS.md')), 'rewritten by hand, no kit header\n')
        self.assertIn('left', out.stdout)
        self.assertEqual(read(os.path.join(self.claude, 'CLAUDE.md')), 'my own global rules\n')

    def test_a_clean_slate_install_gets_its_archived_files_back(self):
        write(os.path.join(self.claude, 'hooks', 'mine.sh'), '#!/bin/sh\n')
        s = self.settings()
        s['hooks']['Stop'] = [{'hooks': [{'type': 'command', 'command': 'sh ~/.claude/hooks/mine.sh'}]}]
        write(os.path.join(self.claude, 'settings.json'), json.dumps(s))
        before = snapshot(self.home)
        before_settings = self.settings()
        self.ok('--clean-slate')
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'hooks', 'mine.sh')))
        out = self.ok('--uninstall')
        after = snapshot(self.home)
        for rel in ('.claude/settings.json', '.codex/hooks.json'):
            before.pop(rel)
            after.pop(rel)
        self.assertEqual(after, before)
        self.assertEqual(self.settings(), before_settings, 'the dropped hook entry is back with its script')
        self.assertIn('restored:', out.stdout)
        self.assertIn('--clean-slate', out.stdout)


class Symlinks(Sandbox):
    def link(self, where, name, text=None):
        """Make ~/<where> a symlink to ~/dotfiles/<name> holding text (default: what the file held)."""
        link = os.path.join(self.home, where)
        target = os.path.join(self.home, 'dotfiles', name)
        write(target, read(link) if text is None else text)
        if os.path.lexists(link):
            os.remove(link)
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(target, link)
        return link, target

    def assertLinkTo(self, link, target):
        self.assertTrue(os.path.islink(link), link)
        self.assertEqual(os.readlink(link), target)

    def test_a_symlinked_claude_md_and_agents_md_get_the_original_text_back_through_the_link(self):
        claude_link, claude_target = self.link('.claude/CLAUDE.md', 'CLAUDE.md', 'my dotfile rules\n')
        codex_link, codex_target = self.link('.codex/AGENTS.md', 'AGENTS.md', 'my codex dotfile rules\n')
        self.ok()
        self.assertLinkTo(claude_link, claude_target)
        self.assertIn('Generated by', read(claude_target))
        (first,) = self.backups()
        bk = os.path.join(self.local, 'backups', first)
        self.assertEqual(read(os.path.join(bk, 'claude', 'CLAUDE.md')), 'my dotfile rules\n')
        links = json.loads(read(os.path.join(bk, 'LINKS.json')))
        self.assertEqual(links['claude/CLAUDE.md']['target'], claude_target)
        self.assertEqual(links['codex/AGENTS.md']['target'], codex_target)
        out = self.ok('--uninstall')
        self.assertLinkTo(claude_link, claude_target)
        self.assertLinkTo(codex_link, codex_target)
        self.assertEqual(read(claude_target), 'my dotfile rules\n')
        self.assertEqual(read(codex_target), 'my codex dotfile rules\n')
        self.assertIn('symlink', out.stdout)
        (down,) = [b for b in self.backups() if b.endswith('-uninstall')]
        self.assertIn('Generated by', read(os.path.join(self.local, 'backups', down, 'claude', 'CLAUDE.md')))

    def test_a_symlinked_settings_and_hooks_json_are_written_through_and_stay_links(self):
        settings_link, settings_target = self.link('.claude/settings.json', 'settings.json')
        hooks_link, hooks_target = self.link('.codex/hooks.json', 'hooks.json')
        toml_link, toml_target = self.link('.codex/config.toml', 'config.toml')
        self.configure(codex={'models': {}, 'pin_memories_off': True})
        original = json.loads(read(settings_target))
        original_hooks = json.loads(read(hooks_target))
        original_toml = read(toml_target)
        self.ok()
        self.assertLinkTo(settings_link, settings_target)
        self.assertIn(MARK, read(settings_target))
        self.assertIn(MARK, read(hooks_target))
        self.assertIn('memories = false', read(toml_target))
        links = json.loads(read(os.path.join(self.local, 'backups', self.backups()[0], 'LINKS.json')))
        self.assertEqual(links['claude/settings.json']['target'], settings_target)
        self.assertEqual(links['codex/config.toml']['target'], toml_target)
        self.ok('--uninstall')
        self.assertLinkTo(settings_link, settings_target)
        self.assertLinkTo(hooks_link, hooks_target)
        self.assertLinkTo(toml_link, toml_target)
        self.assertEqual(json.loads(read(settings_target)), original)
        self.assertEqual(json.loads(read(hooks_target)), original_hooks)
        self.assertEqual(read(toml_target), original_toml)

    def test_restore_writes_the_original_back_through_the_link(self):
        link, target = self.link('.claude/CLAUDE.md', 'CLAUDE.md', 'my dotfile rules\n')
        self.ok()
        self.assertIn('Generated by', read(target))
        out = self.ok('--restore', self.backups()[0])
        self.assertLinkTo(link, target)
        self.assertEqual(read(target), 'my dotfile rules\n')
        self.assertIn('symlink', out.stdout)
        (before,) = [b for b in self.backups() if b.endswith('-before-restore')]
        self.assertIn('Generated by', read(os.path.join(self.local, 'backups', before, 'claude', 'CLAUDE.md')))

    def test_a_restore_after_an_uninstall_keeps_the_links_too(self):
        link, target = self.link('.claude/CLAUDE.md', 'CLAUDE.md', 'my dotfile rules\n')
        self.ok()
        self.ok('--uninstall')
        (down,) = [b for b in self.backups() if b.endswith('-uninstall')]
        self.ok('--restore', down)
        self.assertLinkTo(link, target)
        self.assertIn('Generated by', read(target))
        self.assertIn('in sync', self.ok('--check').stdout)


class KeepTheUsersSettings(Sandbox):
    def put(self, **keys):
        s = self.settings()
        s.update(keys)
        write(os.path.join(self.claude, 'settings.json'), json.dumps(s))

    def test_model_env_and_cleanup_period_of_the_user_survive_the_first_install(self):
        self.put(model='sonnet', env={'MCP_TIMEOUT': '5', 'MINE': '1'}, cleanupPeriodDays=365)
        self.ok()
        s = self.settings()
        self.assertEqual(s['model'], 'sonnet')
        self.assertEqual(s['env'], {'MCP_TIMEOUT': '5', 'MINE': '1', 'CLAUDE_CODE_SUBAGENT_MODEL': 'sonnet'})
        self.assertEqual(s['cleanupPeriodDays'], 365, 'never lowered')

    def test_cleanup_period_is_raised_to_ninety_and_no_further(self):
        self.ok()
        self.assertEqual(self.settings()['cleanupPeriodDays'], 90)
        self.put(cleanupPeriodDays=30)
        self.ok('--sync')
        self.assertEqual(self.settings()['cleanupPeriodDays'], 90)
        self.put(cleanupPeriodDays=36500)
        self.ok('--sync')
        self.assertEqual(self.settings()['cleanupPeriodDays'], 36500)

    def hook_commands(self, s):
        return sorted(h['command'] for es in s['hooks'].values() for e in es for h in e['hooks'])

    def seed_hooks(self):
        s = self.settings()
        for event, cmd in (('Stop', 'sh ~/.claude/hooks/gone.sh'), ('PostToolUse', '"$CLAUDE_PROJECT_DIR"/.claude/hooks/p.sh'),
                           ('SubagentStop', '$CLAUDE_PROJECT_DIR/.claude/hooks/q.sh'),
                           ('Notification', 'sh $HOME/.claude/hooks/mine.sh')):
            s['hooks'][event] = [{'hooks': [{'type': 'command', 'command': cmd}]}]
        write(os.path.join(self.claude, 'settings.json'), json.dumps(s))
        write(os.path.join(self.claude, 'hooks', 'mine.sh'), '#!/bin/sh\n')
        return [e['hooks'][0]['command'] for es in s['hooks'].values() for e in es]

    def test_a_plain_install_never_drops_a_user_hook_entry(self):
        mine = self.seed_hooks()
        out = self.ok()
        have = self.hook_commands(self.settings())
        for cmd in mine:
            self.assertIn(cmd, have)
        self.assertNotIn('dropped', out.stdout)
        self.ok('--sync')
        for cmd in mine:
            self.assertIn(cmd, self.hook_commands(self.settings()))

    def test_clean_slate_drops_only_entries_whose_scripts_it_archived_and_says_so(self):
        mine = self.seed_hooks()
        out = self.ok('--clean-slate')
        have = self.hook_commands(self.settings())
        self.assertNotIn('sh $HOME/.claude/hooks/mine.sh', have)
        for cmd in mine:
            if 'mine.sh' not in cmd:
                self.assertIn(cmd, have, 'a path it cannot resolve, or a script it did not move, stays')
        line = [l for l in out.stdout.splitlines() if 'dropped' in l]
        self.assertEqual(len(line), 1, out.stdout)
        self.assertIn('dropped 1 of your hooks', line[0])
        self.assertIn('mine.sh', line[0])
        self.assertNotIn('gone.sh', line[0])
        self.assertTrue(os.path.isfile(os.path.join(self.local, 'backups', self.backups()[0], 'claude', 'hooks', 'mine.sh')))


class AgentsAreTracked(Sandbox):
    def seed(self):
        super(AgentsAreTracked, self).seed()
        write(os.path.join(self.codex, 'agents', 'worker.toml'), 'name = "worker"\n# mine\n')

    def test_a_same_named_agent_is_left_alone_with_a_message_and_the_rest_install_beside_it(self):
        out = self.ok()
        mine = '---\nname: worker\n---\nmy own worker\n'
        self.assertEqual(read(os.path.join(self.claude, 'agents', 'worker.md')), mine)
        self.assertIn('worker.md exists in', out.stdout)
        self.assertIn('Rename one of them', out.stdout)
        self.assertEqual(read(os.path.join(self.codex, 'agents', 'worker.toml')), 'name = "worker"\n# mine\n')
        self.assertIn('worker.toml exists in', out.stdout)
        for rel in (('claude', 'agents', 'reviewer.md'), ('claude', 'agents', 'Explore.md'), ('codex', 'agents', 'reviewer.toml'),
                    ('codex', 'agents', 'explorer.toml')):
            self.assertTrue(os.path.isfile(os.path.join(self.home, '.' + rel[0], *rel[1:])), rel)
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'agents', '.agent-core-manifest.json')),
                         'Codex loads its agents folder, so its manifest lives in local/')
        for dest, manifest in ((os.path.join(self.claude, 'agents'), os.path.join(self.claude, 'agents', '.agent-core-manifest.json')),
                               (os.path.join(self.codex, 'agents'), os.path.join(self.local, 'codex-agents-manifest.json'))):
            managed = json.loads(read(manifest))['managed']
            self.assertNotIn('worker.md' if 'claude' in dest else 'worker.toml', managed)
            self.assertIn('reviewer.md' if 'claude' in dest else 'reviewer.toml', managed)
        self.assertEqual(os.path.exists(os.path.join(self.local, 'backups', self.backups()[0], 'claude', 'agents')), False)
        self.assertIn('in sync', self.ok('--check').stdout, 'an agent that is not the kit\'s is not drift')
        self.ok('--sync')
        self.assertEqual(read(os.path.join(self.claude, 'agents', 'worker.md')), mine)

    def test_uninstall_takes_only_the_agents_the_kit_installed(self):
        self.ok()
        self.ok('--uninstall')
        self.assertIn('my own worker', read(os.path.join(self.claude, 'agents', 'worker.md')))
        self.assertTrue(os.path.isfile(os.path.join(self.codex, 'agents', 'worker.toml')))
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'agents', 'reviewer.md')))
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'agents', 'reviewer.toml')))
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'agents', '.agent-core-manifest.json')))
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'agents', '.agent-core-manifest.json')))

    def test_an_agent_the_kit_installed_before_manifests_existed_is_still_the_kits(self):
        self.ok()
        os.remove(os.path.join(self.claude, 'agents', '.agent-core-manifest.json'))
        write(os.path.join(self.claude, 'agents', 'reviewer.md'), 'edited by hand\n')
        out = self.ok('--sync')
        self.assertNotIn('reviewer.md exists', out.stdout)
        self.assertNotEqual(read(os.path.join(self.claude, 'agents', 'reviewer.md')), 'edited by hand\n')
        self.assertIn('reviewer.md', json.loads(read(os.path.join(self.claude, 'agents', '.agent-core-manifest.json')))['managed'])

    def test_clean_slate_still_archives_the_old_agents_and_installs_the_kits(self):
        self.ok('--clean-slate')
        self.assertNotIn('my own worker', read(os.path.join(self.claude, 'agents', 'worker.md')))
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'agents', 'my-agent.md')))
        bk = os.path.join(self.local, 'backups', self.backups()[0])
        self.assertIn('my own worker', read(os.path.join(bk, 'claude', 'agents', 'worker.md')))


class FileModes(Sandbox):
    def mode(self, *parts):
        return os.stat(os.path.join(self.home, *parts)).st_mode & 0o777

    def test_existing_modes_survive_every_rewrite(self):
        os.chmod(os.path.join(self.claude, 'settings.json'), 0o640)
        os.chmod(os.path.join(self.codex, 'hooks.json'), 0o600)
        os.chmod(os.path.join(self.codex, 'config.toml'), 0o640)
        self.configure(codex={'models': {}, 'pin_memories_off': True})
        self.ok()
        self.assertIn('memories = false', read(os.path.join(self.codex, 'config.toml')))
        self.assertEqual((self.mode('.claude', 'settings.json'), self.mode('.codex', 'hooks.json'),
                          self.mode('.codex', 'config.toml')), (0o640, 0o600, 0o640))
        self.configure(permission_mode='acceptEdits', codex={'models': {}, 'pin_memories_off': True})
        self.ok('--sync')
        self.assertEqual(self.mode('.claude', 'settings.json'), 0o640)
        self.ok('--uninstall')
        self.assertEqual((self.mode('.claude', 'settings.json'), self.mode('.codex', 'hooks.json'),
                          self.mode('.codex', 'config.toml')), (0o640, 0o600, 0o640))

    def test_new_files_are_private_where_they_hold_settings(self):
        for rel in (('.claude', 'settings.json'), ('.codex', 'hooks.json'), ('.codex', 'config.toml')):
            os.remove(os.path.join(self.home, *rel))
        self.configure(codex={'models': {}, 'pin_memories_off': True})
        self.ok()
        self.assertEqual((self.mode('.claude', 'settings.json'), self.mode('.codex', 'config.toml'),
                          self.mode('.codex', 'hooks.json')), (0o600, 0o600, 0o644))
        self.ok('--uninstall')
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'config.toml')), 'the kit made it, so it goes into the backup')
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'hooks.json')))
        self.assertFalse(os.path.exists(os.path.join(self.claude, 'settings.json')))


class SettingsTemplate(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(CORE, 'claude', 'settings.json')) as fh:
            self.template = json.load(fh)

    def test_the_guard_hook_covers_every_sql_tool_the_guard_handles_and_has_time_for_gh(self):
        import re
        sys.path.insert(0, os.path.join(CORE, 'claude', 'hooks'))
        import guard
        (entry,) = [e for e in self.template['hooks']['PreToolUse'] if 'guard.py' in json.dumps(e)]
        for name in ('execute_sql', 'apply_migration', 'd1_database_query', 'run_sql', 'sql_query'):
            tool = 'mcp__some_server__' + name
            self.assertTrue(re.search(entry['matcher'], tool), tool)
            self.assertTrue(guard.SQL_MCP.match(tool), tool)
        for tool in ('Bash', 'Agent', 'Task'):
            self.assertTrue(re.fullmatch(entry['matcher'], tool), tool)
        self.assertFalse(re.search(entry['matcher'], 'mcp__some_server__list_tables'))
        self.assertEqual(entry['hooks'][0]['timeout'], 30)

    def test_other_hooks_keep_their_timeouts_and_cleanup_floor_is_ninety(self):
        timeouts = dict((h['command'].split('/')[-1].split()[0], h['timeout'])
                        for es in self.template['hooks'].values() for e in es for h in e['hooks'])
        self.assertEqual(timeouts, {'guard.py': 30, 'budget.py': 10, 'capture.py': 10, 'session_start.py': 15, 'stop_steps.py': 10})
        self.assertEqual(self.template['cleanupPeriodDays'], 90)


if __name__ == '__main__':
    unittest.main()
