"""Execution-order and generated-operand regressions from the review of PR #13."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[1]
GUARD = CORE / 'claude' / 'hooks' / 'guard.py'
LAKE = '/srv/guard-flow-lake'


def decision(command, cwd='/work/project'):
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / 'config.json'
        config.write_text(json.dumps({'protected_dirs': [LAKE]}))
        out = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(
            {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': cwd}),
            text=True, capture_output=True, timeout=20,
            env=dict(os.environ, AGENT_CORE_DIR=str(CORE), AGENT_CORE_LOCAL=str(tmp)))
        result = json.loads(out.stdout) if out.stdout.strip() else {}
        return result.get('hookSpecificOutput', {}).get('permissionDecision')


class ShellFlows(unittest.TestCase):
    def test_first_cd_success_assumption_expires_with_its_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / 'missing')
            command = 'cd %s && echo ready; rm -rf records' % missing
            self.assertEqual(decision(command, cwd=LAKE), 'deny')
            self.assertIsNone(decision('cd %s && touch report.txt' % missing, cwd=LAKE))

    def test_cd_success_state_stays_known_inside_its_group(self):
        self.assertIsNone(decision('cd /tmp && (echo ready; touch report.txt)', cwd=LAKE))
        self.assertEqual(decision('cd /tmp && (echo ready); touch report.txt', cwd=LAKE), 'deny')

    def test_expired_cd_chain_checks_both_complete_directory_candidates(self):
        self.assertIsNone(decision('cd /tmp && echo ready; touch report.txt'))
        self.assertIsNone(decision('cd /tmp && echo ready; (touch report.txt)'))
        self.assertEqual(decision('cd /srv && echo ready; touch guard-flow-lake/raw'), 'deny')
        self.assertEqual(decision('cd "$Q" && echo ready; touch report.txt'), 'deny')

    def test_substitutions_use_the_containing_commands_state(self):
        for substitution in ('"$(touch $D/raw)"', '"`touch $D/raw`"'):
            command = 'D=%s; echo %s; D=/tmp' % (LAKE, substitution)
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        command = 'cd %s; echo "$(touch raw)"; cd /tmp' % LAKE
        self.assertEqual(decision(command), 'deny')
        for command in ('D=/tmp; echo "$(touch $D/raw)"; D=' + LAKE,
                        "D=%s; echo '$(touch $D/raw)'; D=/tmp" % LAKE):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))

    def test_process_substitution_output_is_not_a_staging_pathspec(self):
        for command in ('git add -A > >(tee stage.log)',
                        'git stage -u > >(cat)', 'git add --all < <(printf input)'):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision('git add -A named.py > stage.log'))

    def test_operand_free_cd_uses_the_shell_home(self):
        for prefix in ('HOME=' + LAKE, 'export HOME=' + LAKE, 'unset HOME'):
            command = prefix + '; cd && touch note'
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision('HOME=/tmp; cd && touch note'))

    def test_unknown_child_reset_modes_cannot_hide_exported_hard(self):
        for command in ("export FLAG=--hard; sh -c 'git reset \"$FLAG\"'",
                        "env FLAG=--hard bash -c 'git reset \"$FLAG\"'"):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision("sh -c 'git reset --soft HEAD'"))
        self.assertIsNone(decision('git reset -- "$PATHSPEC"'))

    def test_env_attached_values_stop_short_option_parsing(self):
        for command in ('env -uSSH_AUTH_SOCK git reset --hard',
                        'env -vuSSH_AUTH_SOCK git reset --hard',
                        'env -uS git reset --hard', 'env -u S git reset --hard'):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision('env -uSSH_AUTH_SOCK git status --short'))

    def test_read_redirections_are_not_builtin_destinations(self):
        for command in ('IFS= read -r line < README.md', 'read -r line <<< "hello"',
                        'read -r line 2> notes.log'):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))
        self.assertEqual(decision('read -r line > ' + LAKE + '/raw'), 'deny')

    def test_abbreviated_pathspec_file_options_cannot_supply_named_paths(self):
        for option in ('--pathspec-from-fil paths', '--pathspec-from-fil=paths',
                       '--pathspec-from-f paths'):
            command = 'git add -A ' + option
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision('git add -A -- --pathspec-from-fil'))

    def test_printf_shell_pipelines_use_formatted_output(self):
        for command in ("printf '%s%s' 'git reset ' --hard | sh",
                        "printf '%s %s' git 'reset --hard' | bash",
                        "printf '%s\\n' 'git reset --hard' | sh",
                        "printf '%b' 'git reset \\055\\055hard' | sh"):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        for command in ("printf '%s%s' 'echo ' safe | sh", "printf '%s\\n' 'echo safe' | sh"):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))


if __name__ == '__main__':
    unittest.main()
