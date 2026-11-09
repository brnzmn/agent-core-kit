"""A git pre-commit hook in every repo under projects_root keeps CLAUDE.md and AGENTS.md short and log-free."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN, CORE, HOOKS, make_local  # noqa: E402

RULES = ''.join('- Rule %d.\n' % i for i in range(1, 101))


class GitCase(unittest.TestCase):
    """Throwaway HOME, projects folder, local/ folder and helpers; no tests of its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.tmp.name)
        self.home = os.path.join(self.base, 'home')
        os.makedirs(self.home)
        self.projects = os.path.join(self.base, 'Projects')
        os.makedirs(self.projects)
        self.local = make_local({'projects_root': self.projects})
        self.addCleanup(shutil.rmtree, self.local, True)
        self.env = dict((k, v) for k, v in os.environ.items() if not k.startswith(('AGENT_CORE', 'GIT_')))
        self.env.update(HOME=self.home, AGENT_CORE_DIR=CORE, AGENT_CORE_LOCAL=self.local, GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, repo, *args):
        return subprocess.run(['git', '-C', repo] + list(args), capture_output=True, text=True, env=self.env)

    def repo(self, name):
        path = os.path.join(self.projects, name)
        subprocess.run(['git', 'init', '-q', '-b', 'main', path], check=True, env=self.env)
        self.write(path, 'README.md', 'x\n')
        self.git(path, 'add', 'README.md')
        self.git(path, 'commit', '-q', '-m', 'init')
        return path

    def write(self, repo, rel, text, mode=None):
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write(text)
        if mode:
            os.chmod(path, mode)

    @staticmethod
    def read(path):
        with open(path) as fh:
            return fh.read()

    def hooks(self, *args, **kw):
        env = dict(self.env, **kw.get('env', {}))
        extra = [] if kw.get('no_root') else ['--root', self.projects]
        return subprocess.run([sys.executable, os.path.join(BIN, 'git_hooks.py')] + list(args) + extra,
                              capture_output=True, text=True, env=env)

    def commit(self, repo, rel, text):
        self.write(repo, rel, text)
        self.git(repo, 'add', rel)
        return self.git(repo, 'commit', '-q', '-m', 'change ' + rel)


