"""The review may consume immutable Git objects, but never an arbitrary host file or a stale base."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_boundaries', ROOT / 'bin/pr_review.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
HEAD, BASE = 'a' * 40, 'b' * 40
META = dict(number=13, headRefOid=HEAD, baseRefOid=BASE, baseRefName='master',
            url='https://github.com/valid/upstream/pull/13')


class Boundaries(unittest.TestCase):
    def test_named_secrets_are_redacted_completely_and_once(self):
        for name, value in [('SUPABASE_SERVICE_ROLE_KEY', 'eyFha2UxMjM0NTY3ODkw.encodedPayload.testSignature'),
                            ('password', 'LettersOnlyPassword'),
                            ('apikey', r'a\ spaced\ value'),
                            ('client_secret', '"space and escaped \\"quote\\" and punctuation!"'),
                            ('"apiKey"', "'a short key'")]:
            with self.subTest(name=name):
                line = name + ' = ' + value + ' next field'
                quote = value[:1] if value[:1] in ('"', "'") else ''
                expected = name + ' = ' + quote + '[REDACTED]' + quote + ' next field'
                self.assertEqual(runner.redact(line), (expected, 1))
                self.assertEqual(runner.redact(expected), (expected, 0))

    def test_reviewer_gets_tracked_diff_and_no_host_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            repo = d / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            def git(*args):
                return subprocess.check_output(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid',
                                                '-c', 'commit.gpgsign=false', *args], cwd=repo, text=True).strip()
            git('commit', '--allow-empty', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (repo / 'sample').write_text('committed sample\n')
            git('add', 'sample')
            git('commit', '-qm', 'sample')
            (repo / 'sample').write_text('private uncommitted text\n')
            cli = d / 'claude'
            cli.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$PROBE_ARGS"\ncat > "$PROBE_DIFF"\nprintf "VERDICT: PASS\\n"\n')
            cli.chmod(0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'], TMPDIR=tmp,
                       PROBE_ARGS=str(d / 'args'), PROBE_DIFF=str(d / 'diff'))
            out = subprocess.run(['bash', str(ROOT / 'bin/claude-review.sh'), base], cwd=repo, env=env,
                                 text=True, capture_output=True, timeout=15)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIn('+committed sample', (d / 'diff').read_text())
            self.assertNotIn('private uncommitted text', (d / 'diff').read_text())
            args = (d / 'args').read_text().splitlines()
            self.assertIn('--tools', args)
            self.assertEqual(args[args.index('--tools') + 1], '')
            self.assertNotIn('--allowedTools', args)
            self.assertIn('--strict-mcp-config', args)

    def test_review_uses_upstream_git_object_and_stops_on_fetch_failure(self):
        for fetch_code in (124, 0):
            calls = []
            def fake(cmd, **kwargs):
                calls.append(cmd)
                code, out = 0, ''
                if cmd[1:3] == ['pr', 'view']:
                    out = json.dumps(META)
                elif cmd[:2] == ['git', 'fetch']:
                    code = fetch_code
                elif cmd[:2] == ['git', 'rev-parse']:
                    out = HEAD if cmd[-1] == 'HEAD' else BASE
                return subprocess.CompletedProcess(cmd, code, out, '')
            result = runner.Review('PASS', 'Reviewed.') if hasattr(runner, 'Review') else ('PASS', 'Reviewed.')
            with self.subTest(fetch=fetch_code), patch.object(runner, 'sh', side_effect=fake), \
                    patch.object(runner, 'codex_review', return_value=result) as review, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(['13', 'codex']), 3 if fetch_code else 0)
                if fetch_code:
                    review.assert_not_called()
                    self.assertFalse(any(c[1:3] == ['pr', 'comment'] for c in calls))
                else:
                    review.assert_called_once_with(BASE, False)
                fetch = next(c for c in calls if c[:2] == ['git', 'fetch'])
                self.assertIn('https://github.com/valid/upstream.git', fetch)
                self.assertIn(BASE, fetch)
