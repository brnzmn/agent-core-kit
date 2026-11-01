"""SessionStart: repo context (deploy targets, accounts, decisions) and the once-a-day notices."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN, CORE, HOOKS, TEST_CONFIG, env_for, make_local  # noqa: E402

sys.path.insert(0, HOOKS)
import repo_context  # noqa: E402

# Runs session_start in a process that believes it is on macOS (the iCloud check only fires there) and, when
# STUB_GIT_HOOKS is set, with bin/git_hooks.py replaced by a stub that records what the hook asks of it.
WRAPPER = '''
import json, os, sys, types
sys.path.insert(0, %(hooks)r)
import common
common.sys = types.SimpleNamespace(platform='darwin')
if os.environ.get('STUB_GIT_HOOKS'):
    stub = types.ModuleType('git_hooks')
    stub.projects_root = lambda: os.environ['STUB_GIT_HOOKS'] if os.environ['STUB_GIT_HOOKS'] != '-' else ''
    def check(root):
        if os.environ.get('STUB_RAISE'):
            raise RuntimeError('boom')
        return (os.environ.get('STUB_INSTALLED') == '1', 'stub')
    def install(root):
        with open(os.environ['STUB_LOG'], 'a') as fh:
            fh.write(root + '\\n')
    stub.check, stub.install = check, install
    sys.modules['git_hooks'] = stub
import session_start
session_start.main()
''' % {'hooks': HOOKS}


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        fh.write(text)


def git_repo(root, remote=None):
    subprocess.run(['git', 'init', '-q', root], check=True)
    if remote:
        subprocess.run(['git', '-C', root, 'remote', 'add', 'origin', remote], check=True)


def local_with_rulebook(config=None):
    """A throwaway LOCAL that has a rulebook, so no 'not set up' notice."""
    local = make_local(config)
    if not os.path.isfile(os.path.join(local, 'AGENTS.md')):
        write(local, 'AGENTS.md', '# Rules\n')
    return local


def session_start(cwd, local=None, home=None, wrapper=False, proc_cwd=None, **env):
    """stdout of the hook. HOME points at an empty folder so the machine's own ~/.claude/CLAUDE.md never counts."""
    env = dict(env_for(local, HOME=home or tempfile.mkdtemp(prefix='akit-home-')), **env)
    cmd = [sys.executable, '-c', WRAPPER] if wrapper else [sys.executable, os.path.join(HOOKS, 'session_start.py')]
    out = subprocess.run(cmd, input=json.dumps({'cwd': cwd}), capture_output=True, text=True, env=env, timeout=20,
                         cwd=proc_cwd)
    assert out.returncode == 0 and not out.stderr, out.stderr
    return out.stdout