class Repos(GitCase):
    def test_rejects_long_and_log_shaped_instruction_files(self):
        repo = self.repo('app')
        self.assertEqual(self.hooks('install').returncode, 0)
        self.assertEqual(self.commit(repo, 'AGENTS.md', RULES).returncode, 0)
        r = self.commit(repo, 'AGENTS.md', RULES + ''.join('- More %d.\n' % i for i in range(60)))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('AGENTS.md would be 160 lines (limit 150', r.stderr)
        self.git(repo, 'reset', '-q', 'AGENTS.md')
        r = self.commit(repo, 'packages/web/CLAUDE.md', '# Rules\n\n## Status\n- 2026-10-19 shipped the login page\n')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('docs/build-log.md', r.stderr)
        self.git(repo, 'reset', '-q', 'packages/web/CLAUDE.md')
        self.assertEqual(self.commit(repo, 'docs/build-log.md', '## Status\n- 2026-10-19 shipped x\n- '
                                     '2026-10-19 fixed y\n').returncode, 0, 'logs outside instruction files pass')

    def test_a_claude_md_that_only_imports_agents_md_has_the_smaller_budget(self):
        repo = self.repo('imports')
        self.hooks('install')
        body = '@AGENTS.md\n' + ''.join('- Extra %d.\n' % i for i in range(45))
        r = self.commit(repo, 'CLAUDE.md', body)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('limit 40', r.stderr)

    def test_over_budget_file_may_shrink(self):
        repo = self.repo('big')
        self.write(repo, 'CLAUDE.md', ''.join('- Rule %d.\n' % i for i in range(300)))
        self.git(repo, 'add', 'CLAUDE.md')
        self.git(repo, 'commit', '-q', '-m', 'before the hook')
        self.hooks('install')
        self.assertEqual(self.commit(repo, 'CLAUDE.md', ''.join('- Rule %d.\n' % i for i in range(200))).returncode, 0)
        self.assertNotEqual(self.commit(repo, 'CLAUDE.md', ''.join('- Rule %d.\n' % i for i in range(201))).returncode, 0)

    def test_existing_hook_is_chained_and_restored(self):
        repo = self.repo('chained')
        hook = os.path.join(repo, '.git', 'hooks', 'pre-commit')
        self.write(repo, '.git/hooks/pre-commit', '#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/.ran"\n', 0o755)
        self.hooks('install')
        self.assertEqual(self.commit(repo, 'a.txt', 'a\n').returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(repo, '.ran')))
        self.assertEqual(self.hooks('install').returncode, 0, 'install is idempotent')
        self.assertIn('touch', self.read(os.path.join(repo, '.git', 'hooks', 'pre-commit.agent-core-orig')))
        self.hooks('uninstall')
        self.assertIn('touch', self.read(hook))

    def test_tracked_hooks_path_is_wrapped_not_edited(self):
        repo = self.repo('tracked')
        self.write(repo, '.githooks/pre-commit', '#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/.guard"\n', 0o755)
        self.write(repo, '.githooks/pre-push', '#!/bin/sh\nexit 0\n', 0o755)
        self.git(repo, 'add', '.githooks')
        self.git(repo, 'commit', '-q', '-m', 'hooks')
        self.git(repo, 'config', 'core.hooksPath', '.githooks')
        self.assertEqual(self.hooks('check').returncode, 1)
        self.hooks('install')
        self.assertEqual(self.hooks('check').returncode, 0, self.hooks('check').stdout)
        self.assertEqual(self.git(repo, 'config', 'agentcore.origHooksPath').stdout.strip(), '.githooks')
        self.assertTrue(os.path.exists(os.path.join(repo, '.git', 'agent-core-hooks', 'pre-push')))
        self.assertNotEqual(self.commit(repo, 'AGENTS.md', RULES * 2).returncode, 0)
        self.git(repo, 'reset', '-q', 'AGENTS.md')
        self.assertEqual(self.commit(repo, 'b.txt', 'b\n').returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(repo, '.guard')), 'the repo guard still runs')
        self.assertEqual(self.git(repo, 'status', '--porcelain', '.githooks').stdout, '')
        self.hooks('uninstall')
        self.assertEqual(self.git(repo, 'config', 'core.hooksPath').stdout.strip(), '.githooks')

    def test_worktrees_share_the_hook_and_check_reports_gaps(self):
        repo = self.repo('main-repo')
        wt = os.path.join(self.projects, 'main-repo-wt')
        self.git(repo, 'worktree', 'add', '-q', wt, '-b', 'feature')
        self.repo('other')
        self.assertEqual(self.hooks('check').returncode, 1)
        out = self.hooks('install').stdout
        self.assertIn('2 repos', out, 'the worktree and its main repo count once')
        self.assertNotEqual(self.commit(wt, 'AGENTS.md', RULES * 2).returncode, 0)
        self.assertEqual(self.hooks('check').returncode, 0)

    def test_the_hook_finds_the_kit_through_core_dir_when_the_environment_has_no_path(self):
        repo = self.repo('lookup')
        self.hooks('install')
        env = dict(self.env)
        del env['AGENT_CORE_DIR']
        core_dir = os.path.join(self.home, '.claude', 'hooks', 'agent-core', '.core-dir')
        os.makedirs(os.path.dirname(core_dir))
        with open(core_dir, 'w') as fh:
            fh.write(CORE + '\n')
        self.env = env
        self.assertNotEqual(self.commit(repo, 'AGENTS.md', RULES * 2).returncode, 0)

    def test_a_missing_kit_never_blocks_a_commit(self):
        repo = self.repo('gone')
        self.hooks('install')
        hook = os.path.join(repo, '.git', 'hooks', 'pre-commit')
        text = self.read(hook)
        with open(hook, 'w') as fh:
            fh.write(text.replace(CORE, os.path.join(self.base, 'moved-away')))
        self.env.pop('AGENT_CORE_DIR')
        self.assertEqual(self.commit(repo, 'AGENTS.md', RULES * 2).returncode, 0)

    def test_hook_text_has_no_home_specific_path(self):
        repo = self.repo('text')
        self.hooks('install')
        text = self.read(os.path.join(repo, '.git', 'hooks', 'pre-commit'))
        self.assertNotIn('$HOME/agent-core', text)
        self.assertIn('precommit.py', text)


