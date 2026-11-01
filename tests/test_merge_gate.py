"""gh pr merge into a gated branch waits for a Claude and a Codex verdict on the PR head, neither BLOCK.

The gate comes from config pr_gate (enabled, branches, repos, reviewers, trust_codex_bot). gh is a fake
(AGENT_CORE_GH) that returns canned PR JSON and a canned login for `gh api user`, so nothing here touches GitHub.
Verdict comments count only from an allowed author; gh calls time out at 8 seconds and then hold the merge.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CORE, HOOKS, LOCAL, TEST_CONFIG, bash, make_local  # noqa: E402

os.environ['AGENT_CORE_DIR'] = CORE  # so the in-process import sees the same kit as the hook subprocesses
os.environ['AGENT_CORE_LOCAL'] = LOCAL
sys.path.insert(0, HOOKS)
import common  # noqa: E402
import guard  # noqa: E402

HEAD = 'a' * 40
REPO = 'acme/shop'


def comment(who, verdict, head=HEAD):
    return {'author': {'login': 'alex'}, 'body': '## %s review\nhead: %s\n\nfindings\n\nVERDICT: %s\n' % (who, head, verdict)}


def gate(**over):
    """A config whose pr_gate is the test default with the given keys changed."""
    return dict(TEST_CONFIG, pr_gate=dict(TEST_CONFIG['pr_gate'], **over))


class Fixture:
    """A fake gh, canned PR JSON and a decide() that runs the guard on a command. Mixed into the test cases below."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pr_json = os.path.join(self.dir, 'pr.json')
        self.calls = os.path.join(self.dir, 'calls')
        self.login = os.path.join(self.dir, 'login')  # what `gh api user --jq .login` prints
        with open(self.login, 'w') as fh:
            fh.write('alex\n')
        self.gh = os.path.join(self.dir, 'gh')
        with open(self.gh, 'w') as fh:
            fh.write('#!/bin/sh\necho "$@" >> "%s"\n[ "$1" = api ] && { cat "%s"; exit 0; }\n'
                     '[ -f "%s" ] || { echo "no pull requests found" >&2; exit 1; }\ncat "%s"\n'
                     % (self.calls, self.login, self.pr_json, self.pr_json))
        os.chmod(self.gh, 0o755)

    def pr(self, base='main', repo=REPO, comments=(), reviews=()):
        with open(self.pr_json, 'w') as fh:
            json.dump({'number': 7, 'url': 'https://github.com/%s/pull/7' % repo, 'baseRefName': base,
                       'headRefOid': HEAD, 'comments': list(comments), 'reviews': list(reviews)}, fh)

    def decide(self, command='gh pr merge 7 --squash', config=None, gh=None):
        local = make_local(config=config) if config is not None else None
        decision, out = bash(command, cwd=self.dir, local=local, env={'AGENT_CORE_GH': gh or self.gh})
        return out['hookSpecificOutput'] if out else None

    def both_pass(self):
        return [comment('Claude', 'PASS'), comment('Codex', 'PASS')]