class RepoContext(unittest.TestCase):
    def test_full_repo(self):
        with tempfile.TemporaryDirectory() as root:
            git_repo(root, 'git@github.com:acme/shop.git')
            write(root, 'scripts/deploy', '#!/usr/bin/env bash\n# Deploy. Usage: scripts/deploy preview|prod\n'
                                          'rsync -a . root@vps.example.com:/srv/shop\n')
            write(root, '.vercel/project.json', '{"projectName":"shop","orgId":"team_abc"}')
            write(root, 'supabase/.temp/project-ref', 'abcdefgh\n')
            write(root, 'wrangler.toml', 'name = "shop-edge"\naccount_id = "cf123"\n')
            write(root, 'docs/decisions.md', '# Decisions\n\nAppend-only.\n\n' +
                  ''.join('- **2026-10-%02d (Alex).** Decision %d %s\n' % (i, i, 'x' * 300 * (i == 7))
                          for i in range(1, 8)))
            out = '\n'.join(repo_context.lines(root))
            self.assertIn('scripts/deploy preview|prod', out)
            self.assertIn('Vercel project shop', out)
            self.assertIn('Cloudflare worker shop-edge', out)
            self.assertIn('GitHub acme/shop', out)
            for acct in ('Vercel team team_abc', 'Supabase project abcdefgh', 'Cloudflare account cf123',
                         'ssh root@vps.example.com'):
                self.assertIn(acct, out)
            self.assertIn('Last 5 decisions in docs/decisions.md', out)
            self.assertNotIn('Decision 2 ', out)
            self.assertIn('2026-10-03 (Alex). Decision 3', out)
            self.assertTrue(max(len(l) for l in out.splitlines()) < 200, 'long entries are truncated')

    def test_adr_folder_and_deploy_skill(self):
        with tempfile.TemporaryDirectory() as root:
            git_repo(root)
            for name in ('README.md', '0001-use-postgres.md', '0002-drop-redis.md'):
                write(root, 'docs/adr/' + name, '# x\n')
            write(root, '.claude/skills/deploy-vps/SKILL.md', '# deploy\n')
            out = '\n'.join(repo_context.lines(root))
            self.assertIn('skill .claude/skills/deploy-vps', out)
            self.assertIn('0002-drop-redis', out)
            self.assertNotIn('README', out)

    def test_bare_repo_is_quiet(self):
        with tempfile.TemporaryDirectory() as root:
            git_repo(root)
            write(root, 'README.md', '# nothing to see\n')
            self.assertEqual(repo_context.lines(root), [])

    def remote_line(self, remote):
        with tempfile.TemporaryDirectory() as root:
            git_repo(root, remote)
            return '\n'.join(repo_context.lines(root))

    def test_non_github_remote_loses_its_credentials(self):
        out = self.remote_line('https://oauth2:glpat-xxx@gitlab.com/g/r.git')
        self.assertIn('git remote https://gitlab.com/g/r.git', out)
        for secret in ('glpat', 'oauth2', '@'):
            self.assertNotIn(secret, out)

    def test_remote_urls_lose_userinfo_and_query_in_every_form(self):
        cases = {
            'https://user:p@ss@git.example.com:8443/g/r.git': 'https://git.example.com:8443/g/r.git',
            'https://token123@git.example.com/g/r.git': 'https://git.example.com/g/r.git',
            'https://git.example.com/g/r.git?private_token=glpat-abc#frag': 'https://git.example.com/g/r.git',
            'https://tok:x@git.example.com/g/r.git?a=b': 'https://git.example.com/g/r.git',
            'ssh://git@git.example.com:2222/g/r.git': 'ssh://git.example.com:2222/g/r.git',
            'git@git.example.com:g/r.git': 'git.example.com:g/r.git',
            'git@git.example.com:g/r.git?x=secret': 'git.example.com:g/r.git',
            '/srv/git/r.git': '/srv/git/r.git',
        }
        for remote, shown in cases.items():
            out = self.remote_line(remote)
            self.assertIn('git remote ' + shown, out, remote)
            for secret in ('token123', 'glpat', 'p@ss', 'tok:x', 'secret', 'private_token', 'frag', 'git@', 'user:'):
                self.assertNotIn(secret, out, remote)

    def test_github_remote_output_is_unchanged_and_carries_no_credentials(self):
        for remote in ('git@github.com:acme/shop.git', 'https://github.com/acme/shop', 'https://github.com/acme/shop.git',
                       'ssh://git@github.com/acme/shop.git',
                       'https://x-access-token:ghp_secret@github.com/acme/shop.git',
                       'https://github.com/acme/shop.git?token=ghp_secret'):
            out = self.remote_line(remote)
            self.assertEqual(out.splitlines()[1], '- Accounts: GitHub acme/shop', remote)
            self.assertNotIn('ghp_', out)

    def test_hook_output_has_no_remote_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            git_repo(root, 'https://oauth2:glpat-xxx@gitlab.com/g/r.git?private_token=glpat-yyy')
            out = session_start(root, local_with_rulebook())
            self.assertIn('git remote https://gitlab.com/g/r.git', out)
            self.assertNotIn('glpat', out)
            self.assertNotIn('oauth2', out)


class Hook(unittest.TestCase):
    def test_hook_prints_context_from_a_subfolder(self):
        with tempfile.TemporaryDirectory() as root:
            git_repo(root, 'https://github.com/acme/shop')
            write(root, 'fly.toml', 'app = "shop-api"\n')
            os.makedirs(os.path.join(root, 'src'))
            out = session_start(os.path.join(root, 'src'), local_with_rulebook())
            self.assertIn('Deploy targets: Fly app shop-api', out)
            self.assertIn('Accounts: GitHub acme/shop', out)

    def test_hook_is_quiet_outside_a_repo_and_in_a_bare_repo(self):
        local = local_with_rulebook()
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(session_start(root, local), '')
            git_repo(root)
            self.assertEqual(session_start(root, local), '')


