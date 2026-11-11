"""Review outages must never become verdicts, and fallback evidence must describe actual current-head tests."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

CORE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pr_review_runner', CORE / 'bin' / 'pr_review.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
HEAD = 'a' * 40
META = {'number': 7, 'baseRefName': 'master', 'headRefOid': HEAD, 'baseRefOid': HEAD,
        'url': 'https://github.com/acme/kit/pull/7'}
CMD = ['python3', '-m', 'unittest', 'discover', '-s', 'tests']


# Sanitized CLI result shapes. subtype may be success even on a provider error.
REAL_AUTH_401 = '{"terminal_reason": "api_error", "is_error": true, "subtype": "success", "api_error_status": 401, "api_error_origin": "first_party", "result": "Invalid API key \\u00b7 Fix external API key", "type": "result", "session_id": "fixture-session"}'
REAL_MODEL_404 = '{"terminal_reason": "api_error", "is_error": true, "subtype": "success", "api_error_status": 404, "result": "There\'s an issue with the selected model (claude-nonexistent-9). It may not exist or you may not have access to it. Run --model to pick a different model.", "type": "result", "session_id": "fixture-session"}'
REAL_OK = '{"terminal_reason": "completed", "is_error": false, "subtype": "success", "api_error_status": null, "result": "OK", "type": "result", "session_id": "fixture-session"}'

def completed(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, out, err)


class ProviderResults(unittest.TestCase):
    def setUp(self):
        available = patch.object(runner.shutil, 'which', return_value='/bin/provider')
        available.start()
        self.addCleanup(available.stop)

    def test_model_outage_words_do_not_establish_a_provider_outage(self):
        words = "Partial review. " * 40 + "You've hit your weekly limit"
        diagnostics = (words, 'Request failed while processing model output: ' + words,
                       json.dumps({'type': 'result', 'is_error': True, 'result': words}),
                       json.dumps({'type': 'error', 'error': {'type': 'internal_error', 'message': words}}))
        for error in diagnostics:
            with self.subTest(error=error), patch.object(runner, 'sh', return_value=completed(4, words, error)):
                self.assertIsNone(runner.claude_review('origin/main').unavailable)
            result = self.codex(1, {'codex': {'status': 1, 'stdout': words, 'stderr': error,
                                             'error': {'code': 'internal_error', 'message': words}}})
            self.assertIsNone(result.unavailable)

    def test_claude_outage_needs_a_typed_provider_error(self):
        for error_type, expected in (('rate_limit_error', 'quota'), ('authentication_error', 'auth'),
                                     ('overloaded_error', 'service')):
            error = json.dumps({'type': 'error', 'error': {'type': error_type, 'message': 'Provider error'}})
            with self.subTest(error_type=error_type), \
                    patch.object(runner, 'sh', return_value=completed(4, err=error)):
                self.assertEqual(runner.claude_review('origin/main').unavailable, expected)

    def test_claude_model_output_prevents_diagnostics_from_becoming_outage_evidence(self):
        error = json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error'}})
        for model in ('Partial review prose.', error):
            with self.subTest(model=model), patch.object(runner, 'sh', return_value=completed(4, model, error)):
                self.assertIsNone(runner.claude_review('origin/main').unavailable)

    def test_missing_claude_cli_is_independently_checked(self):
        with patch.object(runner.shutil, 'which', return_value=None), patch.object(runner, 'sh') as execute:
            result = runner.claude_review('origin/main')
        self.assertEqual(result.unavailable, 'missing_cli')
        execute.assert_not_called()

    def test_native_rate_limit_error_is_a_confirmed_quota_outage(self):
        result = self.codex(1, {'codex': {'status': 1, 'stdout': '', 'stderr': '',
                                          'error': {'codexErrorInfo': 'rateLimitExceeded'}}})
        self.assertEqual(result.unavailable, 'quota')

    def test_failed_installed_cli_is_not_classified_as_missing(self):
        result = self.codex(1, err='codex CLI not found in the runtime deployment')
        self.assertIsNone(result.unavailable)
        with patch.object(runner, 'sh', return_value=completed(4, 'claude: command not found in runtime')):
            self.assertIsNone(runner.claude_review('origin/main').unavailable)

    def test_provider_diagnostics_are_redacted_before_the_tail_is_taken(self):
        secret = 'gh' + 'p_' + 'a' * 3000
        for result in (self.codex(1, err='failure: ' + secret),
                       self.codex(1, {'codex': {'status': 1, 'stderr': 'failure: ' + secret}}),
                       self.codex(1, {'error': 'failure: ' + secret})):
            with self.subTest(result=result):
                self.assertIn('[REDACTED]', result.text)
                self.assertNotIn('a' * 100, result.text)
        with patch.object(runner, 'sh', return_value=completed(3, err='failure: ' + secret)):
            result = runner.claude_review('origin/main')
        self.assertIn('[REDACTED]', result.text)
        self.assertNotIn('a' * 100, result.text)

    def test_reviewer_script_comes_from_the_runner_installation(self):
        with patch.object(runner.common, 'CORE', '/untrusted-checkout'), \
                patch.object(runner, 'sh', return_value=completed(0, 'VERDICT: PASS')) as launch:
            runner.claude_review('origin/main')
        self.assertEqual(launch.call_args.args[0][1], str(CORE / 'bin/claude-review.sh'))

    def codex(self, code=0, data=None, err='', adversarial=False):
        out = json.dumps(data) if data is not None else 'bad JSON'
        with patch.object(runner.os.path, 'isfile', return_value=True), \
                patch.object(runner.shutil, 'which', return_value='/bin/tool'), \
                patch.object(runner, 'sh', side_effect=[completed(code, out, err), completed()]):
            return runner.codex_review('origin/main', adversarial)

    def test_completed_native_review_passes_or_blocks(self):
        self.assertEqual(self.codex(data={'codex': {'status': 0, 'stdout': 'No actionable findings.'}}).verdict, 'PASS')
        self.assertEqual(self.codex(data={'codex': {'status': 0, 'stdout': '[P1] Unsafe deletion'}}).verdict, 'BLOCK')
        self.assertEqual(self.codex(data={'codex': {'status': 0, 'stdout': 'VERDICT: BLOCK'}}).verdict, 'BLOCK')

    def test_failed_codex_cannot_pass_based_on_nonempty_text(self):
        for code, status in ((1, 0), (0, 1), (0, None), (0, False)):
            with self.subTest(code=code, status=status):
                result = self.codex(code, {'codex': {'status': status, 'stdout': 'The execution failed.'}})
                self.assertIsNone(result.verdict)
                self.assertIsNone(result.unavailable)

    def test_partial_prose_is_not_an_outage_diagnostic(self):
        result = self.codex(1, {'codex': {'status': 1, 'stdout': '[P2] service unavailable handler',
                                          'error': {'code': 'internal_error', 'message': 'local file missing'}}})
        self.assertIsNone(result.unavailable)
        self.assertIsNone(result.verdict)

    def test_a_review_requires_text_not_an_error_object(self):
        result = self.codex(data={'codex': {'status': 0, 'stdout': {'error': 'bad frame'}}})
        self.assertIsNone(result.verdict)
        self.assertIsNone(result.unavailable)

    def test_malformed_codex_is_an_error_not_an_outage(self):
        for data in (None, [], {'codex': 'bad'}, {'codex': {'status': 0, 'stdout': ''}}):
            with self.subTest(data=data):
                result = self.codex(data=data)
                self.assertIsNone(result.verdict)
                self.assertIsNone(result.unavailable)

    def test_adversarial_requires_a_valid_structured_verdict(self):
        self.assertIsNone(self.codex(data={'codex': {'status': 0, 'stdout': 'okay'}}, adversarial=True).verdict)
        data = {'codex': {'status': 0, 'stdout': 'okay'}, 'result': {'verdict': 'approve'}}
        self.assertEqual(self.codex(data=data, adversarial=True).verdict, 'PASS')

    def test_codex_quota_failure_is_available_for_fallback(self):
        result = self.codex(1, {'codex': {'status': 1, 'error': {'codexErrorInfo': 'usageLimitExceeded'}}})
        self.assertIsNone(result.verdict)
        self.assertEqual(result.unavailable, 'quota')

    def test_capacity_outage_needs_a_typed_provider_error(self):
        message = 'Selected model is at capacity. Please try a different model.'
        down = self.codex(1, {'codex': {'status': 1, 'stderr': message}})
        self.assertIsNone(down.unavailable)
        typed = self.codex(1, {'codex': {'status': 1, 'error': {'type': 'overloaded_error'}}})
        self.assertEqual(typed.unavailable, 'service')
        incomplete = self.codex(1, {'codex': {'status': 1, 'stdout': message}})
        self.assertIsNone(incomplete.unavailable)

    def test_native_structured_quota_error_survives_empty_review_streams(self):
        # executeReviewRun forwards runAppServerReview.error into codex.error; --json has no progress stderr.
        error = {'code': 'usage_limit_reached', 'message': 'Usage cap', 'reset_at': '2026-11-04T12:00:00Z'}
        envelope = {'review': 'Review', 'threadId': 'review-thread', 'sourceThreadId': 'source-thread',
                    'codex': {'status': 1, 'stdout': '', 'stderr': '', 'error': error}}
        result = self.codex(1, envelope)
        self.assertIsNone(result.verdict)
        self.assertEqual(result.unavailable, 'quota')
        self.assertIn('2026-11-04T12:00:00Z', result.text)
        self.assertIn('usage_limit_reached', result.text)

    def test_failed_codex_partial_blocker_cannot_become_an_outage(self):
        for text in ('[P1] Unsafe delete', '[P0] Broken protection', 'VERDICT: BLOCK'):
            with self.subTest(text=text):
                envelope = {'codex': {'status': 1, 'stdout': text, 'stderr': '',
                                      'error': {'message': 'Usage cap', 'codexErrorInfo': 'usageLimitExceeded'}}}
                result = self.codex(1, envelope)
                self.assertEqual(result.verdict, 'BLOCK')
                self.assertIsNone(result.unavailable)
                self.assertIn('Interrupted', result.text)
                self.assertIn(text, result.text)

    def test_native_codex_error_info_quota_is_recognized(self):
        error = {'message': 'Usage cap', 'codexErrorInfo': 'usageLimitExceeded'}
        result = self.codex(1, {'codex': {'status': 1, 'stdout': '', 'stderr': '', 'error': error}})
        self.assertEqual(result.unavailable, 'quota')

    def test_generic_structured_errors_timeouts_and_bad_status_do_not_enable_fallback(self):
        cases = [(1, 1, {'code': 'internal_error', 'message': 'Failed to read local configuration'}),
                 (124, 1, {'code': 'usage_limit_reached', 'message': 'Usage cap'}),
                 (1, None, {'code': 'usage_limit_reached', 'message': 'Usage cap'}),
                 (1, False, {'code': 'usage_limit_reached', 'message': 'Usage cap'})]
        for code, status, error in cases:
            with self.subTest(code=code, status=status):
                result = self.codex(code, {'codex': {'status': status, 'stdout': '', 'stderr': '', 'error': error}})
                self.assertIsNone(result.verdict)
                self.assertIsNone(result.unavailable)

    def test_missing_plugin_or_node_is_a_local_error(self):
        with patch.object(runner.os.path, 'isfile', return_value=False):
            self.assertIsNone(runner.codex_review('origin/main', False).unavailable)
        with patch.object(runner.os.path, 'isfile', return_value=True), \
                patch.object(runner.shutil, 'which', return_value=None):
            self.assertIsNone(runner.codex_review('origin/main', False).unavailable)

    def test_missing_codex_cli_can_use_fallback(self):
        with patch.object(runner.os.path, 'isfile', return_value=True), \
                patch.object(runner.shutil, 'which', side_effect=['/bin/node', None]):
            self.assertEqual(runner.codex_review('origin/main', False).unavailable, 'missing_cli')

    def test_claude_outages_keep_reason_and_reset_text(self):
        examples = [('rate_limit_error', 'quota', 'Quota resets Nov 4 at 1pm'),
                    ('authentication_error', 'auth', 'Sign in is required'),
                    ('overloaded_error', 'service', 'Service unavailable')]
        for error_type, reason, message in examples:
            error = json.dumps({'type': 'error', 'error': {'type': error_type, 'message': message}})
            with self.subTest(reason=reason), patch.object(runner, 'sh', return_value=completed(4, err=error)):
                result = runner.claude_review('origin/main')
                self.assertEqual(result.unavailable, reason)
                self.assertIn(message, result.text)
                self.assertIsNone(result.verdict)

    def test_wrapper_marker_alone_does_not_hide_a_typed_outage(self):
        # claude-review.sh prints only its (saved to ...) line when the CLI wrote nothing to stdout
        error = json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'Quota resets at 1pm'}})
        with patch.object(runner, 'sh', return_value=completed(4, '(saved to /tmp/claude-review-abc.x1)\n', error)):
            result = runner.claude_review('origin/main')
        self.assertEqual(result.unavailable, 'quota')
        self.assertIsNone(result.verdict)

    def test_failed_claude_partial_blocker_cannot_become_an_outage(self):
        for partial in ('VERDICT: BLOCK', '[P1] Unsafe delete', '[P0] Broken protection'):
            for text in (partial + "\nYou've hit your weekly limit", "You've hit your weekly limit\n" + partial):
                with self.subTest(text=text), patch.object(runner, 'sh', return_value=completed(4, text)):
                    result = runner.claude_review('origin/main')
                    self.assertEqual(result.verdict, 'BLOCK')
                    self.assertIsNone(result.unavailable)
                    self.assertIn('Interrupted', result.text)
                    self.assertIn(partial, result.text)

    def test_timeout_base_error_and_script_failure_do_not_allow_fallback(self):
        for code, text in ((124, 'timed out: claude'), (3, 'claude-review: base origin/main not found'),
                           (3, 'Traceback: bad script'), (124, 'service unavailable'),
                           (3, 'The service unavailable branch catches wrong exception; review incomplete')):
            with self.subTest(text=text), patch.object(runner, 'sh', return_value=completed(code, text)):
                self.assertIsNone(runner.claude_review('origin/main').unavailable)

    def test_successful_review_discussing_outages_is_still_a_review(self):
        with patch.object(runner, 'sh', return_value=completed(0, 'Handle service unavailable.\nVERDICT: PASS\n')):
            result = runner.claude_review('origin/main')
            self.assertEqual(result.verdict, 'PASS')
            self.assertIsNone(result.unavailable)


class CommentRedaction(unittest.TestCase):
    def record(self, command=CMD):
        return {'run_id': '1' * 32, 'head': HEAD, 'base': 'master', 'base_oid': HEAD, 'status': 'PASS',
                'providers': {'Codex': 'PASS', 'Claude': 'UNAVAILABLE'}, 'reasons': {'Claude': 'quota'},
                'tests': {'status': 'PASS', 'count': 1, 'exit_code': 0, 'command': command}}

    def publish(self, body):
        posted = []
        def write(cmd, **kwargs):
            posted.append(Path(cmd[cmd.index('--body-file') + 1]).read_text())
            return completed()
        with patch.object(runner, 'unchanged'), patch.object(runner, 'sh', side_effect=write), \
                contextlib.redirect_stdout(io.StringIO()):
            runner.post_comment('7', META, body)
        return posted

    def publish_record(self, title, record, detail=''):
        posted = []
        def write(cmd, **kwargs):
            posted.append(Path(cmd[cmd.index('--body-file') + 1]).read_text())
            return completed()
        with patch.object(runner, 'unchanged'), patch.object(runner, 'sh', side_effect=write), \
                contextlib.redirect_stdout(io.StringIO()):
            if title == runner.RUN_TITLE:
                runner.post_run('7', META, record)
            else:
                runner.post_availability('7', META, record, detail)
        return posted

    def test_receipt_json_and_exact_command_survive_prose_redaction(self):
        command = ['pytest', '-k', 'a"quoted-name']
        data = self.record(command)
        for title, marker in ((runner.RUN_TITLE, runner.RUN_MARK), (runner.EVIDENCE_TITLE, runner.EVIDENCE_MARK)):
            with self.subTest(title=title):
                posted = self.publish_record(title, data, 'password=FakeValue')[0]
                record = json.loads(posted.splitlines()[1][len(marker):])
                self.assertEqual(record, data)
                self.assertEqual(record['tests']['command'], command)
                self.assertNotIn('FakeValue', posted)

    def test_credential_bearing_receipts_are_rejected_before_posting(self):
        for argument in ('API_KEY=FakeValue', '--api-key=FakeValue'):
            for marker, title in ((runner.RUN_MARK, runner.RUN_TITLE), (runner.EVIDENCE_MARK, runner.EVIDENCE_TITLE)):
                raw = json.dumps(self.record(['env', argument, 'pytest']), separators=(',', ':'))
                for data in (raw, raw.replace('API_KEY', r'API_\u004bEY')):
                    with self.subTest(argument=argument, marker=marker), \
                            patch.object(runner, 'unchanged'), patch.object(runner, 'sh') as publish, \
                            contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'credential') as error:
                        if title == runner.RUN_TITLE:
                            runner.post_run('7', META, json.loads(data))
                        else:
                            runner.post_availability('7', META, json.loads(data), '')
                    publish.assert_not_called()
                    self.assertNotIn('FakeValue', str(error.exception))

    def test_marker_shaped_model_prose_never_uses_the_record_path(self):
        body = ('## Codex review\nhead: ' + HEAD + '\n\n' + runner.RUN_TITLE + '\n' + runner.RUN_MARK
                + json.dumps(self.record(['env', 'API_KEY=FakeValue', 'pytest'])) + '\n')
        with patch.object(runner, 'post_record') as structured:
            posted = self.publish(body)[0]
        structured.assert_not_called()
        self.assertNotIn('FakeValue', posted)
        self.assertIn('[REDACTED]', posted)

    def test_an_unclosed_credential_label_cannot_swallow_the_verdict_line(self):
        for prose in ('Fix the token: `', 'Fix the token:', 'password=`\nsee above', "key: '"):
            with self.subTest(prose=prose):
                body = runner.verdict_comment('Claude review', HEAD, prose, 'BLOCK', 'run-1')
                posted = []
                with patch.object(runner, 'publish_body', side_effect=lambda pr, meta, text: posted.append(text)):
                    runner.post_comment('8', {}, body)
                self.assertTrue(posted[0].endswith('\nVERDICT: BLOCK\n'), posted[0])
                self.assertIn('head: ' + HEAD + '\nrun: run-1\n', posted[0])

    def test_token_crossing_comment_cutoff_is_redacted_before_truncation(self):
        secret = 'ghp_' + 'a' * 64
        text = 'x' * 64 + '\n' + secret + '\n' + 'remaining text ' * 20
        with patch.object(runner, 'MAX_TEXT', 80):
            body = runner.verdict_comment('Codex review', HEAD, text, 'PASS')
        self.assertIn('[REDACTED]', body)
        self.assertNotIn('ghp_', body)
        self.assertNotIn(secret, body)
        self.assertIn('[review truncated', body)
        self.assertTrue(body.endswith('VERDICT: PASS\n'))


class ClaudeWrapper(unittest.TestCase):
    def test_model_stdout_and_cli_error_stderr_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / 'claude'
            cli.write_text('#!/bin/sh\nprintf "Model review prose\\n"\n'
                           'printf \'{"type":"error","error":{"type":"rate_limit_error"}}\\n\' >&2\nexit 1\n')
            cli.chmod(0o755)
            out = subprocess.run(['bash', str(CORE / 'bin/claude-review.sh'), 'HEAD'], cwd=CORE,
                                 env=dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH']),
                                 text=True, capture_output=True, timeout=20)
        self.assertEqual(out.returncode, 4)
        self.assertIn('Model review prose', out.stdout)
        self.assertNotIn('rate_limit_error', out.stdout)
        self.assertEqual(json.loads(out.stderr)['error']['type'], 'rate_limit_error')

    def test_provider_failure_is_distinct_from_successful_incomplete_prose(self):
        cases = [(0, 'The service unavailable branch catches wrong exception; review incomplete', 3),
                 (1, "You've hit your weekly limit", 4),
                 (0, 'VERDICT: PASS', 0), (0, 'VERDICT: BLOCK', 1),
                 (1, 'VERDICT: PASS', 4)]
        for cli_exit, text, expected in cases:
            with self.subTest(cli_exit=cli_exit, text=text), tempfile.TemporaryDirectory() as tmp:
                cli = Path(tmp) / 'claude'
                cli.write_text('#!/bin/sh\nprintf \'%s\\n\' ' + json.dumps(text) + '\nexit ' + str(cli_exit) + '\n')
                cli.chmod(0o755)
                out = subprocess.run(['bash', str(CORE / 'bin' / 'claude-review.sh'), 'HEAD'],
                                     cwd=CORE, env=dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH']),
                                     capture_output=True, text=True, timeout=20)
                self.assertEqual(out.returncode, expected, out.stdout + out.stderr)


def typed_line(raw, **changes):
    """The diagnostic claude-review.sh prints on stderr for a failed JSON run, built from a real CLI result."""
    data = json.loads(raw)
    data.update(changes)
    keys = ('type', 'is_error', 'terminal_reason', 'api_error_status', 'api_error_origin', 'subtype')
    line = {k: data.get(k) for k in keys}
    line['result'] = data.get('result', '')[:500]
    return json.dumps(line, separators=(',', ':'))


class ClaudeTypedOutage(unittest.TestCase):
    def review(self, stderr, stdout='', code=4):
        with patch.object(runner.shutil, 'which', return_value='/bin/claude'), \
                patch.object(runner, 'sh', return_value=completed(code, stdout, stderr)):
            return runner.claude_review('origin/main')

    def test_real_401_is_auth_and_keeps_the_message(self):
        result = self.review(typed_line(REAL_AUTH_401), 'Invalid API key \u00b7 Fix external API key\n')
        self.assertEqual(result.unavailable, 'auth')
        self.assertIsNone(result.verdict)
        self.assertIn('Invalid API key', result.text)

    def test_status_maps_to_outage_kind(self):
        cases = {429: 'quota', 401: 'auth', 403: None, 500: 'service', 502: 'service', 503: 'service',
                 504: 'service', 529: 'service', 404: None, 400: None, 413: None, 418: None, None: None}
        for status, reason in cases.items():
            with self.subTest(status=status):
                self.assertEqual(self.review(typed_line(REAL_AUTH_401, api_error_status=status)).unavailable, reason)

    def test_short_outage_prose_from_a_finished_review_is_not_an_outage(self):
        # a review that exited 0, 1 or 3 is a verdict or a local error, whatever its text says
        for code in (0, 1, 3):
            with self.subTest(code=code):
                self.assertIsNone(self.review('', "You've hit your weekly limit\n", code=code).unavailable)

    def test_a_long_typed_stderr_error_still_counts(self):
        err = json.dumps({'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'x' * 900}})
        self.assertEqual(self.review(err).unavailable, runner.codex_error_reason({'type': 'overloaded_error'}))

    def test_real_404_bad_model_fails_closed(self):
        result = self.review(typed_line(REAL_MODEL_404), 'There is an issue with the selected model\n')
        self.assertIsNone(result.unavailable)
        self.assertIsNone(result.verdict)

    def test_only_an_api_error_that_is_flagged_as_error_counts(self):
        for changes in ({'terminal_reason': 'max_turns'}, {'terminal_reason': 'completed'},
                        {'terminal_reason': 'budget_exceeded'}, {'is_error': False}, {'is_error': None}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.review(typed_line(REAL_AUTH_401, **changes)).unavailable)

    def test_typed_fields_win_over_prose_that_looks_like_an_outage(self):
        # a 404 whose message happens to match the old text patterns must still fail closed
        line = typed_line(REAL_MODEL_404, result="You've hit your weekly limit")
        self.assertIsNone(self.review(line, "You've hit your weekly limit\n").unavailable)

    def test_typed_outage_cannot_hide_a_blocking_partial_review(self):
        result = self.review(typed_line(REAL_AUTH_401), '[P1] Unsafe delete\n')
        self.assertEqual(result.verdict, 'BLOCK')
        self.assertIsNone(result.unavailable)

    def test_typed_line_only_counts_on_a_failed_provider_exit(self):
        for code in (0, 1, 3, 124):
            with self.subTest(code=code):
                self.assertIsNone(self.review(typed_line(REAL_AUTH_401), 'Review text.\n', code=code).unavailable)

    def test_cli_built_failure_without_an_api_status_uses_its_message(self):
        # a subscription limit the CLI reports itself carries is_error but no api_error_status
        line = typed_line(REAL_AUTH_401, terminal_reason=None, api_error_status=None, result="You've hit your weekly limit")
        self.assertEqual(self.review(line, "You've hit your weekly limit\n").unavailable, 'quota')
        quiet = typed_line(REAL_AUTH_401, terminal_reason=None, api_error_status=None, result='Something went wrong')
        self.assertIsNone(self.review(quiet, 'Something went wrong\n').unavailable)

    def test_cli_built_fallback_keeps_the_final_cases_final(self):
        limit = "You've hit your weekly limit"
        for changes in ({'terminal_reason': 'budget_exceeded'}, {'terminal_reason': 'max_turns'},
                        {'terminal_reason': 'completed'}, {'terminal_reason': None, 'api_error_status': 404}):
            with self.subTest(changes=changes):
                line = typed_line(REAL_AUTH_401, **dict({'api_error_status': None, 'result': limit}, **changes))
                self.assertIsNone(self.review(line, limit + '\n').unavailable)
        # a long partial review that mentions a rate limit is not an outage, even though the typed copy is short
        long_review = 'Partial review. ' * 40 + 'rate limit exceeded while reading the diff'
        line = typed_line(REAL_AUTH_401, terminal_reason=None, api_error_status=None, result=long_review)
        self.assertIsNone(self.review(line, long_review + '\n').unavailable)

    def test_malformed_and_noninteger_statuses_do_not_establish_an_outage(self):
        for status in ('429', True, 429.0, [], {}):
            with self.subTest(status=status):
                self.assertIsNone(self.review(typed_line(REAL_AUTH_401, api_error_status=status)).unavailable)
        for raw in ('[]', '{broken json', '{"type":"error","error":{"message":"rate limit exceeded"}}'):
            with self.subTest(raw=raw):
                self.assertIsNone(self.review(raw).unavailable)

    def test_warnings_do_not_hide_a_typed_failure_or_override_a_final_result(self):
        for status, expected in ((429, 'quota'), (404, None)):
            with self.subTest(status=status):
                line = typed_line(REAL_AUTH_401, api_error_status=status)
                result = self.review('warning before\n' + line + '\nwarning after\n')
                self.assertEqual(result.unavailable, expected)

    def test_non_json_stderr_keeps_the_text_fallback(self):
        self.assertEqual(self.review('', "You've hit your weekly limit").unavailable, 'quota')
        self.assertEqual(self.review('some warning\nnot json {', 'Not logged in. Please log in.').unavailable, 'auth')


class ClaudeJsonWrapper(unittest.TestCase):
    """bin/claude-review.sh end to end with a stub `claude` that prints a canned JSON object."""

    def run_script(self, stdout, cli_exit=0, stderr='', **extra_env):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / 'stdout').write_text(stdout)
            (d / 'stderr').write_text(stderr)
            cli = d / 'claude'
            cli.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$STUB_DIR/args"\ncat > /dev/null\n'
                           'cat "$STUB_DIR/stdout"\ncat "$STUB_DIR/stderr" >&2\nexit ' + str(cli_exit) + '\n')
            cli.chmod(0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'], TMPDIR=tmp, STUB_DIR=tmp)
            env.pop('AGENT_CORE_CLAUDE_REVIEW_MODEL', None)
            env.update(extra_env)
            out = subprocess.run(['bash', str(CORE / 'bin' / 'claude-review.sh'), 'HEAD'], cwd=CORE, env=env,
                                 capture_output=True, text=True, timeout=30)
            args = d / 'args'
            out.args_seen = args.read_text().splitlines() if args.exists() else []
            out.leftovers = sorted(p.name for p in d.iterdir() if p.name.startswith('claude-review-raw')
                                   or p.name.startswith('claude-review-err'))
            return out

    def success(self, text):
        return json.dumps(dict(json.loads(REAL_OK), result=text))

    def test_requests_json_and_keeps_the_tool_lockdown(self):
        out = self.run_script(self.success('Fine.\nVERDICT: PASS'))
        args = out.args_seen
        self.assertEqual(args[args.index('--output-format') + 1], 'json')
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertIn('--strict-mcp-config', args)
        self.assertEqual(args[args.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(args[args.index('--setting-sources') + 1], '')
        self.assertEqual(out.leftovers, [])

    def test_model_defaults_to_opus_and_takes_fable(self):
        out = self.run_script(self.success('Fine.\nVERDICT: PASS'))
        self.assertEqual(out.args_seen[out.args_seen.index('--model') + 1], 'opus')
        out = self.run_script(self.success('Fine.\nVERDICT: PASS'), AGENT_CORE_CLAUDE_REVIEW_MODEL='fable')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.args_seen[out.args_seen.index('--model') + 1], 'fable')

    def test_a_lower_or_unknown_model_is_refused_without_calling_claude(self):
        for model in ('sonnet', 'haiku', 'opus --tools Bash', ''):
            with self.subTest(model=model):
                out = self.run_script(self.success('Fine.\nVERDICT: PASS'), AGENT_CORE_CLAUDE_REVIEW_MODEL=model)
                if model == '':
                    self.assertEqual(out.args_seen[out.args_seen.index('--model') + 1], 'opus')
                    continue
                self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
                self.assertEqual(out.args_seen, [])
                self.assertIn('must be opus or fable', out.stdout)

    def test_success_json_pass_and_block_come_from_the_result_text(self):
        for verdict, code in (('PASS', 0), ('BLOCK', 1)):
            with self.subTest(verdict=verdict):
                out = self.run_script(self.success('Looked at it.\nVERDICT: ' + verdict))
                self.assertEqual(out.returncode, code, out.stdout + out.stderr)
                self.assertIn('Looked at it.', out.stdout)
                self.assertNotIn('"terminal_reason"', out.stdout)
                self.assertIn('(saved to ', out.stdout)

    def test_success_json_without_a_verdict_is_no_verdict(self):
        self.assertEqual(self.run_script(self.success('No verdict line here.')).returncode, 3)

    def test_successful_review_mentioning_quota_is_not_a_failure(self):
        out = self.run_script(self.success("Handle the rate_limit_error path.\nVERDICT: PASS"))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_real_error_shapes_exit_4_with_one_typed_stderr_line(self):
        for raw, status in ((REAL_AUTH_401, 401), (REAL_MODEL_404, 404)):
            with self.subTest(status=status):
                out = self.run_script(raw, cli_exit=1)
                self.assertEqual(out.returncode, 4, out.stdout + out.stderr)
                typed = [json.loads(line) for line in out.stderr.splitlines() if line.startswith('{')]
                self.assertEqual(len(typed), 1, out.stderr)
                self.assertEqual(sorted(typed[0]), sorted(['type', 'is_error', 'terminal_reason', 'api_error_status',
                                                           'api_error_origin', 'subtype', 'result']))
                self.assertEqual(typed[0]['api_error_status'], status)
                self.assertEqual(typed[0]['terminal_reason'], 'api_error')
                self.assertIs(typed[0]['is_error'], True)
                self.assertNotIn('session_id', out.stderr)

    def test_error_result_is_cut_to_500_characters(self):
        raw = json.dumps(dict(json.loads(REAL_AUTH_401), result='x' * 900))
        out = self.run_script(raw, cli_exit=1)
        typed = [json.loads(line) for line in out.stderr.splitlines() if line.startswith('{')]
        self.assertEqual(len(typed[0]['result']), 500)

    def test_is_error_with_a_zero_exit_still_fails_and_never_passes(self):
        raw = json.dumps(dict(json.loads(REAL_AUTH_401), result='Fine.\nVERDICT: PASS'))
        self.assertEqual(self.run_script(raw, cli_exit=0).returncode, 4)

    def test_nonzero_exit_with_successful_json_is_a_failure(self):
        self.assertEqual(self.run_script(self.success('Fine.\nVERDICT: PASS'), cli_exit=1).returncode, 4)

    def test_script_output_classifies_end_to_end(self):
        for raw, reason in ((REAL_AUTH_401, 'auth'), (REAL_MODEL_404, None),
                            (json.dumps(dict(json.loads(REAL_AUTH_401), api_error_status=429)), 'quota'),
                            (json.dumps(dict(json.loads(REAL_AUTH_401), api_error_status=529)), 'service')):
            with self.subTest(reason=reason, raw=raw[-120:]):
                out = self.run_script(raw, cli_exit=1)
                with patch.object(runner.shutil, 'which', return_value='/bin/claude'), \
                        patch.object(runner, 'sh', return_value=completed(out.returncode, out.stdout, out.stderr)):
                    result = runner.claude_review('origin/main')
                self.assertIsNone(result.verdict)
                self.assertEqual(result.unavailable, reason)

    def test_non_json_stdout_is_printed_raw_with_todays_exit_codes(self):
        for cli_exit, text, expected in ((0, 'VERDICT: PASS', 0), (0, 'VERDICT: BLOCK', 1), (0, 'prose only', 3),
                                         (1, "You've hit your weekly limit", 4), (1, 'VERDICT: PASS', 4)):
            with self.subTest(text=text, cli_exit=cli_exit):
                out = self.run_script(text + '\n', cli_exit=cli_exit)
                self.assertEqual(out.returncode, expected, out.stdout + out.stderr)
                self.assertIn(text, out.stdout)

    def test_non_json_stderr_stays_on_the_error_stream(self):
        out = self.run_script('', cli_exit=1, stderr='Not logged in. Please log in.\n')
        self.assertEqual(out.returncode, 4)
        self.assertIn('Not logged in', out.stderr)
        self.assertNotIn('Not logged in', out.stdout)


class TestExecution(unittest.TestCase):
    def test_reviewed_revision_is_passed_to_the_worker(self):
        with patch.object(runner, 'checkout_root', return_value=str(CORE)), \
                patch.object(runner.review_worker, 'execute', return_value=completed(0, 'Ran 1 test in 0.1s\nOK')) as work, \
                contextlib.redirect_stdout(io.StringIO()):
            runner.run_tests(CMD, 'acme/kit', HEAD)
        work.assert_called_once_with(str(CORE), 'acme/kit', CMD, HEAD)

    def test_long_pytest_duration_is_a_completed_positive_suite(self):
        for text in ('=== 345 passed in 95.03s (0:01:35) ===',
                     '345 passed, 2 skipped in 3661.01s (1:01:01)',
                     '345 passed in 95.03s (01:35)'):
            with self.subTest(text=text):
                self.assertEqual(runner.test_count(text), 345)

    def setUp(self):
        # The fixtures exercise real test output and cwd without starting Docker recursively inside the suite.
        def fixture_worker(root, slug, command, head):
            return subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
        worker = patch.object(runner.review_worker, 'execute', side_effect=fixture_worker)
        self.worker = worker.start()
        self.addCleanup(worker.stop)

    def test_subdirectory_invocation_runs_the_full_checkout_suite(self):
        for slug in ('acme/kit', 'owner/another-repo'):
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                subprocess.run(['git', 'init', '-q', str(root)], check=True)
                package = root / 'package'
                package.mkdir()
                (package / '__init__.py').write_text('')
                (package / 'test_package.py').write_text(
                    'import unittest\nclass Package(unittest.TestCase):\n def test_pass(self): pass\n')
                (root / 'test_root.py').write_text(
                    'import unittest\nclass Root(unittest.TestCase):\n'
                    ' def test_fail(self): self.fail("root failure must prevent fallback")\n')
                previous = os.getcwd()
                try:
                    os.chdir(package)
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = runner.run_tests([sys.executable, '-m', 'unittest', 'discover'], slug, HEAD)
                finally:
                    os.chdir(previous)
                self.assertEqual(result['status'], 'FAIL')
                self.assertEqual(result['exit_code'], 1)
                self.assertEqual(result['count'], 0)

    def test_tests_without_a_git_checkout_cannot_supply_fallback_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            try:
                os.chdir(tmp)
                with self.assertRaisesRegex(ValueError, 'checkout root'), \
                        contextlib.redirect_stdout(io.StringIO()):
                    runner.run_tests([sys.executable, '-c', 'print("1 passed")'], 'owner/another-repo', HEAD)
            finally:
                os.chdir(previous)

    def test_real_test_command_supplies_positive_count_and_actual_exit(self):
        command = [sys.executable, '-c',
                   'import unittest; suite=unittest.TestSuite([unittest.FunctionTestCase(lambda: None)]); '
                   'unittest.TextTestRunner().run(suite)']
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.run_tests(command, 'acme/kit', HEAD)
        self.assertEqual(result, {'status': 'PASS', 'count': 1, 'command': command, 'exit_code': 0})

    def test_failed_and_empty_suites_cannot_supply_passing_evidence(self):
        for code, text in ((1, 'Ran 3 tests in 0.01s\nFAILED'), (0, 'Ran 0 tests in 0.00s\nOK'), (0, 'no tests found')):
            with self.subTest(text=text), patch.object(runner, 'checkout_root', return_value=str(CORE)), \
                    patch.object(runner.review_worker, 'execute', return_value=completed(code, text)), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = runner.run_tests(CMD, 'acme/kit', HEAD)
                self.assertEqual(result['status'], 'FAIL')
                self.assertEqual(result['exit_code'], code)

    def test_unittest_count_excludes_skips_and_expected_failures(self):
        for total, completion, passed in ((4, 'OK (skipped=4)', 0),
                                           (3, 'OK (expected failures=3)', 0),
                                           (5, 'OK (skipped=3, expected failures=2)', 0),
                                           (5, 'OK (skipped=2, expected failures=1)', 2),
                                           (7, 'OK', 7),
                                           (4, 'FAILED (failures=1)', 0)):
            text = 'Ran %d tests in 0.001s\n\n%s\n' % (total, completion)
            with self.subTest(text=text):
                self.assertEqual(runner.test_count(text), passed)
        self.assertEqual(runner.test_count('Ran 5 tests in 0.001s\n'), 0)

    def test_all_skipped_success_exit_cannot_supply_passing_evidence(self):
        script = ('import unittest\n'
                  'class Skipped(unittest.TestCase):\n'
                  ' @unittest.skip("offline")\n'
                  ' def test_case(self): pass\n'
                  'unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Skipped))')
        command = [sys.executable, '-c', script]
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.run_tests(command, 'acme/kit', HEAD)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['status'], 'FAIL')

    def test_unrelated_output_does_not_certify_passing_tests(self):
        for text in ('preflight: 2 passed checks\nNo tests found, exiting with code 0',
                     'Warning: previously 8 passed', '=== 0 passed in 1.2s ==='):
            self.assertEqual(runner.test_count(text), 0)

    def test_pytest_and_javascript_summaries(self):
        for text in ('=== 17 passed in 1.2s ===', 'Tests: 9 passed, 9 total', ' Tests  13 passed (13)'):
            with self.subTest(text=text):
                self.assertGreater(runner.test_count(text), 0)

    def test_snapshot_is_the_only_host_mount_in_an_offline_worker(self):
        command = runner.review_worker.worker_args(['docker'], Path('/scratch/objects.git'), HEAD,
                                                   'probe', 'owner/repo', CMD)
        self.assertIn('--network=none', command)
        self.assertIn('--read-only', command)
        self.assertIn('--cap-drop=ALL', command)
        self.assertIn('--security-opt=no-new-privileges', command)
        self.assertEqual(command.count('--mount'), 1)
        self.assertEqual(command[command.index('--mount') + 1],
                         'type=bind,source=/scratch/objects.git,target=/snapshot,readonly')
        self.assertNotIn('AGENT_CORE_LOCAL', command)
        self.assertEqual(command[-len(CMD):], CMD)

    def test_missing_worker_never_executes_pr_code_on_host(self):
        with patch.object(runner, 'checkout_root', return_value='/repo'), \
                patch.object(runner.review_worker, 'execute', side_effect=ValueError('no worker')), \
                patch.object(runner, 'sh') as host, self.assertRaisesRegex(ValueError, 'no worker'):
            runner.run_tests(CMD, 'owner/project', HEAD)
        host.assert_not_called()


class PinnedWorker(unittest.TestCase):
    def test_worker_snapshots_the_reviewed_object_without_resampling_head(self):
        calls = []
        def trusted(args, **kwargs):
            calls.append(args)
            output = 'unix:///private/test-docker.sock' if args[:3] == ('docker', 'context', 'inspect') else ''
            return subprocess.CompletedProcess(args, 0, output, '')
        read, write = os.pipe()
        os.write(write, b'1 passed in 0.1s\n')
        os.close(write)
        child = MagicMock()
        child.stdout = os.fdopen(read, 'rb')
        child.wait.return_value = 0
        child.__enter__.return_value = child
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [(None, None)]
        try:
            with patch.object(runner.review_worker.subprocess, 'run', side_effect=trusted), \
                    patch.object(runner.review_worker.subprocess, 'Popen', return_value=child) as launch, \
                    patch.object(runner.review_worker.selectors, 'DefaultSelector', return_value=selector):
                result = runner.review_worker.execute('/repo', 'acme/kit', CMD, HEAD)
        finally:
            child.stdout.close()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(calls)
        self.assertFalse(any('HEAD^{commit}' in args for args in calls))
        fetches = [args for args in calls if 'fetch' in args]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0][-1], HEAD)
        self.assertIn(HEAD, launch.call_args.args[0])

    def test_worker_rejects_an_unverified_revision_before_any_local_commands(self):
        with patch.object(runner.review_worker.subprocess, 'run') as command, \
                self.assertRaisesRegex(ValueError, 'reviewed commit'):
            runner.review_worker.execute('/repo', 'acme/kit', CMD, 'HEAD')
        command.assert_not_called()


class AutomaticRun(unittest.TestCase):
    def test_credential_options_in_test_commands_are_rejected_before_publication(self):
        for command in (['env', 'API_KEY=FakeValue', 'pytest'], ['pytest', '--api-key=FakeValue'],
                        ['pytest', '--api-key', 'FakeValue'], ['pytest', '--apiKey', 'FakeValue'],
                        ['pytest', '--accessToken', 'FakeValue'], ['pytest', '--clientSecret', 'FakeValue']):
            with self.subTest(command=command):
                self.assertEqual(self.run_main(command=command), 3)
                self.assertEqual(self.posts, [])
                self.test_call.assert_not_called()

    def run_main(self, codex=None, claude=None, tests=None, policy=True, command=CMD, mode='auto'):
        self.posts = []
        self.codex = codex or runner.Review('PASS', 'No findings')
        self.claude = claude or runner.Review(None, "You've hit your weekly limit; resets tomorrow", 'quota')
        self.tests = tests or {'status': 'PASS', 'count': 10, 'command': CMD, 'exit_code': 0}
        with patch.object(runner, 'metadata', return_value=META), \
                patch.object(runner, 'unchanged'), \
                patch.object(runner, 'sh', return_value=completed(0, HEAD)), \
                patch.object(runner, 'codex_review', return_value=self.codex), \
                patch.object(runner, 'claude_review', return_value=self.claude), \
                patch.object(runner, 'run_tests', return_value=self.tests) as test_call, \
                patch.object(runner, 'persisted_blockers', return_value=[]), \
                patch.object(runner, 'publish_body', side_effect=lambda pr, meta, body: self.posts.append(body)), \
                patch.object(runner.common, 'config', return_value={'pr_gate': {'allow_single_on_unavailable': policy}}), \
                patch.object(runner.common, 'review_test_command', return_value=command, create=True), \
                contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(['7', mode])
        self.test_call = test_call
        return code

    def evidence(self):
        bodies = [body for body in self.posts if body.startswith(runner.EVIDENCE_TITLE)]
        return [json.loads(body.splitlines()[1][len(runner.EVIDENCE_MARK):]) for body in bodies]

    def test_manual_provider_commands_keep_their_existing_behavior(self):
        self.assertEqual(self.run_main(mode='codex'), 0)
        self.assertEqual(len(self.posts), 1)
        self.assertIn('## Codex review', self.posts[0])
        self.test_call.assert_not_called()
        self.assertEqual(self.run_main(mode='claude'), 3)
        self.assertEqual(self.posts, [])

    def test_one_pass_and_quota_outage_records_passing_test_evidence(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.posts), 4)
        evidence = self.evidence()[0]
        self.assertEqual(evidence['head'], HEAD)
        self.assertEqual(evidence['base'], 'master')
        self.assertEqual(evidence['provider'], 'Claude')
        self.assertEqual(evidence['reason'], 'quota')
        self.assertEqual(evidence['tests']['count'], 10)
        receipt = next(body for body in self.posts if body.startswith(runner.EVIDENCE_TITLE))
        self.assertNotIn('VERDICT:', receipt)
        self.assertIn('resets tomorrow', receipt)

    def test_reverse_provider_outage_is_supported(self):
        self.assertEqual(self.run_main(codex=runner.Review(None, 'Not signed in', 'auth'),
                                       claude=runner.Review('PASS', 'No findings')), 0)
        self.assertEqual(self.evidence()[0]['provider'], 'Codex')

    def test_two_passes_need_no_fallback_or_extra_test_run(self):
        self.assertEqual(self.run_main(claude=runner.Review('PASS', 'No findings')), 0)
        self.test_call.assert_not_called()
        self.assertEqual(self.evidence(), [])

    def test_block_is_never_replaced_by_outage_fallback(self):
        self.assertEqual(self.run_main(codex=runner.Review('BLOCK', '[P1] Delete bug')), 1)
        self.test_call.assert_not_called()
        self.assertEqual(self.evidence(), [])

    def test_interrupted_provider_rerun_posts_a_durable_block_instead_of_reusing_old_pass(self):
        # An earlier same-head PASS and receipt can exist on GitHub. The rerun must supersede it with BLOCK.
        envelope = {'codex': {'status': 1, 'stdout': '[P1] Unsafe deletion', 'stderr': '',
                              'error': {'codexErrorInfo': 'usageLimitExceeded', 'message': 'Usage cap'}}}
        with patch.object(runner.os.path, 'isfile', return_value=True), \
                patch.object(runner.shutil, 'which', return_value='/bin/tool'), \
                patch.object(runner, 'sh', side_effect=[completed(1, json.dumps(envelope)), completed()]):
            codex = runner.codex_review('origin/master', False)
        with patch.object(runner, 'sh', return_value=completed(4, "VERDICT: BLOCK\nYou've hit your weekly limit")):
            claude = runner.claude_review('origin/master')
        for name, opts in (('Codex', {'codex': codex}), ('Claude', {'claude': claude})):
            with self.subTest(provider=name):
                self.assertEqual(self.run_main(**opts), 1)
                bodies = [body for body in self.posts if body.startswith('## ' + name + ' review')]
                self.assertEqual(len(bodies), 1)
                self.assertIn('head: ' + HEAD, bodies[0])
                self.assertIn('Interrupted', bodies[0])
                self.assertTrue(bodies[0].endswith('VERDICT: BLOCK\n'))
                self.assertEqual(self.evidence(), [])
                self.test_call.assert_not_called()

    def test_two_outages_or_generic_error_do_not_allow_fallback(self):
        for codex in (runner.Review(None, 'Not signed in', 'auth'), runner.Review(None, 'Malformed output')):
            with self.subTest(codex=codex):
                self.assertEqual(self.run_main(codex=codex), 3)
                self.test_call.assert_not_called()
                self.assertEqual(self.evidence(), [])

    def test_failed_tests_publish_failure_to_supersede_old_receipts(self):
        tests = {'status': 'FAIL', 'count': 9, 'command': CMD, 'exit_code': 1}
        self.assertEqual(self.run_main(tests=tests), 3)
        self.assertEqual(self.evidence()[0]['tests'], tests)

    def test_disabled_policy_or_missing_test_command_holds_fallback(self):
        for opts in ({'policy': False}, {'command': None}):
            with self.subTest(opts=opts):
                self.assertEqual(self.run_main(**opts), 3)
                self.test_call.assert_not_called()
                self.assertEqual(self.evidence(), [])

    def test_stale_or_dirty_checkout_posts_no_review(self):
        with patch.object(runner, 'metadata', return_value=META), \
                patch.object(runner, 'unchanged', side_effect=ValueError('dirty or changed')), \
                patch.object(runner, 'publish_body') as post, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(['7', 'auto']), 3)
        post.assert_not_called()

    def test_check_after_reviews_rejects_new_commit_before_posting(self):
        with patch.object(runner, 'metadata', return_value=META), \
                patch.object(runner, 'unchanged', side_effect=[None, ValueError('head changed')]), \
                patch.object(runner, 'sh', return_value=completed(0, HEAD)), \
                patch.object(runner, 'codex_review', return_value=runner.Review('PASS', 'fine')), \
                patch.object(runner, 'claude_review', return_value=runner.Review('PASS', 'fine')), \
                patch.object(runner, 'publish_body') as post, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(['7', 'auto']), 3)
        self.assertFalse(any('VERDICT:' in call.args[2] for call in post.call_args_list))

    def test_head_change_during_tests_posts_no_availability_receipt(self):
        posts = []
        with patch.object(runner, 'metadata', return_value=META), \
                patch.object(runner, 'unchanged', side_effect=[None, None, None, ValueError('head changed')]), \
                patch.object(runner, 'sh', return_value=completed(0, HEAD)), \
                patch.object(runner, 'codex_review', return_value=runner.Review('PASS', 'fine')), \
                patch.object(runner, 'claude_review', return_value=runner.Review(None, 'quota', 'quota')), \
                patch.object(runner, 'run_tests', return_value={'status': 'PASS', 'count': 4, 'command': CMD, 'exit_code': 0}), \
                patch.object(runner, 'persisted_blockers', return_value=[]), \
                patch.object(runner, 'publish_body', side_effect=lambda pr, meta, body: posts.append(body)), \
                patch.object(runner.common, 'config', return_value={'pr_gate': {'allow_single_on_unavailable': True}}), \
                patch.object(runner.common, 'review_test_command', return_value=CMD, create=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(['7', 'auto']), 3)
        self.assertEqual(len([body for body in posts if body.startswith('## Codex review')]), 1)
        self.assertFalse(any(body.startswith(runner.EVIDENCE_TITLE) for body in posts))

    def test_fetch_failure_stops_before_provider_calls(self):
        with patch.object(runner, 'metadata', return_value=META), patch.object(runner, 'unchanged'), \
                patch.object(runner, 'sh', return_value=completed(1, err='fetch failed')), \
                patch.object(runner, 'codex_review') as review, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(['7', 'auto']), 3)
        review.assert_not_called()



class TestCommandConfiguration(unittest.TestCase):
    def test_configured_repository_keys_match_github_names_case_insensitively(self):
        with patch.object(runner.common, 'config', return_value={
                'pr_gate': {'test_commands': {'AcMe/Kit': CMD}}}):
            for slug in ('acme/kit', 'ACME/KIT', 'Acme/Kit'):
                with self.subTest(slug=slug):
                    self.assertEqual(runner.common.review_test_command(slug), CMD)


class SnapshotChecks(unittest.TestCase):
    def test_tracked_changes_and_local_head_changes_are_rejected(self):
        for head, status in (('b' * 40, ''), (HEAD, ' M bin/script.py'), (HEAD, '?? tests/test_hidden.py')):
            with self.subTest(head=head, status=status), \
                    patch.object(runner, 'sh', side_effect=[completed(0, head), completed(0, status)]):
                with self.assertRaises(ValueError):
                    runner.unchanged('7', META)

    def test_untracked_files_are_included_in_the_clean_source_check(self):
        with patch.object(runner, 'sh', side_effect=[completed(0, HEAD), completed(0, '?? new_module.py')]) as call:
            with self.assertRaises(ValueError):
                runner.unchanged('7', META)
        self.assertIn('--untracked-files=normal', call.call_args_list[1].args[0])

    def test_remote_head_and_base_changes_are_rejected(self):
        for changed in ({**META, 'headRefOid': 'b' * 40}, {**META, 'baseRefName': 'main'}):
            with self.subTest(changed=changed), \
                    patch.object(runner, 'sh', side_effect=[completed(0, HEAD), completed()]), \
                    patch.object(runner, 'metadata', return_value=changed):
                with self.assertRaises(ValueError):
                    runner.unchanged('7', META)

    def test_post_rechecks_snapshot_before_publishing(self):
        with patch.object(runner, 'unchanged', side_effect=ValueError('changed')), \
                patch.object(runner, 'sh') as call:
            with self.assertRaises(ValueError):
                runner.post_comment('7', META, 'body')
        call.assert_not_called()

    def test_malformed_remote_metadata_is_rejected(self):
        for data in ('bad JSON', '{}', json.dumps({**META, 'headRefOid': 'not a SHA'})):
            with self.subTest(data=data), patch.object(runner, 'sh', return_value=completed(0, data)):
                with self.assertRaises(ValueError):
                    runner.metadata('7')

    def test_clean_matching_snapshot_passes(self):
        with patch.object(runner, 'sh', side_effect=[completed(0, HEAD), completed()]), \
                patch.object(runner, 'metadata', return_value=META):
            runner.unchanged('7', META)


if __name__ == '__main__':
    unittest.main()