class MergeGate(Fixture, unittest.TestCase):

    # verdicts, with the default gate (on, main/master/production, every repo)

    def test_missing_reviews_hold_the_merge(self):
        self.pr()
        out = self.decide()
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('Missing: Claude and Codex', out['permissionDecisionReason'])
        self.pr(comments=[comment('Claude', 'PASS')])
        self.assertIn('Missing: Codex', self.decide()['permissionDecisionReason'])
        self.pr(comments=[comment('Codex', 'PASS')])
        self.assertIn('Missing: Claude', self.decide()['permissionDecisionReason'])

    def test_both_passes_merge(self):
        self.pr(comments=self.both_pass())
        self.assertIsNone(self.decide())
        self.assertIsNone(self.decide('gh pr merge https://github.com/%s/pull/7 --squash --delete-branch' % REPO))

    def test_a_block_holds_the_merge(self):
        self.pr(comments=[comment('Claude', 'PASS'), comment('Codex', 'BLOCK')])
        out = self.decide()
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('VERDICT: BLOCK', out['permissionDecisionReason'])
        self.assertIn('Codex review', out['permissionDecisionReason'])
        self.pr(comments=[comment('Claude', 'BLOCK'), comment('Codex', 'PASS')])
        self.assertIn('Claude review', self.decide()['permissionDecisionReason'])

    def test_a_later_pass_replaces_a_block(self):
        self.pr(comments=[comment('Claude', 'PASS'), comment('Codex', 'BLOCK'), comment('Codex', 'PASS')])
        self.assertIsNone(self.decide())

    def test_a_verdict_on_an_older_head_does_not_count(self):
        self.pr(comments=[comment('Claude', 'PASS', 'b' * 40), comment('Codex', 'PASS')])
        self.assertIn('Missing: Claude', self.decide()['permissionDecisionReason'])

    def test_codex_github_app_review_does_not_count_by_default(self):
        bot = {'author': {'login': 'chatgpt-codex-connector'}, 'commit': {'oid': HEAD}, 'state': 'COMMENTED'}
        self.pr(comments=[comment('Claude', 'PASS')], reviews=[bot])
        self.assertIn('Missing: Codex', self.decide()['permissionDecisionReason'])
        self.assertIn('Missing: Codex', self.decide(config=gate(trust_codex_bot=False))['permissionDecisionReason'])

    def test_codex_github_app_review_counts_when_trusted(self):
        bot = {'author': {'login': 'chatgpt-codex-connector'}, 'commit': {'oid': HEAD}, 'state': 'COMMENTED'}
        self.pr(comments=[comment('Claude', 'PASS')], reviews=[bot])
        self.assertIsNone(self.decide(config=gate(trust_codex_bot=True)))
        stale = dict(bot, commit={'oid': 'b' * 40})
        self.pr(comments=[comment('Claude', 'PASS')], reviews=[stale])
        self.assertIn('Missing: Codex', self.decide(config=gate(trust_codex_bot=True))['permissionDecisionReason'])

    def test_the_message_gives_the_kit_review_command(self):
        self.pr(comments=[comment('Claude', 'PASS')])
        why = self.decide()['permissionDecisionReason']
        self.assertIn('%s 7 codex' % common.kit_cmd('bin/pr_review.py'), why)
        self.assertIn('main', why)
        self.assertIn(REPO, why)

    def test_gh_failing_holds_the_merge(self):
        out = self.decide()  # the fake gh exits 1 while there is no pr.json
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('could not be read', out['permissionDecisionReason'])
        self.assertIn('no pull requests found', out['permissionDecisionReason'])

    def test_gh_missing_or_returning_garbage_holds_the_merge(self):
        out = self.decide(gh=os.path.join(self.dir, 'no-such-gh'))
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('could not be read', out['permissionDecisionReason'])
        with open(self.pr_json, 'w') as fh:
            fh.write('not json')
        self.assertIn('could not be read', self.decide()['permissionDecisionReason'])

    def test_repo_flag_is_passed_to_gh(self):
        self.pr(comments=self.both_pass())
        self.decide('cd /tmp && gh pr merge 7 -R %s --merge' % REPO)
        with open(self.calls) as fh:
            self.assertIn('pr view 7 -R %s' % REPO, fh.read())

    # the gate in config

    def test_gate_off_in_config_lets_everything_merge(self):
        self.pr()  # no reviews at all
        self.assertIsNone(self.decide(config=gate(enabled=False)))
        os.remove(self.pr_json)  # and an unreadable PR does not hold it either: gh is never asked
        self.assertIsNone(self.decide(config=gate(enabled=False)))
        self.assertFalse(os.path.exists(self.calls))

    def test_no_pr_gate_key_means_the_default_gate(self):
        self.pr()
        config = {k: v for k, v in TEST_CONFIG.items() if k != 'pr_gate'}
        self.assertEqual(self.decide(config=config)['permissionDecision'], 'deny')

    def test_repo_not_in_the_list_passes(self):
        self.pr(repo='someone/else')
        self.assertIsNone(self.decide(config=gate(repos=[REPO])))
        self.pr(repo='acme/lib')
        self.assertIsNone(self.decide(config=gate(repos=[REPO])))

    def test_repo_in_the_list_is_gated_whatever_the_case(self):
        self.pr(repo='Acme/Shop')
        out = self.decide(config=gate(repos=['acme/shop', 'acme/other']))
        self.assertEqual(out['permissionDecision'], 'deny')
        self.pr(repo='acme/shop')
        out = self.decide(config=gate(repos=['Acme/Shop']))
        self.assertEqual(out['permissionDecision'], 'deny')
        self.pr(repo='acme/shop', comments=self.both_pass())
        self.assertIsNone(self.decide(config=gate(repos=['Acme/Shop'])))

    def test_any_repo_with_the_star(self):
        self.pr(repo='someone/else')
        self.assertEqual(self.decide(config=gate(repos='*'))['permissionDecision'], 'deny')

    def test_branch_not_in_the_list_passes(self):
        self.pr(base='feature')
        self.assertIsNone(self.decide())
        self.pr(base='main')
        self.assertIsNone(self.decide(config=gate(branches=['production'])))
        self.assertIsNone(self.decide(config=gate(branches=[])))

    def test_branch_in_the_list_is_gated(self):
        for base in ('main', 'master', 'production'):
            self.pr(base=base)
            self.assertEqual(self.decide()['permissionDecision'], 'deny', base)
        self.pr(base='release')
        self.assertIsNone(self.decide())
        self.assertEqual(self.decide(config=gate(branches=['release']))['permissionDecision'], 'deny')

    def test_non_merge_gh_commands_never_ask_gh(self):
        self.assertIsNone(self.decide('gh pr view 7'))
        self.assertIsNone(self.decide('gh pr create --fill'))
        self.assertFalse(os.path.exists(self.calls))


