"""A parser error must never turn a guarded tool request into permission."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class GuardError(unittest.TestCase):
    def test_evaluator_failure_returns_an_explicit_denial(self):
        root = Path(__file__).resolve().parents[1]
        script = """import guard, sys
def fail(command):
    raise ValueError('synthetic parse failure')
guard.check_script = fail
guard.common.log_error = lambda where, exc: print(where, file=sys.stderr)
guard.main()
"""
        run = subprocess.run([sys.executable, '-c', script], cwd=root / 'claude/hooks',
                             input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'echo dummy'}}),
                             env=dict(os.environ, AGENT_CORE_DIR=str(root)), text=True, capture_output=True,
                             timeout=10, check=True)
        self.assertEqual(run.stderr.strip(), 'guard')
        self.assertTrue(run.stdout, 'empty output grants permission')
        self.assertEqual(json.loads(run.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
