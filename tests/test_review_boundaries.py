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
    def test_credentials_on_the_next_line_are_redacted_completely(self):
        for whitespace in ('\n  ', '\r\n\t'):
            text = 'DB_PASSWORD:' + whitespace + 'FakeLettersOnlyPassword next field'
            expected = 'DB_PASSWORD:' + whitespace + '[REDACTED] next field'
            with self.subTest(whitespace=whitespace):
                self.assertEqual(runner.redact(text), (expected, 1))
                self.assertEqual(runner.redact(expected), (expected, 0))

    def test_markdown_code_credentials_are_redacted_completely(self):
        for ticks, value in [('`', 'LettersOnlyPassword'), ('``', 'short `quoted` value'),
                             ('`', 'x'), ('```', 'multiline\nfake password')]:
            with self.subTest(ticks=ticks, value=value):
                text = 'password: ' + ticks + value + ticks + ' next field'
                expected = 'password: ' + ticks + '[REDACTED]' + ticks + ' next field'
                self.assertEqual(runner.redact(text), (expected, 1))
                self.assertEqual(runner.redact(expected), (expected, 0))

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
            fixture = repo / 'tests/fixtures/hook_denials.json'
            fixture.parent.mkdir(parents=True)
            records = ['UNRELATED_CONTEXT_SENTINEL'] + ['fixture row %d' % i for i in range(400)]
            fixture.write_text(json.dumps(records, indent=2) + '\n')
            git('add', 'tests/fixtures/hook_denials.json')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (repo / 'sample').write_text('committed sample\n')
            records[200] = 'committed fixture change'
            fixture.write_text(json.dumps(records, indent=2) + '\n')
            git('add', 'sample', 'tests/fixtures/hook_denials.json')
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
            self.assertIn('+  "committed fixture change"', (d / 'diff').read_text())
            self.assertNotIn('UNRELATED_CONTEXT_SENTINEL', (d / 'diff').read_text())
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


class AuthenticatedBaseFetch(unittest.TestCase):
    def fetch(self, remotes, failures=(), resolved=BASE):
        calls = []
        def fake(cmd, **kwargs):
            calls.append((cmd, kwargs))
            code, out = 0, ''
            if cmd == ['git', 'remote']:
                out = '\n'.join(remotes)
            elif cmd[:4] == ['git', 'remote', 'get-url', '--all']:
                out = '\n'.join(remotes.get(cmd[-1], []))
            elif cmd[:2] == ['git', 'fetch']:
                code = 1 if cmd[-2] in failures else 0
            elif cmd[:2] == ['git', 'rev-parse']:
                out = resolved
            return subprocess.CompletedProcess(cmd, code, out, 'fetch unavailable' if code else '')
        with patch.object(runner, 'sh', side_effect=fake):
            result = runner.fetch_base(META)
        return result, [(cmd, kw) for cmd, kw in calls if cmd[:2] == ['git', 'fetch']]

    def assert_no_prompts(self, kwargs):
        self.assertEqual(kwargs.get('stdin'), subprocess.DEVNULL)
        self.assertEqual(kwargs.get('env', {}).get('GIT_TERMINAL_PROMPT'), '0')

    def test_matching_ssh_remote_preserves_authenticated_transport(self):
        ssh = 'git@github.com:valid/upstream.git'
        result, fetches = self.fetch({'origin': [ssh]})
        self.assertEqual(result, BASE)
        self.assertEqual([cmd[-2] for cmd, _ in fetches], [ssh])
        self.assert_no_prompts(fetches[0][1])

    def test_fork_origin_cannot_substitute_for_the_upstream(self):
        fork = 'git@github.com:fork/upstream.git'
        result, fetches = self.fetch({'origin': [fork]})
        self.assertEqual(result, BASE)
        self.assertEqual([cmd[-2] for cmd, _ in fetches], ['https://github.com/valid/upstream.git'])
        self.assert_no_prompts(fetches[0][1])

    def test_failed_matching_transport_tries_only_verified_upstream(self):
        ssh = 'ssh://git@github.com/valid/upstream.git'
        result, fetches = self.fetch({'origin': ['git@github.com:fork/upstream.git'], 'upstream': [ssh]},
                                    failures={ssh})
        self.assertEqual(result, BASE)
        self.assertEqual([cmd[-2] for cmd, _ in fetches], [ssh, 'https://github.com/valid/upstream.git'])
        for _, kwargs in fetches:
            self.assert_no_prompts(kwargs)

    def test_fetch_result_must_still_be_the_upstream_object(self):
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.fetch({'origin': ['git@github.com:valid/upstream.git']}, resolved='c' * 40)