class GlobalFlagsAndApi(Fixture, unittest.TestCase):
    """gh's global flags and `gh api` must not walk around the gate. Reuses MergeGate's fake gh and PR helpers."""

    def test_repo_flag_before_or_inside_the_command_is_still_gated(self):
        self.pr()
        for cmd in ('gh -R %s pr merge 7' % REPO, 'gh --repo %s pr merge 7 --squash' % REPO, 'gh --repo=%s pr merge 7' % REPO,
                    'gh pr -R %s merge 7' % REPO, 'gh -R%s pr merge 7' % REPO, 'gh --repo %s pr merge' % REPO):
            with self.subTest(cmd=cmd):
                out = self.decide(cmd)
                self.assertEqual(out['permissionDecision'], 'deny')
                self.assertIn('Missing: Claude and Codex', out['permissionDecisionReason'])

    def test_repo_flag_before_the_command_reaches_gh_and_a_reviewed_pr_merges(self):
        self.pr(comments=self.both_pass())
        self.assertIsNone(self.decide('gh -R %s pr merge 7 --squash' % REPO))
        with open(self.calls) as fh:
            self.assertIn('pr view 7 -R %s' % REPO, fh.read())

    def test_gh_api_merge_endpoint_is_denied_with_the_gh_pr_merge_hint(self):
        self.pr(comments=self.both_pass())
        for cmd in ('gh api -X PUT repos/%s/pulls/7/merge' % REPO, 'gh api repos/%s/pulls/7/merge --method PUT -f merge_method=squash' % REPO,
                    'gh api /repos/{owner}/{repo}/pulls/12/merge', 'gh api repos/%s/pulls/7/merge -X POST' % REPO,
                    'gh api repos/%s/pulls/7/merge?x=1 -X PUT' % REPO,
                    "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"X\"}) { clientMutationId } }'"):
            with self.subTest(cmd=cmd):
                out = self.decide(cmd)
                self.assertEqual(out['permissionDecision'], 'deny')
                self.assertIn('gh pr merge', out['permissionDecisionReason'])
        self.assertFalse(os.path.exists(self.calls))  # decided without asking gh

    def test_other_gh_api_calls_pass_and_a_gate_that_is_off_lets_the_endpoint_through(self):
        for cmd in ('gh api user', 'gh api repos/%s/pulls/7' % REPO, 'gh api repos/%s/pulls/7/comments' % REPO,
                    'gh api repos/%s/issues/7/merge' % REPO):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd))
        self.assertIsNone(self.decide('gh api -X PUT repos/%s/pulls/7/merge' % REPO, config=gate(enabled=False)))

    def test_curl_to_the_merge_endpoint_is_denied_too(self):
        out = self.decide('curl -X PUT -H "Authorization: token x" https://api.github.com/repos/%s/pulls/7/merge' % REPO)
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIsNone(self.decide('curl https://api.github.com/repos/%s/pulls/7' % REPO))