class Notices(unittest.TestCase):
    def test_kit_not_set_up_is_noticed_once_a_day(self):
        local = make_local(rulebook=False)
        with tempfile.TemporaryDirectory() as cwd:
            first = session_start(cwd, local)
            self.assertIn('mention once to Alex', first)
            self.assertIn('not set up', first)
            self.assertIn('install.sh', first)
            self.assertIn('/setup', first)
            self.assertEqual(session_start(cwd, local), '', 'second session on the same day stays quiet')

    def test_set_up_kit_has_no_setup_notice(self):
        with tempfile.TemporaryDirectory() as cwd:
            self.assertNotIn('not set up', session_start(cwd, local_with_rulebook()))

    def test_unnamed_user_is_the_user(self):
        local = make_local(dict(TEST_CONFIG, user_name=''), rulebook=False)
        with tempfile.TemporaryDirectory() as cwd:
            self.assertIn('mention once to the user when relevant', session_start(cwd, local))

    def test_missing_local_folder_prints_the_setup_notice_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as cwd:
            local = os.path.join(parent, 'no-local-yet')
            out = session_start(cwd, local)
            self.assertIn('not set up', out)
            self.assertFalse(os.path.exists(local))

    def icloud_repo(self, home):
        root = os.path.join(home, 'Documents', 'app')
        git_repo(root)
        return root

    def test_icloud_notice_suggests_projects_root(self):
        local = local_with_rulebook(dict(TEST_CONFIG, projects_root='/work/projects'))
        home = tempfile.mkdtemp(prefix='akit-home-')
        root = self.icloud_repo(home)
        first = session_start(root, local, home=home, wrapper=True)
        self.assertIn('iCloud', first)
        self.assertIn('moving it to /work/projects', first)
        self.assertEqual(session_start(root, local, home=home, wrapper=True), '', 'once a day per repo')

    def test_icloud_notice_without_projects_root(self):
        home = tempfile.mkdtemp(prefix='akit-home-')
        root = self.icloud_repo(home)
        out = session_start(root, local_with_rulebook(), home=home, wrapper=True)
        self.assertIn('moving it to a folder outside iCloud', out)

    def test_icloud_warning_can_be_turned_off(self):
        local = local_with_rulebook(dict(TEST_CONFIG, icloud_warning=False))
        home = tempfile.mkdtemp(prefix='akit-home-')
        root = self.icloud_repo(home)
        self.assertEqual(session_start(root, local, home=home, wrapper=True), '')

    def test_repo_outside_icloud_folders_is_not_noticed(self):
        home = tempfile.mkdtemp(prefix='akit-home-')
        root = os.path.join(home, 'code', 'app')
        git_repo(root)
        self.assertEqual(session_start(root, local_with_rulebook(), home=home, wrapper=True), '')

    def test_instruction_files_over_budget_are_noticed(self):
        local = local_with_rulebook()
        with tempfile.TemporaryDirectory() as root:
            git_repo(root)
            write(root, 'AGENTS.md', ''.join('- rule %d\n' % i for i in range(200)))
            out = session_start(root, local)
            self.assertIn('AGENTS.md is 200 lines (budget 150)', out)
            self.assertIn('/instructions', out)

    def test_waiting_corrections_are_noticed(self):
        local = local_with_rulebook()
        inbox = os.path.join(local, 'learning', 'inbox')
        os.makedirs(inbox)
        with open(os.path.join(inbox, '2026-10.jsonl'), 'w') as fh:
            for i in range(10):
                fh.write(json.dumps({'id': 'id%d' % i, 'text': 'no, wrong %d' % i}) + '\n')
        with tempfile.TemporaryDirectory() as cwd:
            out = session_start(cwd, local)
            self.assertIn('10 captured corrections are waiting', out)
            self.assertIn('/learn', out)

    def test_drift_from_the_installed_global_file_is_noticed(self):
        local = local_with_rulebook()
        home = tempfile.mkdtemp(prefix='akit-home-')
        write(home, '.claude/CLAUDE.md', '# an older build\n')
        with tempfile.TemporaryDirectory() as cwd:
            out = session_start(cwd, local, home=home)
            self.assertIn('no longer matches the kit', out)
            self.assertIn('install.sh --sync', out)
            self.assertIn('bash %s --sync' % os.path.join(CORE, 'install.sh'), out)

    def test_installed_file_matching_the_build_is_silent(self):
        local = local_with_rulebook()
        home = tempfile.mkdtemp(prefix='akit-home-')
        built = subprocess.run(
            [sys.executable, '-c', 'import sys; sys.path.insert(0, %r); import common; '
             'sys.stdout.write(common.build_text("claude"))' % HOOKS],
            capture_output=True, text=True, env=env_for(local, HOME=home), check=True).stdout
        write(home, '.claude/CLAUDE.md', built)
        with tempfile.TemporaryDirectory() as cwd:
            self.assertEqual(session_start(cwd, local, home=home), '')


