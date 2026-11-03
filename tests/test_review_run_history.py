"""An automatic attempt supersedes old success before it can publish partial results."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_review_runner import runner, META, HEAD, CMD
import test_merge_gate as merge_tests

comment = merge_tests.comment

RUN_TITLE = '## Agent-core review run'
RUN_MARK = 'AGENT_CORE_REVIEW_RUN_V1: '


class AutomaticPublication(unittest.TestCase):
    def run_auto(self, codex=None, claude=None, tests=None, blockers=(), fail_block=False):
        self.posts = []
        codex = codex or runner.Review('PASS', 'No findings')
        claude = claude or runner.Review(None, 'Quota reset tomorrow', 'quota')
        tests = tests or {'status': 'PASS', 'count': 7, 'command': CMD, 'exit_code': 0}

        def post(pr, meta, body):
            self.posts.append(body)
            if fail_block and body.endswith('VERDICT: BLOCK\n'):
                raise ValueError('GitHub refused the blocker comment')

        with patch.object(runner, 'metadata', return_value=META), patch.object(runner, 'unchanged'), \
                patch.object(runner, 'fetch_base', return_value=HEAD), \
                patch.object(runner, 'codex_review', return_value=codex), \
                patch.object(runner, 'claude_review', return_value=claude), \
                patch.object(runner, 'run_tests', return_value=tests), \
                patch.object(runner, 'post_comment', side_effect=post), \
                patch.object(runner, 'persisted_blockers', return_value=list(blockers), create=True), \
                patch.object(runner.common, 'config', return_value={'pr_gate': {'allow_single_on_unavailable': True}}), \
                patch.object(runner.common, 'review_test_command', return_value=CMD), \
                contextlib.redirect_stdout(io.StringIO()):
            return runner.main(['7', 'auto'])

    def records(self):
        return [json.loads(body.splitlines()[1][len(RUN_MARK):])
                for body in self.posts if body.startswith(RUN_TITLE)]

    def test_attempt_is_published_before_any_provider_verdict(self):
        self.assertEqual(self.run_auto(), 0)
        self.assertTrue(self.posts[0].startswith(RUN_TITLE))
        self.assertEqual(self.records()[0]['status'], 'RUNNING')
        self.assertEqual(self.records()[-1]['status'], 'PASS')
        self.assertEqual(self.records()[0]['run_id'], self.records()[-1]['run_id'])
        self.assertEqual(self.records()[-1]['base_oid'], META['baseRefOid'])

    def test_known_blocker_posts_before_pass_even_when_github_rejects_it(self):
        self.assertEqual(self.run_auto(claude=runner.Review('BLOCK', '[P1] Unsafe'), fail_block=True), 3)
        verdict_posts = [body for body in self.posts if 'VERDICT:' in body]
        self.assertTrue(verdict_posts)
        self.assertTrue(verdict_posts[0].endswith('VERDICT: BLOCK\n'))
        self.assertFalse(any(body.endswith('VERDICT: PASS\n') for body in verdict_posts))
        self.assertEqual(self.records()[0]['status'], 'RUNNING')

    def test_incomplete_rerun_publishes_an_ineligible_outcome(self):
        self.assertEqual(self.run_auto(claude=runner.Review(None, 'Timed out')), 3)
        self.assertEqual(self.records()[-1]['status'], 'INCOMPLETE')

    def test_failed_tests_publish_a_failed_attempt(self):
        tests = {'status': 'FAIL', 'count': 0, 'command': CMD, 'exit_code': 1}
        self.assertEqual(self.run_auto(tests=tests), 3)
        self.assertEqual(self.records()[-1]['status'], 'FAIL')

    def test_unsuperseded_persisted_block_cannot_report_fallback_success(self):
        self.assertEqual(self.run_auto(blockers=['Claude']), 1)
        self.assertEqual(self.records()[-1]['status'], 'BLOCK')


class PersistedBlockers(unittest.TestCase):
    def persisted(self, comments, reviewers=('@reviewer',), **metadata):
        current = dict(META, comments=comments, **metadata)
        completed = merge_tests.subprocess.CompletedProcess([], 0, json.dumps(current), '')
        with patch.object(runner, 'sh', return_value=completed), \
                patch.object(runner.common, 'config', return_value={'pr_gate': {'reviewers': reviewers}}):
            return runner.persisted_blockers('7', META)

    def test_single_reviewer_string_is_normalized_like_the_guard(self):
        block = comment('Claude', 'BLOCK', login='reviewer')
        self.assertEqual(self.persisted([block], reviewers=' @Reviewer '), ['Claude'])

    def test_only_latest_trusted_head_verdict_can_supersede_a_block(self):
        block = comment('Claude', 'BLOCK', login='reviewer')
        self.assertEqual(self.persisted([block]), ['Claude'])
        for later in (comment('Claude', 'PASS', login='stranger'),
                      comment('Claude', 'PASS', head='b' * 40, login='reviewer')):
            self.assertEqual(self.persisted([block, later]), ['Claude'])
        self.assertEqual(self.persisted([block, comment('Claude', 'PASS', login='reviewer')]), [])

    def test_current_pr_base_and_head_are_required_before_success(self):
        for changed in ({'headRefOid': 'b' * 40}, {'baseRefOid': 'b' * 40}, {'baseRefName': 'other'}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'current PR'):
                self.persisted([], **changed)


class LatestAutomaticGate(unittest.TestCase):
    setUp = merge_tests.ProviderFallback.setUp
    pr = merge_tests.Fixture.pr
    decide = merge_tests.ProviderFallback.decide
    write_config = merge_tests.ProviderFallback.write_config
    evidence = merge_tests.ProviderFallback.evidence

    def record(self, status, run='1' * 32, **changes):
        data = {'run_id': run, 'status': status, 'head': HEAD, 'base': 'main', 'base_oid': HEAD,
                'providers': {'Codex': 'PASS', 'Claude': 'UNAVAILABLE'},
                'reasons': {'Claude': 'quota'},
                'tests': {'status': 'PASS', 'count': 12, 'exit_code': 0, 'command': self.command}}
        data.update(changes)
        return {'author': {'login': 'alex'}, 'body': RUN_TITLE + '\n' + RUN_MARK + json.dumps(data) + '\n'}

    def reviewed(self, who, verdict='PASS', run='1' * 32):
        result = comment(who, verdict)
        result['body'] = result['body'].replace('head: ' + HEAD, 'head: ' + HEAD + '\nrun: ' + run)
        return result

    def write_pr(self, comments):
        self.pr(comments=comments)
        path = Path(self.pr_json)
        data = json.loads(path.read_text())
        data['baseRefOid'] = HEAD
        path.write_text(json.dumps(data))

    def test_failed_latest_attempt_holds_even_two_historical_passes(self):
        for status in ('RUNNING', 'FAIL', 'INCOMPLETE'):
            with self.subTest(status=status):
                self.write_pr([comment('Codex', 'PASS'), comment('Claude', 'PASS'),
                               self.record('RUNNING'), self.record(status)])
                self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_legacy_failed_test_receipt_holds_two_historical_passes(self):
        self.write_pr([comment('Codex', 'PASS'), comment('Claude', 'PASS'),
                       self.evidence(status='FAIL', exit_code=1)])
        self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_same_run_pass_and_tests_allow_fallback(self):
        self.write_pr([self.record('RUNNING'), self.reviewed('Codex'), self.record('PASS')])
        self.assertIsNone(self.decide())

    def test_old_run_finishing_after_new_start_cannot_reopen_gate(self):
        self.write_pr([self.record('RUNNING'), self.reviewed('Codex'),
                       self.record('RUNNING', run='2' * 32), self.record('PASS')])
        self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_outcome_cannot_borrow_an_older_provider_pass(self):
        self.write_pr([comment('Codex', 'PASS'), self.record('RUNNING'), self.record('PASS')])
        self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_unsuperseded_block_still_holds_successful_attempt(self):
        self.write_pr([comment('Claude', 'BLOCK'), self.record('RUNNING'),
                       self.reviewed('Codex'), self.record('PASS')])
        self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_base_commit_and_receipt_markers_are_enforced(self):
        self.write_pr([self.record('RUNNING'), self.reviewed('Codex'),
                       self.record('PASS', base_oid='b' * 40)])
        self.assertEqual(self.decide()['permissionDecision'], 'deny')
        self.assertEqual(self.decide('gh pr comment 7 -b "%s{}"' % RUN_MARK)['permissionDecision'], 'deny')

    def test_run_and_head_mentions_in_review_prose_are_not_metadata(self):
        for key in ('run', 'head'):
            review = self.reviewed('Codex')
            literal = ('1' * 32) if key == 'run' else HEAD
            review['body'] = review['body'].replace(key + ': ' + literal, key + ': ' + ('b' * len(literal)))
            review['body'] += '\n' + key + ': ' + literal + '\n'
            self.write_pr([self.record('RUNNING'), review, self.record('PASS')])
            with self.subTest(key=key):
                self.assertEqual(self.decide()['permissionDecision'], 'deny')


if __name__ == '__main__':
    unittest.main()