class ProjectsRoot(GitCase):
    def test_cli_reads_the_root_from_config(self):
        repo = self.repo('configured')
        out = self.hooks('install', no_root=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('1 repos', out.stdout)
        self.assertEqual(self.hooks('check', no_root=True).returncode, 0)
        self.assertNotEqual(self.commit(repo, 'AGENTS.md', RULES * 2).returncode, 0)

    def test_env_overrides_the_config_root(self):
        other = os.path.join(self.base, 'Other')
        os.makedirs(other)
        subprocess.run(['git', 'init', '-q', os.path.join(other, 'r')], check=True, env=self.env)
        self.repo('mine')
        out = self.hooks('check', no_root=True, env={'AGENT_CORE_PROJECTS': other})
        self.assertIn(os.path.join(other, 'r'), out.stdout)
        self.assertNotIn('mine', out.stdout)

    def test_no_root_says_so_and_exits_zero(self):
        with open(os.path.join(self.local, 'config.json'), 'w') as fh:
            json.dump({'projects_root': ''}, fh)
        for action in ('install', 'check', 'uninstall'):
            out = self.hooks(action, no_root=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('projects_root is not set', out.stdout)

    def test_projects_root_function(self):
        out = subprocess.run([sys.executable, '-c', 'import git_hooks; print(repr(git_hooks.projects_root()))'],
                             capture_output=True, text=True, env=dict(self.env, PYTHONPATH=BIN))
        self.assertEqual(out.stdout.strip(), repr(self.projects), out.stderr)
        with open(os.path.join(self.local, 'config.json'), 'w') as fh:
            json.dump({'projects_root': '~/Code'}, fh)
        out = subprocess.run([sys.executable, '-c', 'import git_hooks; print(git_hooks.projects_root())'],
                             capture_output=True, text=True, env=dict(self.env, PYTHONPATH=BIN))
        self.assertEqual(out.stdout.strip(), os.path.join(self.home, 'Code'), out.stderr)
        with open(os.path.join(self.local, 'config.json'), 'w') as fh:
            json.dump({}, fh)
        out = subprocess.run([sys.executable, '-c', 'import git_hooks; print(repr(git_hooks.projects_root()))'],
                             capture_output=True, text=True, env=dict(self.env, PYTHONPATH=BIN))
        self.assertEqual(out.stdout.strip(), "''", out.stderr)

    def test_install_and_check_functions(self):
        repo = self.repo('api')
        script = ('import sys, git_hooks\nrepo = sys.argv[1]\nprint(git_hooks.check(repo)[0])\n'
                  'print(git_hooks.install(repo))\nprint(git_hooks.check(repo)[0])\n')
        out = subprocess.run([sys.executable, '-c', script, repo], capture_output=True, text=True,
                             env=dict(self.env, PYTHONPATH=BIN))
        lines = out.stdout.split('\n')
        self.assertEqual((lines[0], lines[1], lines[2]), ('False', 'installed', 'True'), out.stdout + out.stderr)

    def test_session_start_installs_the_hook_in_a_new_clone(self):
        repo = self.repo('fresh-clone')
        out = subprocess.run([sys.executable, os.path.join(HOOKS, 'session_start.py')], input=json.dumps({'cwd': repo}),
                             capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.hooks('check').returncode, 0)
        self.assertNotEqual(self.commit(repo, 'AGENTS.md', RULES * 2).returncode, 0)

    def test_session_start_leaves_repos_outside_the_root_alone(self):
        outside = os.path.join(self.base, 'elsewhere')
        subprocess.run(['git', 'init', '-q', '-b', 'main', outside], check=True, env=self.env)
        subprocess.run([sys.executable, os.path.join(HOOKS, 'session_start.py')], input=json.dumps({'cwd': outside}),
                       capture_output=True, text=True, env=self.env, timeout=20)
        self.assertFalse(os.path.exists(os.path.join(outside, '.git', 'hooks', 'pre-commit')))


PRE_COMMIT_FRAMEWORK = ('#!/usr/bin/env python3\n# File generated by pre-commit: https://pre-commit.com\n'
                        '# ID: 138fd403232d2ddd5efb44317e38bf03\nimport sys\nsys.exit(0)\n')
LEFTHOOK = ('#!/bin/sh\nif [ "$LEFTHOOK" = "0" ]; then\n  exit 0\nfi\ncall_lefthook() {\n  lefthook "$@"\n}\n'
            'call_lefthook run "pre-commit" "$@"\n')
HUSKY_V4 = '#!/bin/sh\n# husky\n. "$(dirname "$0")/husky.sh"\n'
OWN_HOOK = '#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/.own-ran"\n'


class OtherPeoplesHooks(GitCase):
    """A saved original is never overwritten, hook managers are left alone, and the session-start path never takes over."""

    def hook(self, repo):
        return os.path.join(repo, '.git', 'hooks', 'pre-commit')

    def test_a_replacement_is_chained_and_the_older_hook_is_preserved(self):
        repo = self.repo('twice')
        self.write(repo, '.git/hooks/pre-commit', '#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/.ran-a"\n', 0o755)
        self.hooks('install')
        orig = os.path.join(repo, '.git', 'hooks', 'pre-commit.agent-core-orig')
        first = self.read(orig)
        self.assertIn('.ran-a', first)
        second = '#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/.ran-b"\n'
        self.write(repo, '.git/hooks/pre-commit', second, 0o755)  # something replaced ours
        out = self.hooks('install', '-q')
        self.assertEqual(self.read(orig), second, 'the latest user hook is the one chained')
        saved = [n for n in os.listdir(os.path.dirname(orig)) if n.startswith('pre-commit.agent-core-orig.')]
        self.assertEqual(len(saved), 1, saved)
        self.assertEqual(self.read(os.path.join(os.path.dirname(orig), saved[0])), first)
        self.assertIn(saved[0], out.stdout)
        self.assertIn('moved to hooks/', out.stdout, 'the archive notice survives quiet mode')
        self.assertIn('agent-core pre-commit', self.read(self.hook(repo)))
        self.assertEqual(self.commit(repo, 'a.txt', 'a\n').returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(repo, '.ran-b')), 'the latest hook runs')
        self.assertFalse(os.path.exists(os.path.join(repo, '.ran-a')), 'the older hook is retained as an archive')
        out = self.hooks('uninstall')
        self.assertEqual(self.read(self.hook(repo)), second)
        self.assertIn(saved[0], out.stdout, 'uninstall says what else it kept')

    def test_hooks_made_by_the_pre_commit_framework_husky_and_lefthook_are_left_alone(self):
        repos = {}
        for name, text in (('framework', PRE_COMMIT_FRAMEWORK), ('lefthook', LEFTHOOK), ('husky4', HUSKY_V4)):
            repos[name] = self.repo(name)
            self.write(repos[name], '.git/hooks/pre-commit', text, 0o755)
        repos['husky9'] = self.repo('husky9')
        self.write(repos['husky9'], '.husky/_/pre-commit', '#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n', 0o755)
        self.write(repos['husky9'], '.husky/_/h', '#!/usr/bin/env sh\nexit 0\n', 0o755)
        self.git(repos['husky9'], 'config', 'core.hooksPath', '.husky/_')
        out = self.hooks('install', '-q')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        for name, tool in (('framework', 'pre-commit framework'), ('lefthook', 'lefthook'), ('husky4', 'husky'), ('husky9', 'husky')):
            (line,) = [l for l in out.stdout.splitlines() if repos[name] in l]
            self.assertIn('skipped', line)
            self.assertIn('instruction-file check is not installed', line)
            self.assertIn(tool, line)
        self.assertIn('4 skipped', out.stdout)
        for name, text in (('framework', PRE_COMMIT_FRAMEWORK), ('lefthook', LEFTHOOK), ('husky4', HUSKY_V4)):
            self.assertEqual(self.read(self.hook(repos[name])), text)
            self.assertFalse(os.path.exists(self.hook(repos[name]) + '.agent-core-orig'))
        self.assertEqual(self.git(repos['husky9'], 'config', 'core.hooksPath').stdout.strip(), '.husky/_')
        self.assertFalse(os.path.exists(os.path.join(repos['husky9'], '.git', 'agent-core-hooks')))
        check = self.hooks('check')
        self.assertEqual(check.returncode, 0, check.stdout, )
        self.assertNotIn('missing', check.stdout)
        self.assertIn('skipped', check.stdout)

    def test_check_function_says_skipped_not_missing(self):
        repo = self.repo('fw')
        self.write(repo, '.git/hooks/pre-commit', PRE_COMMIT_FRAMEWORK, 0o755)
        plain = self.repo('plain')
        script = ('import sys, git_hooks\nfor r in sys.argv[1:]:\n    print(git_hooks.check(r))\n')
        out = subprocess.run([sys.executable, '-c', script, repo, plain], capture_output=True, text=True,
                             env=dict(self.env, PYTHONPATH=BIN))
        first, second = out.stdout.strip().splitlines()
        self.assertTrue(first.startswith("(True, 'skipped"), out.stdout + out.stderr)
        self.assertEqual(second, "(False, 'missing')")

    def test_session_start_never_takes_over_a_hook_it_did_not_write(self):
        own, fw, tracked = self.repo('own'), self.repo('fw'), self.repo('tracked')
        self.write(own, '.git/hooks/pre-commit', OWN_HOOK, 0o755)
        self.write(fw, '.git/hooks/pre-commit', PRE_COMMIT_FRAMEWORK, 0o755)
        self.write(tracked, '.githooks/pre-commit', OWN_HOOK, 0o755)
        self.git(tracked, 'config', 'core.hooksPath', '.githooks')
        for repo in (own, fw, tracked, own, fw):  # the second round is a later session
            out = subprocess.run([sys.executable, os.path.join(HOOKS, 'session_start.py')], input=json.dumps({'cwd': repo}),
                                 capture_output=True, text=True, env=self.env, timeout=20)
            self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.read(self.hook(own)), OWN_HOOK)
        self.assertFalse(os.path.exists(self.hook(own) + '.agent-core-orig'))
        self.assertEqual(self.read(self.hook(fw)), PRE_COMMIT_FRAMEWORK)
        self.assertEqual(self.git(tracked, 'config', 'core.hooksPath').stdout.strip(), '.githooks')
        self.assertFalse(os.path.exists(os.path.join(tracked, '.git', 'agent-core-hooks')))
        self.assertEqual(self.hooks('check').returncode, 1, 'the repos with hooks of the user\'s own still show as not covered')
        # the command line, which the user runs on purpose, does chain the hook of their own
        self.hooks('install')
        self.assertIn('agent-core pre-commit', self.read(self.hook(own)))
        self.assertEqual(self.read(self.hook(own) + '.agent-core-orig'), OWN_HOOK)
        self.assertEqual(self.read(self.hook(fw)), PRE_COMMIT_FRAMEWORK)


if __name__ == '__main__':
    unittest.main()