class GitHook(unittest.TestCase):
    """The pre-commit hook goes into repos under projects_root, through bin/git_hooks.py (stubbed here)."""

    def run_with_stub(self, root, projects, installed=False, raising=False, proc_cwd=None):
        log = tempfile.NamedTemporaryFile('w', suffix='.log', delete=False)
        log.close()
        local = local_with_rulebook()
        out = session_start(root, local, wrapper=True, STUB_GIT_HOOKS=projects or '-',
                            STUB_INSTALLED='1' if installed else '0', STUB_RAISE='1' if raising else '',
                            STUB_LOG=log.name, proc_cwd=proc_cwd)
        with open(log.name) as fh:
            calls = fh.read().split()
        os.unlink(log.name)
        return out, calls, local

    def test_repo_under_projects_root_gets_the_hook(self):
        with tempfile.TemporaryDirectory() as projects:
            root = os.path.join(projects, 'app')
            git_repo(root)
            _, calls, _ = self.run_with_stub(root, projects)
            self.assertEqual([os.path.realpath(c) for c in calls], [os.path.realpath(root)])

    def test_repo_that_already_has_it_is_left_alone(self):
        with tempfile.TemporaryDirectory() as projects:
            root = os.path.join(projects, 'app')
            git_repo(root)
            self.assertEqual(self.run_with_stub(root, projects, installed=True)[1], [])

    def test_repo_outside_projects_root_is_left_alone(self):
        with tempfile.TemporaryDirectory() as projects, tempfile.TemporaryDirectory() as elsewhere:
            git_repo(elsewhere)
            self.assertEqual(self.run_with_stub(elsewhere, projects)[1], [])

    def test_no_projects_root_means_off(self):
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, 'app')
            git_repo(root)
            # started from the folder above the repo, so an empty projects_root must not be read as 'here'
            self.assertEqual(self.run_with_stub(root, '', proc_cwd=parent)[1], [])

    def test_a_failing_git_hooks_never_breaks_the_hook(self):
        with tempfile.TemporaryDirectory() as projects:
            root = os.path.join(projects, 'app')
            git_repo(root)
            out, calls, local = self.run_with_stub(root, projects, raising=True)
            self.assertEqual(calls, [])
            self.assertNotIn('Traceback', out)
            with open(os.path.join(local, 'learning', 'hook-errors.log')) as fh:
                self.assertIn('git_hooks', fh.read())

    def test_real_git_hooks_installs_under_projects_root(self):
        sys.path.insert(0, BIN)
        try:
            import git_hooks
            has_api = all(hasattr(git_hooks, n) for n in ('projects_root', 'check', 'install'))
        except Exception:
            has_api = False
        if not has_api:
            self.skipTest('bin/git_hooks.py does not expose projects_root()/check()/install() yet')
        with tempfile.TemporaryDirectory() as projects:
            root = os.path.join(projects, 'app')
            git_repo(root)
            local = local_with_rulebook(dict(TEST_CONFIG, projects_root=projects))
            session_start(root, local)
            hook = os.path.join(root, '.git', 'hooks', 'pre-commit')
            self.assertTrue(os.path.isfile(hook), 'pre-commit hook installed')
            with open(hook) as fh:
                self.assertIn('agent-core', fh.read())


if __name__ == '__main__':
    unittest.main()