class Authors(Fixture, unittest.TestCase):
    """A verdict comment counts only when an allowed author wrote it."""

    def stranger(self, who, verdict):
        c = comment(who, verdict)
        c['author'] = {'login': 'mallory'}
        return c

    def test_a_comment_from_someone_else_does_not_count(self):
        self.pr(comments=[self.stranger('Claude', 'PASS'), self.stranger('Codex', 'PASS')])
        self.assertIn('Missing: Claude and Codex', self.decide()['permissionDecisionReason'])
        self.pr(comments=[comment('Claude', 'PASS'), self.stranger('Codex', 'PASS')])
        self.assertIn('Missing: Codex', self.decide()['permissionDecisionReason'])

    def test_a_block_from_someone_else_does_not_hold_the_merge(self):
        self.pr(comments=self.both_pass() + [self.stranger('Codex', 'BLOCK')])
        self.assertIsNone(self.decide())

    def test_the_signed_in_gh_user_is_the_default_author_and_is_read_once(self):
        self.pr(comments=self.both_pass())
        self.assertIsNone(self.decide())
        with open(self.calls) as fh:
            calls = fh.read().splitlines()
        self.assertEqual(calls.count('api user --jq .login'), 1, calls)
        with open(self.login, 'w') as fh:
            fh.write('Alex\n')  # logins are case-insensitive
        self.assertIsNone(self.decide())
        with open(self.login, 'w') as fh:
            fh.write('someone-else\n')
        self.assertIn('Missing: Claude and Codex', self.decide()['permissionDecisionReason'])

    def test_reviewers_in_config_replace_the_signed_in_user(self):
        self.pr(comments=[comment('Claude', 'PASS'), self.stranger('Codex', 'PASS')])
        self.assertIsNone(self.decide(config=gate(reviewers=['alex', 'mallory'])))
        with open(self.calls) as fh:
            self.assertNotIn('api user', fh.read())  # a configured list needs no lookup
        self.assertIn('Missing: Claude and Codex', self.decide(config=gate(reviewers=['someone-else']))['permissionDecisionReason'])
        self.assertIn('Missing: Codex', self.decide(config=gate(reviewers=['ALEX']))['permissionDecisionReason'])

    def test_an_unreadable_signed_in_user_holds_the_merge(self):
        self.pr(comments=self.both_pass())
        with open(self.login, 'w') as fh:
            fh.write('')
        out = self.decide()
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('could not be read', out['permissionDecisionReason'])


class Timeouts(Fixture, unittest.TestCase):
    """The hook is killed at its own timeout (10 s) and a killed hook lets the merge through, so gh gets 8 s."""

    def slow_gh(self, only_api=False):
        gh = os.path.join(self.dir, 'slow-gh')
        guard_line = '[ "$1" = api ] || { cat "%s"; exit 0; }\n' % self.pr_json if only_api else ''
        with open(gh, 'w') as fh:
            fh.write('#!/bin/sh\n%ssleep 30\n' % guard_line)
        os.chmod(gh, 0o755)
        return gh

    def test_a_hanging_gh_holds_the_merge_before_the_hook_is_killed(self):
        self.pr(comments=self.both_pass())
        started = time.monotonic()
        out = self.decide(gh=self.slow_gh())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('could not be read', out['permissionDecisionReason'])
        self.assertIn('8 seconds', out['permissionDecisionReason'])

    def test_a_hanging_login_lookup_holds_the_merge_too(self):
        """The second gh call (`gh api user`), in-process with a 1 second limit so the suite stays fast."""
        self.pr(comments=self.both_pass())
        saved = (guard.GH_TIMEOUT, guard.START, guard.STATE['cwd'], os.environ.get('AGENT_CORE_GH'))
        guard.GH_TIMEOUT, guard.START, guard.STATE['cwd'] = 1, time.monotonic(), self.dir
        os.environ['AGENT_CORE_GH'] = self.slow_gh(only_api=True)
        try:
            started = time.monotonic()
            decision = guard.merge_decision(['7'])
        finally:
            guard.GH_TIMEOUT, guard.START, guard.STATE['cwd'] = saved[:3]
            os.environ.pop('AGENT_CORE_GH') if saved[3] is None else os.environ.__setitem__('AGENT_CORE_GH', saved[3])
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(decision[0], 'deny')
        self.assertIn('could not be read', decision[1])
        self.assertIn('1 seconds', decision[1])

    def test_the_time_left_in_the_budget_limits_a_call(self):
        saved = (guard.START, guard.STATE['cwd'])
        guard.START, guard.STATE['cwd'] = time.monotonic() - 100, self.dir  # the whole budget is gone
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                guard.gh_run(['true'])
        finally:
            guard.START, guard.STATE['cwd'] = saved


if __name__ == '__main__':
    unittest.main()
