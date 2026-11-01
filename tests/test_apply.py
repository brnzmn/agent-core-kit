"""apply.py turns approved proposals into exact bullet edits in the user's own files (local/AGENTS.md, local/claude.md,
local/codex.md, a repo's AGENTS.md or .claude/rules), refuses everything else, records each decision in the ledger,
commits in the git repo that is local/, and renames a fully decided proposals file to .done.md."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN, CORE, env_for, make_local  # noqa: E402

RULEBOOK = """# Working with Alex

## Git
- Stage files by name.
- Never force-push a shared branch.

## Checks
- Run the tests before saying done.

## Empty section
"""


def slurp(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def spit(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)


def proposal(n, action, target, rule='', replaces='', why='Because it matters', review_by=''):
    lines = ['## P%d: title %d' % (n, n), '- action: ' + action, '- target: ' + target, '- rule: ' + rule]
    if replaces:
        lines.append('- replaces: ' + replaces)
    lines.append('- why: ' + why)
    lines.append('- evidence: 2026-10-01, project x, signal 3')
    if review_by:
        lines.append('- review-by: ' + review_by)
    return '\n'.join(lines) + '\n'


class Apply(unittest.TestCase):
    def setUp(self):
        self.local = make_local(rulebook=False)
        self.rulebook = os.path.join(self.local, 'AGENTS.md')
        spit(self.rulebook, RULEBOOK)
        self.git('init', '-q')
        self.git('config', 'user.email', 'alex@example.com')
        self.git('config', 'user.name', 'Alex')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'start')
        self.addCleanup(shutil.rmtree, self.local, True)
        self.stamp = '2026-10-30'

    def git(self, *args, cwd=None):
        out = subprocess.run(['git', '-C', cwd or self.local] + list(args), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def write(self, *proposals, name=None):
        path = os.path.join(self.local, 'learning', 'proposals', (name or self.stamp) + '.md')
        spit(path, '# Proposals\n\n' + '\n'.join(proposals))
        return path

    def apply(self, path, *args, local=None, env=None):
        return subprocess.run([sys.executable, os.path.join(BIN, 'apply.py'), path] + list(args), capture_output=True,
                              text=True, env=env or env_for(local or self.local), timeout=60)

    def no_identity_env(self):
        """An environment in which git knows nobody: no global or system config, no EMAIL, no author variables."""
        home = tempfile.mkdtemp(prefix='akit-nohome-')
        self.addCleanup(shutil.rmtree, home, True)
        env = env_for(self.local, HOME=home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        for name in ('EMAIL', 'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
            env.pop(name, None)
        return env

    def ledger(self):
        path = os.path.join(self.local, 'learning', 'ledger.md')
        rows = []
        for line in slurp(path).splitlines():
            if line.startswith('| 20'):
                rows.append([c.strip() for c in line.strip().strip('|').split('|')])
        return rows

    def test_add_replace_remove_edit_exact_bullets(self):
        path = self.write(
            proposal(1, 'add', 'AGENTS.md#Git', 'Name the branch in every commit message, so history reads alone.'),
            proposal(2, 'replace', 'AGENTS.md#Checks', 'Run the tests and the linter before saying done.',
                     replaces='Run the tests before saying done.'),
            proposal(3, 'remove', 'AGENTS.md#Git', replaces='Never force-push a shared branch.'),
            proposal(4, 'add', 'AGENTS.md#empty section', 'Fill the empty section.'))
        out = self.apply(path, '--approve', 'P1,P2,P3,P4')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(slurp(self.rulebook), """# Working with Alex

## Git
- Stage files by name.
- Name the branch in every commit message, so history reads alone.

## Checks
- Run the tests and the linter before saying done.

