"""The committed-head review must not turn slow analysis or lost operand provenance into permission."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import test_guard_review_inputs as inputs

CORE = Path(__file__).resolve().parents[1]
GUARD = CORE / 'claude' / 'hooks' / 'guard.py'


class ReviewFollowups(unittest.TestCase):
    setUp = inputs.ReviewedGuardInputs.setUp
    decide = inputs.ReviewedGuardInputs.decide

    def run_hook(self, command, program=None, timeout=8):
        argv = [sys.executable, '-c', program] if program else [sys.executable, str(GUARD)]
        payload = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': str(self.cwd)}
        try:
            result = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, timeout=timeout,
                                    env=dict(os.environ, AGENT_CORE_DIR=str(CORE),
                                             AGENT_CORE_LOCAL=str(self.cwd), AGENT_CORE_TOOL='codex'))
        except subprocess.TimeoutExpired:
            self.fail('The guard did not decide before the eight-second analysis deadline')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        return data.get('hookSpecificOutput', {})

    def test_many_branch_probes_cannot_overrun_the_hook_timeout(self):
        subprocess.run(['git', 'init', '-q', '-b', 'feature', str(self.cwd)], check=True, capture_output=True)
        command = 'git push --force-with-lease origin HEAD;' * 2500 + 'rm -rf lake-data/raw'
        self.assertLess(len(command), 200000)
        started = time.monotonic()
        self.assertEqual(self.run_hook(command).get('permissionDecision'), 'deny')
        self.assertLess(time.monotonic() - started, 8)

    def test_blocking_filesystem_analysis_is_preempted(self):
        program = ('import sys,time; sys.path.insert(0,sys.argv[0] if False else %r); import guard; '
                   'guard.ANALYSIS_BUDGET=0.15; '
                   'guard.common.repo_root=lambda path: (time.sleep(2) or None); guard.main()') % str(GUARD.parent)
        started = time.monotonic()
        result = self.run_hook('git add -A named.py', program=program, timeout=1.5)
        self.assertEqual(result.get('permissionDecision'), 'deny')
        self.assertLess(time.monotonic() - started, 1.5)

    def test_nested_shells_share_the_original_analysis_deadline(self):
        program = ('import sys,time; sys.path.insert(0,%r); import guard; '
                   'guard.ANALYSIS_BUDGET=0.12; '
                   'guard.current_branch=lambda path: (time.sleep(0.09) or "feature"); guard.main()') % str(GUARD.parent)
        command = "git push --force-with-lease origin HEAD; sh -c 'git push --force-with-lease origin HEAD'"
        self.assertEqual(self.run_hook(command, program=program, timeout=1.5).get('permissionDecision'), 'deny')

    def test_container_recursion_preserves_unknown_staging_operands(self):
        for wrapper in ('docker exec app', 'podman exec app', 'kubectl exec app --'):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(self.decide('FILES=$(printf .); %s git add -A "$FILES"' % wrapper), 'deny')
                self.assertIsNone(self.decide('FILES=README.md; %s git add -A "$FILES"' % wrapper))
                self.assertIsNone(self.decide('%s git add -A README.md' % wrapper))
        self.assertIsNone(self.decide('git add -A README.md'))

    def test_env_denials_explain_standalone_and_multiline_alternatives(self):
        (self.cwd / '.env').write_text('TOKEN="first\ncontinuation"\n')
        reason = self.run_hook("sed 's/=.*//' .env").get('permissionDecisionReason', '')
        self.assertIn('on its own', reason)
        self.assertIn('grep -c', reason)

    def test_uncertain_backup_names_are_not_exempted_by_their_prefix(self):
        for target in ('lake-data/backups/$NAME.sqlite', 'lake-data/backups/$(date +%Y%m%d).sqlite'):
            command = 'sqlite3 ordinary.db ".backup \'%s\'"' % target
            with self.subTest(target=target):
                result = self.run_hook(command)
                self.assertEqual(result.get('permissionDecision'), 'deny')
                self.assertIn('literal timestamp', result.get('permissionDecisionReason', ''))
        self.assertIsNone(self.decide('sqlite3 ordinary.db ".backup \'lake-data/backups/20261103.sqlite\'"'))

    def test_sed_names_only_cannot_hide_additional_commands_in_replacement_text(self):
        (self.cwd / '.env').write_text('TOKEN=fixture-secret\n')
        for command in ("sed $'s/=.*//\\nr .env\\ns/x/x/' .env",
                        "sed 's/=.*//;r .env;s/x/x/' .env",
                        "sed 's|=.*||;r .env;s|x|x|' .env"):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide("sed 's/=.*//' .env"))
        self.assertIsNone(self.decide("sed 's|=.*|REDACTED|' .env"))
        self.assertIsNone(self.decide("sed 's|=.*|/hidden/|' .env"))


if __name__ == '__main__':
    unittest.main()
