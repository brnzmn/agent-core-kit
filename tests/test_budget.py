"""budget.py: instruction files stay inside their line budgets, carry no build logs, and the generated global files
are not edited by hand."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import HOOKS, env_for, make_local, run_hook  # noqa: E402


def lines(n, prefix='rule'):
    return ''.join('- %s %d\n' % (prefix, i) for i in range(n))


def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        fh.write(text)
    return path


def check(tool, tool_input, local=None):
    """(decision or None, reason or '') from budget.py."""
    out = run_hook('budget.py', {'tool_name': tool, 'tool_input': tool_input}, local=local)
    if not out:
        return None, ''
    spec = out['hookSpecificOutput']
    return spec['permissionDecision'], spec['permissionDecisionReason']


class Budget(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='akit-repo-')

    def test_write_over_budget_is_denied(self):
        path = os.path.join(self.repo, 'AGENTS.md')
        decision, reason = check('Write', {'file_path': path, 'content': lines(151)})
        self.assertEqual(decision, 'deny')
        self.assertIn('151 lines', reason)
        self.assertIn('budget is 150', reason)

    def test_write_within_budget_is_allowed(self):
        path = os.path.join(self.repo, 'AGENTS.md')
        self.assertEqual(check('Write', {'file_path': path, 'content': lines(150)}), (None, ''))

    def test_other_instruction_kinds_have_their_own_budgets(self):
        rule = os.path.join(self.repo, '.claude', 'rules', 'style.md')
        self.assertEqual(check('Write', {'file_path': rule, 'content': lines(81)})[0], 'deny')
        self.assertIsNone(check('Write', {'file_path': rule, 'content': lines(80)})[0])
        claude_md = os.path.join(self.repo, 'CLAUDE.md')
        self.assertEqual(check('Write', {'file_path': claude_md, 'content': '@AGENTS.md\n' + lines(41)})[0], 'deny')
        self.assertIsNone(check('Write', {'file_path': claude_md, 'content': '@AGENTS.md\n' + lines(39)})[0])

    def test_the_rulebook_in_local_has_the_core_budget(self):
        local = make_local()
        path = os.path.join(local, 'AGENTS.md')
        self.assertEqual(check('Write', {'file_path': path, 'content': lines(121)}, local)[0], 'deny')
        self.assertIsNone(check('Write', {'file_path': path, 'content': lines(120)}, local)[0])

    def test_growing_an_edit_past_the_budget_is_denied(self):
        path = write_file(os.path.join(self.repo, 'AGENTS.md'), lines(149))
        decision, reason = check('Edit', {'file_path': path, 'old_string': '- rule 148\n',
                                          'new_string': '- rule 148\n- more a\n- more b\n- more c\n'})
        self.assertEqual(decision, 'deny')
        self.assertIn('152 lines', reason)

    def test_shrinking_edit_of_an_oversize_file_is_allowed(self):
        path = write_file(os.path.join(self.repo, 'AGENTS.md'), lines(200))
        old = ''.join('- rule %d\n' % i for i in range(100, 200))
        self.assertEqual(check('Edit', {'file_path': path, 'old_string': old, 'new_string': ''}), (None, ''))
        small = check('MultiEdit', {'file_path': path, 'edits': [
            {'old_string': '- rule 0\n', 'new_string': ''}, {'old_string': '- rule 1\n', 'new_string': ''}]})
        self.assertEqual(small, (None, ''))

    def test_adding_to_an_oversize_file_is_denied_even_when_small(self):
        path = write_file(os.path.join(self.repo, 'AGENTS.md'), lines(200))
        decision, reason = check('Edit', {'file_path': path, 'old_string': '- rule 0\n',
                                          'new_string': '- rule 0\n- one more\n'})
        self.assertEqual(decision, 'deny')
        self.assertIn('already 200 lines', reason)

    def test_log_entry_is_denied(self):
        path = os.path.join(self.repo, 'AGENTS.md')
        content = '# Repo\n\n## Rules\n- Stage files by name.\n\n## Build status\n- 2026-10-01 shipped the importer\n'
        decision, reason = check('Write', {'file_path': path, 'content': content})
        self.assertEqual(decision, 'deny')
        self.assertIn('build log or status entry', reason)
        self.assertIn('Build status', reason)

    def test_dated_progress_lines_are_denied_in_an_edit(self):
        path = write_file(os.path.join(self.repo, 'AGENTS.md'), '# Repo\n- Stage files by name.\n')
        decision, _ = check('Edit', {'file_path': path, 'old_string': '- Stage files by name.\n',
                                     'new_string': '- Stage files by name.\n- 2026-10-01 added the importer\n'
                                                   '- 2026-10-02 fixed the login\n'})
        self.assertEqual(decision, 'deny')

    def test_ordinary_rule_edit_is_allowed(self):
        path = write_file(os.path.join(self.repo, 'AGENTS.md'), '# Repo\n- Stage files by name.\n')
        self.assertEqual(check('Edit', {'file_path': path, 'old_string': '- Stage files by name.\n',
                                        'new_string': '- Stage files by name.\n- Keep PRs small.\n'}), (None, ''))

    def test_non_instruction_file_is_ignored(self):
        path = os.path.join(self.repo, 'src', 'big.py')
        self.assertEqual(check('Write', {'file_path': path, 'content': lines(2000)}), (None, ''))
        notes = os.path.join(self.repo, 'docs', 'build-log.md')
        self.assertEqual(check('Write', {'file_path': notes, 'content':
                                         '## Changelog\n- 2026-10-01 added x\n- 2026-10-02 fixed y\n'}), (None, ''))

    def test_call_without_a_file_path_is_ignored(self):
        self.assertEqual(check('Write', {'content': lines(500)}), (None, ''))


class Generated(unittest.TestCase):
    """~/.claude/CLAUDE.md and ~/.codex/AGENTS.md are built by the kit; edits belong in local/."""

    def test_generated_global_files_are_denied_with_the_way_out(self):
        local = make_local()
        rulebook = os.path.join(local, 'AGENTS.md')
        for rel in ('.claude/CLAUDE.md', '.codex/AGENTS.md'):
            path = os.path.expanduser(os.path.join('~', rel))
            decision, reason = check('Write', {'file_path': path, 'content': '# mine\n'}, local)
            self.assertEqual(decision, 'deny', rel)
            self.assertIn('generated by the kit', reason)
            self.assertIn('/learn', reason)
            self.assertIn('local/AGENTS.md', reason)
            self.assertIn(rulebook, reason)
            self.assertIn('local/claude.md / local/codex.md', reason)
            self.assertIn('install.sh --sync', reason)
            self.assertNotIn('~/agent-core/', reason)

    def test_edit_tool_is_denied_too(self):
        path = os.path.expanduser('~/.claude/CLAUDE.md')
        self.assertEqual(check('Edit', {'file_path': path, 'old_string': 'a', 'new_string': 'b'})[0], 'deny')


class NoLocalFolder(unittest.TestCase):
    def test_runs_quietly_without_a_local_folder(self):
        with tempfile.TemporaryDirectory() as parent:
            local = os.path.join(parent, 'no-local-yet')
            for payload in ({'tool_name': 'Write', 'tool_input': {'file_path': os.path.join(parent, 'a.py'),
                                                                  'content': 'x = 1\n'}},
                            {'tool_name': 'Write', 'tool_input': {'file_path': os.path.join(parent, 'AGENTS.md'),
                                                                  'content': 'ok\n'}}):
                out = subprocess.run([sys.executable, os.path.join(HOOKS, 'budget.py')], input=json.dumps(payload),
                                     capture_output=True, text=True, env=env_for(local), timeout=20)
                self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))
            self.assertEqual(check('Write', {'file_path': os.path.join(parent, 'AGENTS.md'),
                                             'content': lines(151)}, local)[0], 'deny')
            self.assertFalse(os.path.exists(local))

    def test_garbage_input_never_raises(self):
        out = subprocess.run([sys.executable, os.path.join(HOOKS, 'budget.py')], input='not json',
                             capture_output=True, text=True, env=env_for(), timeout=20)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))


if __name__ == '__main__':
    unittest.main()