## Empty section
- Fill the empty section.
""")
        self.assertIn('P1 applied', out.stdout)
        self.assertRegex(out.stdout, r'next: bash \S*install\.sh --sync')

    def test_edit_replaces_the_proposed_wording(self):
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'Original wording.'))
        out = self.apply(path, '--approve', 'P1', '--edit', 'P1=Edited wording, with a reason.')
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn('- Edited wording, with a reason.', slurp(self.rulebook))
        self.assertNotIn('Original wording', slurp(self.rulebook))
        self.assertIn('Edited wording', self.ledger()[0][5])

    def test_forbidden_targets_are_refused_and_nothing_is_written(self):
        kit_adapter = os.path.join(CORE, 'adapters', 'claude.md')
        before = slurp(kit_adapter)
        targets = ['adapters/claude.md#Claude Code', 'core/AGENTS.md#Git', 'local/AGENTS.md#Git',
                   '../outside.md#Git', '/etc/hosts#Git', 'repo:/tmp/some-repo/README.md#Git',
                   'repo:%s#Claude Code' % kit_adapter,
                   'repo:%s#Git' % os.path.join(CORE, 'templates', 'core', 'AGENTS.md'),
                   'repo:/tmp/x/.claude/rules/../../secret.txt#Git', 'CLAUDE.md#Git', 'Claude.md#Git', 'agents.md#Git']
        path = self.write(*[proposal(i + 1, 'add', t, 'A rule.') for i, t in enumerate(targets)])
        out = self.apply(path, '--approve', ','.join('P%d' % (i + 1) for i in range(len(targets))))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertEqual(out.stdout.count('failed: target not allowed'), len(targets), out.stdout)
        self.assertEqual(slurp(self.rulebook), RULEBOOK)
        self.assertEqual(slurp(kit_adapter), before)
        self.assertFalse(os.path.exists(os.path.join(self.local, 'claude.md')))
        self.assertTrue(os.path.isfile(path), 'a file with failures is not renamed')
        self.assertEqual([r[2][:6] for r in self.ledger()], ['failed'] * len(targets))

    def test_budget_refusal_allows_shrinking_and_equal_size_edits(self):
        full = '# Rules\n\n## Git\n' + ''.join('- rule %d\n' % i for i in range(117))  # 120 lines
        spit(self.rulebook, full)
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'One rule too many.'),
                          proposal(2, 'replace', 'AGENTS.md#Git', 'rule one, reworded', replaces='rule 1'),
                          proposal(3, 'remove', 'AGENTS.md#Git', replaces='rule 2'))
        out = self.apply(path, '--approve', 'P1,P2,P3')
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn('P1 failed: would exceed the 120-line budget; pair it with a removal', out.stdout)
        self.assertIn('P2 applied', out.stdout)
        self.assertIn('P3 applied', out.stdout)
        text = slurp(self.rulebook)
        self.assertNotIn('One rule too many', text)
        self.assertIn('- rule one, reworded', text)
        self.assertNotIn('- rule 2\n', text)

    def test_adapter_budget_is_60_lines(self):
        spit(os.path.join(self.local, 'claude.md'), '## Mine\n' + ''.join('- r%d\n' % i for i in range(59)))  # 60 lines
        path = self.write(proposal(1, 'add', 'claude.md#Mine', 'Number sixty-one.'))
        out = self.apply(path, '--approve', 'P1')
        self.assertIn('would exceed the 60-line budget', out.stdout)

    def test_missing_section_rule_or_replaces_fail_cleanly(self):
        path = self.write(
            proposal(1, 'add', 'AGENTS.md#No such section', 'A rule.'),
            proposal(2, 'add', 'AGENTS.md#Git', 'Stage files by name.'),
            proposal(3, 'replace', 'AGENTS.md#Git', 'New.', replaces='A bullet that is not there'),
            proposal(4, 'add', 'AGENTS.md#Git', ''),
            proposal(5, 'delete', 'AGENTS.md#Git', 'x'))
        out = self.apply(path, '--approve', 'P1,P2,P3,P4,P5')
        self.assertEqual(out.returncode, 1)
        for expect in ('failed: section "No such section" not found', 'failed: rule already present',
                       'failed: rule to replace not found', 'failed: proposal has no rule text',
                       "failed: unknown action 'delete'"):
            self.assertIn(expect, out.stdout)
        self.assertEqual(slurp(self.rulebook), RULEBOOK)

    def test_ledger_rows_and_review_dates(self):
        custom = (datetime.date.today() + datetime.timedelta(days=60)).isoformat()
        default = (datetime.date.today() + datetime.timedelta(days=90)).isoformat()
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'First rule | with a pipe.', review_by=custom),
                          proposal(2, 'add', 'AGENTS.md#Checks', 'Second rule.'),
                          proposal(3, 'add', 'AGENTS.md#Checks', 'Third rule, rejected.'),
                          proposal(4, 'add', 'AGENTS.md#Checks', 'Fourth rule, left for later.'))
        out = self.apply(path, '--approve', 'P1,P2', '--reject', 'P3')
        self.assertEqual(out.returncode, 0, out.stdout)
        rows = self.ledger()
        self.assertEqual([(r[1], r[2]) for r in rows],
                         [('2026-10-30/P1', 'applied'), ('2026-10-30/P2', 'applied'), ('2026-10-30/P3', 'rejected')])
        self.assertEqual(rows[0][0], datetime.date.today().isoformat())
        self.assertEqual(rows[0][3:5], ['add', 'AGENTS.md#Git'])
        self.assertIn('First rule / with a pipe.', rows[0][5])
        self.assertEqual((rows[0][7], rows[1][7], rows[2][7]), (custom, default, ''))
        self.assertTrue(slurp(os.path.join(self.local, 'learning', 'ledger.md')).startswith('# Learning ledger'))
        # P4 stayed undecided: the file is kept for the next round
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.exists(path[:-3] + '.done.md'))

    def test_commit_lands_in_the_local_repo_with_files_staged_by_name(self):
        spit(os.path.join(self.local, 'notes-in-progress.txt'), 'not part of the learning loop\n')
        spit(os.path.join(self.local, 'staged-by-user.txt'), 'x\n')
        self.git('add', 'staged-by-user.txt')
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'Name the branch in commit messages.'))
        out = self.apply(path, '--approve', 'P1')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('committed', out.stdout.splitlines())
        self.assertEqual(self.git('log', '-1', '--format=%s').strip(), 'learn: 2026-10-30 (P1 applied)')
        files = self.git('show', '--name-only', '--format=', 'HEAD').split()
        self.assertEqual(sorted(files), ['AGENTS.md', 'learning/ledger.md', 'learning/proposals/2026-10-30.done.md'])
        status = self.git('status', '--porcelain')
        self.assertIn('?? notes-in-progress.txt', status)
        self.assertIn('A  staged-by-user.txt', status)

    def test_commit_without_a_git_identity_uses_the_kit_identity(self):
        self.git('config', '--unset', 'user.email')
        self.git('config', '--unset', 'user.name')
        self.git('config', 'user.useConfigOnly', 'true')  # no guessing a name from the machine: the "unknown" case
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'Name the branch in commit messages.'))
        out = self.apply(path, '--approve', 'P1', env=self.no_identity_env())
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('committed', out.stdout.splitlines())
        self.assertEqual(self.git('log', '-1', '--format=%an <%ae> / %cn <%ce>').strip(),
                         'agent-core-kit <agent-core-kit@localhost> / agent-core-kit <agent-core-kit@localhost>')
        self.assertEqual(subprocess.run(['git', '-C', self.local, 'config', '--local', 'user.email'], capture_output=True,
                                        text=True).stdout, '', 'the kit identity is not written into the config')

    def test_commit_with_a_git_identity_keeps_the_users_identity(self):
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'Name the branch in commit messages.'))
        out = self.apply(path, '--approve', 'P1', env=self.no_identity_env())
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.git('log', '-1', '--format=%an <%ae>').strip(), 'Alex <alex@example.com>')

    def test_option_abbreviations_are_refused(self):
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'Name the branch in commit messages.'))
        for flag in ('--appr', '--app', '--approv'):
            out = self.apply(path, flag, 'P1')
            self.assertEqual(out.returncode, 2, flag)
            self.assertIn('unrecognized arguments', out.stderr)
        self.assertNotIn('Name the branch', slurp(self.rulebook))
        out = self.apply(path, '--reject', 'P1', '--ed', 'P1=x')
        self.assertEqual(out.returncode, 2)
        self.assertEqual(self.apply(path, '--approve', 'P1').returncode, 0)
        self.assertIn('- Name the branch in commit messages.', slurp(self.rulebook))

    def test_old_proposal_name_leaves_the_repo_when_it_was_committed(self):
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'A rule.'))
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'proposals')
        self.assertEqual(self.apply(path, '--approve', 'P1').returncode, 0)
        self.assertNotIn('proposals/2026-10-30.md', self.git('status', '--porcelain'))
        self.assertIn('learning/proposals/2026-10-30.done.md', self.git('ls-files').split())
        self.assertNotIn('learning/proposals/2026-10-30.md', self.git('ls-files').split())

    def test_no_git_repo_in_local_skips_the_commit_with_a_note(self):
        shutil.rmtree(os.path.join(self.local, '.git'))
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'A rule.'))
        out = self.apply(path, '--approve', 'P1')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertRegex(out.stdout, r'not committed: \S+ is not a git repo')
        self.assertIn('- A rule.', slurp(self.rulebook))

    def test_local_without_git_never_commits_into_a_parent_repo(self):
        parent = tempfile.mkdtemp(prefix='akit-parent-')
        self.addCleanup(shutil.rmtree, parent, True)
        self.git('init', '-q', cwd=parent)
        inner = os.path.join(parent, 'local')
        os.makedirs(inner)
        spit(os.path.join(inner, 'AGENTS.md'), RULEBOOK)
        spit(os.path.join(inner, 'config.json'), '{}')
        path = os.path.join(inner, 'learning', 'proposals', 'p.md')
        spit(path, proposal(1, 'add', 'AGENTS.md#Git', 'A rule.'))
        out = self.apply(path, '--approve', 'P1', local=inner)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('not a git repo', out.stdout)
        self.assertEqual(self.git('status', '--porcelain', cwd=parent).count('A '), 0)

    def test_personal_adapters_are_created_on_first_add(self):
        path = self.write(proposal(1, 'add', 'claude.md#Delegation', 'Brief subagents with the files they need.'),
                          proposal(2, 'add', 'codex.md#Sandbox', 'Say when a command needs network access.'),
                          proposal(3, 'add', 'claude.md#Delegation', 'Return only conclusions from Explore.'))
        out = self.apply(path, '--approve', 'P1,P2,P3')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(slurp(os.path.join(self.local, 'claude.md')),
                         '### Delegation\n\n- Brief subagents with the files they need.\n'
                         '- Return only conclusions from Explore.\n')
        self.assertEqual(slurp(os.path.join(self.local, 'codex.md')),
                         '### Sandbox\n\n- Say when a command needs network access.\n')
        self.assertEqual(sorted(self.git('show', '--name-only', '--format=', 'HEAD').split()),
                         ['claude.md', 'codex.md', 'learning/ledger.md', 'learning/proposals/2026-10-30.done.md'])

    def test_personal_adapter_gets_a_new_section_but_replace_needs_the_file(self):
        spit(os.path.join(self.local, 'codex.md'), '## Codex notes\n- one\n')
        path = self.write(proposal(1, 'add', 'codex.md#Sandbox', 'Two.'),
                          proposal(2, 'replace', 'claude.md#Delegation', 'x', replaces='y'),
                          proposal(3, 'add', 'AGENTS.md#Not there', 'Rulebooks do not grow sections.'))
        out = self.apply(path, '--approve', 'P1,P2,P3')
        self.assertEqual(slurp(os.path.join(self.local, 'codex.md')), '## Codex notes\n- one\n\n### Sandbox\n\n- Two.\n')
        self.assertIn('P2 failed: target file not found', out.stdout)
        self.assertIn('P3 failed: section "Not there" not found', out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.local, 'claude.md')))

    def test_repo_targets_edit_repo_files_and_stay_out_of_the_local_commit(self):
        repo = tempfile.mkdtemp(prefix='akit-repo-')
        self.addCleanup(shutil.rmtree, repo, True)
        spit(os.path.join(repo, 'AGENTS.md'), '# Repo\n\n## Build\n- Run make test.\n')
        spit(os.path.join(repo, '.claude', 'rules', 'api.md'), '# API\n- Version every endpoint.\n')
        path = self.write(proposal(1, 'add', 'repo:%s#Build' % os.path.join(repo, 'AGENTS.md'), 'Run make lint first.'),
                          proposal(2, 'replace', 'repo:%s#API' % os.path.join(repo, '.claude', 'rules', 'api.md'),
                                   'Version every endpoint in the path.', replaces='Version every endpoint.'))
        out = self.apply(path, '--approve', 'P1,P2')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(slurp(os.path.join(repo, 'AGENTS.md')), '# Repo\n\n## Build\n- Run make test.\n- Run make lint first.\n')
        self.assertIn('- Version every endpoint in the path.', slurp(os.path.join(repo, '.claude', 'rules', 'api.md')))
        self.assertIn('changed %s; commit it in that repo when ready' % os.path.join(repo, 'AGENTS.md'), out.stdout)
        self.assertNotIn('next:', out.stdout)
        self.assertEqual(sorted(self.git('show', '--name-only', '--format=', 'HEAD').split()),
                         ['learning/ledger.md', 'learning/proposals/2026-10-30.done.md'])

    def test_repo_file_budget_follows_its_kind(self):
        repo = tempfile.mkdtemp(prefix='akit-repo-')
        self.addCleanup(shutil.rmtree, repo, True)
        spit(os.path.join(repo, '.claude', 'rules', 'big.md'), '# Big\n' + ''.join('- r%d\n' % i for i in range(79)))
        path = self.write(proposal(1, 'add', 'repo:%s#Big' % os.path.join(repo, '.claude', 'rules', 'big.md'), 'More.'))
        self.assertIn('would exceed the 80-line budget', self.apply(path, '--approve', 'P1').stdout)

    def test_proposals_file_is_renamed_done_when_every_proposal_is_decided(self):
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'Rule one.'), proposal(2, 'add', 'AGENTS.md#Git', 'Rule two.'))
        out = self.apply(path, '--approve', 'P1')
        self.assertEqual(out.returncode, 0)
        self.assertTrue(os.path.isfile(path), 'P2 is still open')
        out = self.apply(path, '--reject', 'P2')
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.isfile(path[:-3] + '.done.md'))
        self.assertEqual(slurp(self.rulebook).count('Rule one.'), 1)
        self.assertEqual([r[2] for r in self.ledger()], ['applied', 'rejected'])

    def test_manual_proposals_wait_for_done(self):
        path = self.write(proposal(1, 'add', 'manual:add a deny rule for rm -rf', 'Block recursive deletes.'))
        out = self.apply(path, '--approve', 'P1')
        self.assertIn('P1 approved-manual', out.stdout)
        self.assertTrue(os.path.isfile(path))
        out = self.apply(path, '--done', 'P1')
        self.assertIn('P1 applied-manual', out.stdout)
        self.assertTrue(os.path.isfile(path[:-3] + '.done.md'))
        self.assertEqual([r[2] for r in self.ledger()], ['approved-manual', 'applied-manual'])
        self.assertEqual(slurp(self.rulebook), RULEBOOK)
        self.assertNotIn('next:', out.stdout)

    def test_deciding_all_proposals_closes_the_review_signals(self):
        rdir = os.path.join(self.local, 'learning', 'review', self.stamp)
        spit(os.path.join(rdir, 'ids.json'), json.dumps(['aaa', 'bbb']))
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'Rule one.'))
        self.apply(path, '--approve', 'P1')
        state = json.loads(slurp(os.path.join(self.local, 'learning', '.state.json')))
        self.assertEqual(state['processed_ids'], ['aaa', 'bbb'])

    def test_unknown_ids_and_empty_files_are_reported(self):
        path = self.write(proposal(1, 'add', 'AGENTS.md#Git', 'Rule one.'))
        out = self.apply(path, '--approve', 'P9')
        self.assertEqual(out.returncode, 1)
        self.assertIn('unknown proposal ids: P9', out.stdout)
        empty = os.path.join(self.local, 'learning', 'proposals', 'empty.md')
        spit(empty, '# nothing\n')
        self.assertIn('no proposals found', self.apply(empty, '--approve', 'P1').stdout)
        self.assertEqual(self.apply(os.path.join(self.local, 'nope.md'), '--approve', 'P1').returncode, 1)


if __name__ == '__main__':
    unittest.main()
