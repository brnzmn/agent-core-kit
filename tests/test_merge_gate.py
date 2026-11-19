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


def comment(who, verdict, head=HEAD, login='alex'):
    return {'author': {'login': login}, 'body': '## %s review\nhead: %s\n\nfindings\n\nVERDICT: %s\n' % (who, head, verdict)}


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
                       'headRefOid': HEAD, 'baseRefOid': HEAD, 'comments': list(comments), 'reviews': list(reviews)}, fh)

    def decide(self, command='gh pr merge 7 --squash', config=None, gh=None):
        local = make_local(config=config) if config is not None else None
        decision, out = bash(command, cwd=self.dir, local=local, env={'AGENT_CORE_GH': gh or self.gh})
        return out['hookSpecificOutput'] if out else None

    def both_pass(self):
        return [comment('Claude', 'PASS'), comment('Codex', 'PASS')]


class MergeRepositoryContext(Fixture, unittest.TestCase):

    def test_unknown_directory_cannot_select_a_repository_relative_pr(self):
        self.pr(comments=[comment('Claude', 'PASS'), comment('Codex', 'PASS')])
        for prefix in ('cd -', 'cd "$TARGET"'):
            for selector in ('7', '', 'feature'):
                command = prefix + ' && gh pr merge ' + selector + ' --merge'
                with self.subTest(command=command):
                    out = self.decide(command)
                    self.assertIsNotNone(out)
                    self.assertEqual(out['permissionDecision'], 'deny')
                    self.assertIn('repository', out['permissionDecisionReason'])
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'calls')))

    def test_explicit_literal_repository_or_pr_url_determines_the_target(self):
        self.pr(comments=[comment('Claude', 'PASS'), comment('Codex', 'PASS')])
        for target in ('gh -R acme/shop pr merge 7', 'gh pr merge 7 --repo acme/shop',
                       'gh pr merge 7 --repo=acme/shop',
                       'gh pr merge https://github.com/acme/shop/pull/7'):
            with self.subTest(target=target):
                self.assertIsNone(self.decide('cd - && ' + target))
        self.assertIsNone(self.decide('R=acme/shop; cd - && gh -R "$R" pr merge 7'))

    def test_unresolved_repo_or_url_does_not_resolve_an_unknown_directory(self):
        self.pr(comments=[comment('Claude', 'PASS'), comment('Codex', 'PASS')])
        for target in ('gh -R "$REPO" pr merge 7', 'gh pr merge 7 --repo "$REPO"',
                       'gh pr merge https://github.com/$OWNER/$REPO/pull/7',
                       'gh pr merge https://github.com/acme/shop/pull/$PR'):
            with self.subTest(target=target):
                out = self.decide('cd - && ' + target)
                self.assertIsNotNone(out)
                self.assertEqual(out['permissionDecision'], 'deny')

    def test_direct_merge_api_writes_still_deny_with_unknown_repository_context(self):
        for endpoint in ('repos/acme/shop/pulls/7/merge', 'repos/{owner}/{repo}/pulls/7/merge'):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.decide('cd - && gh api -X PUT ' + endpoint)['permissionDecision'], 'deny')
                self.assertIsNone(self.decide('cd - && gh api ' + endpoint))



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
        self.assertIn('%s 7 auto' % common.kit_cmd('bin/pr_review.py'), why)
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
                    'gh api /repos/{owner}/{repo}/pulls/12/merge -X PUT', 'gh api repos/%s/pulls/7/merge -X POST' % REPO,
                    'gh api repos/%s/pulls/7/merge?x=1 -X PUT' % REPO,
                    "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"X\"}) { clientMutationId } }'"):
            with self.subTest(cmd=cmd):
                out = self.decide(cmd)
                self.assertEqual(out['permissionDecision'], 'deny')
                self.assertIn('gh pr merge', out['permissionDecisionReason'])
        self.assertFalse(os.path.exists(self.calls))  # decided without asking gh

    def test_other_gh_api_calls_pass_and_a_gate_that_is_off_lets_the_endpoint_through(self):
        for cmd in ('gh api user', 'gh api repos/%s/pulls/7' % REPO, 'gh api repos/%s/pulls/7/comments' % REPO,
                    'gh api repos/%s/issues/7/merge' % REPO, 'gh api /repos/{owner}/{repo}/pulls/12/merge'):
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
        reason = out['permissionDecisionReason']
        if 'could not be read' in reason:
            self.assertIn('8 seconds', reason)
        else:
            self.assertEqual(reason, 'agent-core: ' + guard.MSG['deep'])

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


class ProviderFallback(Fixture, unittest.TestCase):
    def setUp(self):
        Fixture.setUp(self)
        self.command = ['python3', '-m', 'unittest', 'discover', '-s', 'tests']
        self.write_config()

    def write_config(self, enabled=True, command=None):
        self.config = gate(allow_single_on_unavailable=enabled, trust_codex_bot=True,
                           test_commands={REPO: self.command if command is None else command})

    def decide(self, command='gh pr merge 7 --squash'):
        return Fixture.decide(self, command, config=self.config)

    def evidence(self, provider='Claude', head=HEAD, base='main', login='alex', **test_changes):
        checks = dict(status='PASS', count=308, command=self.command, exit_code=0)
        checks.update(test_changes)
        data = dict(head=head, base=base, base_oid=HEAD, provider=provider, status='UNAVAILABLE', reason='quota', tests=checks)
        return {'author': {'login': login}, 'body': '## Agent-core review availability\n'
                'AGENT_CORE_REVIEW_EVIDENCE_V1: ' + json.dumps(data) + '\n'}

    def test_one_actual_pass_plus_outage_and_tests_passes_for_either_provider(self):
        for absent, present in (('Claude', 'Codex'), ('Codex', 'Claude')):
            with self.subTest(absent=absent):
                self.pr(comments=[comment(present, 'PASS'), self.evidence(provider=absent)])
                self.assertIsNone(self.decide())

    def test_outage_does_not_hide_a_block_or_replace_both_reviews(self):
        for comments in ([comment('Codex', 'BLOCK'), self.evidence()],
                         [comment('Codex', 'PASS'), comment('Claude', 'BLOCK'), self.evidence()],
                         [self.evidence(), self.evidence(provider='Codex')]):
            self.pr(comments=comments)
            self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_stale_stranger_and_wrong_base_evidence_do_not_count(self):
        for kwargs in ({'head': 'b' * 40}, {'login': 'someone'}, {'base': 'feature'}):
            self.pr(comments=[comment('Codex', 'PASS'), self.evidence(**kwargs)])
            self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_failed_missing_zero_and_wrong_command_tests_hold(self):
        for changes in ({'status': 'FAIL'}, {'count': 0}, {'count': True}, {'exit_code': 1}, {'exit_code': False},
                        {'command': ['true']}, {'command': None}):
            self.pr(comments=[comment('Codex', 'PASS'), self.evidence(**changes)])
            self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_new_failed_tests_invalidate_old_pass(self):
        self.pr(comments=[comment('Codex', 'PASS'), self.evidence(), self.evidence(status='FAIL', exit_code=1)])
        self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_bot_review_cannot_supply_the_fallback_pass(self):
        bot = {'author': {'login': 'chatgpt-codex-connector'}, 'commit': {'oid': HEAD}, 'state': 'COMMENTED'}
        self.pr(comments=[self.evidence()], reviews=[bot])
        self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_disabled_fallback_or_unconfigured_tests_hold(self):
        self.pr(comments=[comment('Codex', 'PASS'), self.evidence()])
        for enabled, command in ((False, self.command), ('true', self.command), (1, self.command), (True, []), (True, 'true')):
            self.write_config(enabled, command)
            self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_malformed_gate_switch_cannot_disable_reviews(self):
        self.pr()
        for enabled in (None, 0, '', [], {}):
            with self.subTest(enabled=enabled):
                self.config = gate(enabled=enabled)
                self.assertEqual(self.decide()['permissionDecision'], 'deny')
        self.config = gate(enabled=False)
        self.assertIsNone(self.decide())

    def test_malformed_evidence_fails_closed(self):
        for text in ('not json', '[]', 'null', '{"head": "%s"}' % HEAD):
            evidence = self.evidence()
            evidence['body'] = '## Agent-core review availability\nAGENT_CORE_REVIEW_EVIDENCE_V1: ' + text
            self.pr(comments=[comment('Codex', 'PASS'), evidence])
            self.assertEqual(self.decide()['permissionDecision'], 'deny')

    def test_manual_evidence_posts_are_denied_but_queries_pass(self):
        marker = 'AGENT_CORE_REVIEW_EVIDENCE_V1: {}'
        path = os.path.join(self.dir, 'evidence.txt')
        with open(path, 'w') as fh:
            fh.write(marker)
        for command in ('gh pr comment 7 -b "%s"' % marker,
                        'gh api repos/o/r/issues/7/comments -f body="%s"' % marker,
                        'gh pr comment 7 --body-file %s' % path,
                        'echo "%s" | gh pr comment 7 --body-file -' % marker):
            self.assertEqual(self.decide(command)['permissionDecision'], 'deny')
        self.assertIsNone(self.decide('gh api repos/o/r/issues/7/comments --jq \'contains("%s")\'' % marker))


class OneReview(Fixture, unittest.TestCase):
    """pr_gate.required_reviews 1: the vendor that did not write the PR (told from the head branch) is the one review."""

    def setUp(self):
        Fixture.setUp(self)
        self.command = ['python3', '-m', 'unittest', 'discover', '-s', 'tests']
        self.write_config()

    def write_config(self, reviews=1, fallback=True):
        over = dict(allow_single_on_unavailable=fallback, test_commands={REPO: self.command})
        if reviews is not None:
            over['required_reviews'] = reviews
        self.config = gate(**over)

    def pr(self, head_ref=None, **kwargs):
        Fixture.pr(self, **kwargs)
        if head_ref is not None:
            with open(self.pr_json) as fh:
                data = json.load(fh)
            data['headRefName'] = head_ref
            with open(self.pr_json, 'w') as fh:
                json.dump(data, fh)

    def decide(self, command='gh pr merge 7 --squash'):
        return Fixture.decide(self, command, config=self.config)

    def run_comment(self, providers, run_id='b' * 32, status='PASS', reasons=None, tests=True, head=HEAD):
        data = dict(run_id=run_id, head=head, base='main', base_oid=HEAD, status=status, providers=providers,
                    reasons=reasons or {}, tests=dict(status='PASS', count=5, command=self.command, exit_code=0)
                    if tests else None)
        return {'author': {'login': 'alex'}, 'body': '## Agent-core review run\nAGENT_CORE_REVIEW_RUN_V1: '
                + json.dumps(data) + '\n'}

    def run_verdict(self, who, verdict='PASS', run_id='b' * 32):
        return {'author': {'login': 'alex'}, 'body': '## %s review\nhead: %s\nrun: %s\n\nfindings\n\nVERDICT: %s\n'
                % (who, HEAD, run_id, verdict)}

    def start(self, run_id='b' * 32):
        data = dict(run_id=run_id, head=HEAD, base='main', base_oid=HEAD, status='RUNNING')
        return {'author': {'login': 'alex'}, 'body': '## Agent-core review run\nAGENT_CORE_REVIEW_RUN_V1: '
                + json.dumps(data) + '\n'}

    def outage(self, provider):
        checks = dict(status='PASS', count=5, command=self.command, exit_code=0)
        data = dict(head=HEAD, base='main', base_oid=HEAD, provider=provider, status='UNAVAILABLE', reason='quota',
                    tests=checks)
        return {'author': {'login': 'alex'}, 'body': '## Agent-core review availability\n'
                'AGENT_CORE_REVIEW_EVIDENCE_V1: ' + json.dumps(data) + '\n'}

    def reason(self):
        out = self.decide()
        return out['permissionDecision'], out['permissionDecisionReason']

    def test_a_claude_written_pr_needs_codex_and_a_claude_pass_alone_never_opens_the_gate(self):
        self.pr(comments=[comment('Codex', 'PASS')], head_ref='claude/x')
        self.assertIsNone(self.decide())
        self.pr(comments=[comment('Claude', 'PASS')], head_ref='claude/x')
        decision, text = self.reason()
        self.assertEqual(decision, 'deny')
        self.assertIn('Missing: Codex', text)
        self.assertNotIn('Claude and', text)

    def test_the_gate_asks_github_for_the_head_branch_name(self):
        self.pr(comments=[comment('Codex', 'PASS')], head_ref='claude/x')
        self.assertIsNone(self.decide())
        with open(self.calls) as fh:
            views = [line for line in fh if line.startswith('pr view') and 'comments' in line]
        self.assertTrue(views and all('headRefName' in line for line in views), views)

    def test_codex_github_app_never_stands_in_for_the_one_review(self):
        bot = {'author': {'login': 'chatgpt-codex-connector'}, 'commit': {'oid': HEAD}, 'state': 'COMMENTED'}
        self.config = gate(allow_single_on_unavailable=True, test_commands={REPO: self.command}, required_reviews=1,
                           trust_codex_bot=True)
        self.pr(reviews=[bot], head_ref='claude/x')
        decision, text = self.reason()
        self.assertEqual(decision, 'deny')
        self.assertIn('Missing: Codex', text)
        self.pr(comments=[comment('Codex', 'PASS')], reviews=[bot], head_ref='claude/x')
        self.assertIsNone(self.decide())

    def test_a_codex_written_pr_needs_claude_and_a_codex_pass_alone_never_opens_the_gate(self):
        self.pr(comments=[comment('Claude', 'PASS')], head_ref='codex/x')
        self.assertIsNone(self.decide())
        self.pr(comments=[comment('Codex', 'PASS')], head_ref='codex/x')
        decision, text = self.reason()
        self.assertEqual(decision, 'deny')
        self.assertIn('Missing: Claude', text)

    def test_a_missing_or_odd_head_branch_name_counts_as_not_codex(self):
        for head_ref in (None, '', 'feature/codex/x', 'Codex/x', 'claude/x'):
            with self.subTest(head_ref=head_ref):
                self.pr(comments=[comment('Claude', 'PASS')], head_ref=head_ref)
                self.assertIn('Missing: Codex', self.reason()[1])
                self.pr(comments=[comment('Codex', 'PASS')], head_ref=head_ref)
                self.assertIsNone(self.decide())
        self.pr(comments=[comment('Claude', 'PASS')], head_ref=7)
        self.assertIn('Missing: Codex', self.reason()[1])

    def test_no_review_at_all_names_only_the_required_reviewer(self):
        self.pr(head_ref='claude/x')
        self.assertIn('Missing: Codex.', self.reason()[1])
        self.pr(head_ref='codex/x')
        self.assertIn('Missing: Claude.', self.reason()[1])

    def test_a_block_from_either_provider_holds_even_next_to_the_required_pass(self):
        for head_ref, required, other in (('claude/x', 'Codex', 'Claude'), ('codex/x', 'Claude', 'Codex')):
            for blocker in (required, other):
                with self.subTest(head_ref=head_ref, blocker=blocker):
                    self.pr(comments=[comment(required, 'PASS'), comment(blocker, 'BLOCK')], head_ref=head_ref)
                    decision, text = self.reason()
                    self.assertEqual(decision, 'deny')
                    self.assertIn('%s review of head' % blocker, text)
                    self.assertIn('VERDICT: BLOCK', text)

    def test_a_pass_on_an_older_head_does_not_count(self):
        self.pr(comments=[comment('Codex', 'PASS', 'b' * 40)], head_ref='claude/x')
        self.assertIn('Missing: Codex', self.reason()[1])

    def test_the_required_reviewers_automatic_run_alone_passes_when_its_verdict_carries_the_run_id(self):
        run = self.run_comment({'Codex': 'PASS'})
        self.pr(comments=[self.start(), self.run_verdict('Codex'), run], head_ref='claude/x')
        self.assertIsNone(self.decide())
        self.pr(comments=[self.start(), self.run_verdict('Codex', run_id='c' * 32), run], head_ref='claude/x')
        self.assertEqual(self.reason()[0], 'deny')
        self.pr(comments=[self.start(), comment('Codex', 'PASS'), run], head_ref='claude/x')
        self.assertEqual(self.reason()[0], 'deny')

    def test_an_automatic_run_of_only_the_other_vendor_does_not_pass(self):
        run = self.run_comment({'Claude': 'PASS'})
        self.pr(comments=[self.start(), self.run_verdict('Claude'), run], head_ref='claude/x')
        self.assertEqual(self.reason()[0], 'deny')
        self.pr(comments=[self.start(), self.run_verdict('Codex'), self.run_comment({'Claude': 'PASS'})],
                head_ref='codex/x')
        self.assertEqual(self.reason()[0], 'deny')

    def test_an_automatic_run_that_is_not_a_pass_or_has_odd_providers_does_not_pass(self):
        for providers, status in (({'Codex': 'INCOMPLETE'}, 'INCOMPLETE'), ({'Codex': 'PASS'}, 'INCOMPLETE'),
                                  ({}, 'PASS'), ({'Codex': 'PASS', 'Gemini': 'PASS'}, 'PASS'), (['Codex'], 'PASS')):
            with self.subTest(providers=providers, status=status):
                self.pr(comments=[self.start(), self.run_verdict('Codex'), self.run_comment(providers, status=status)],
                        head_ref='claude/x')
                self.assertEqual(self.reason()[0], 'deny')

    def test_an_automatic_run_with_the_required_reviewer_unavailable_needs_the_policy_and_passing_tests(self):
        run = self.run_comment({'Codex': 'UNAVAILABLE', 'Claude': 'PASS'}, reasons={'Codex': 'quota'})
        comments = [self.start(), self.run_verdict('Claude'), run]
        self.pr(comments=comments, head_ref='claude/x')
        self.assertIsNone(self.decide())
        self.write_config(fallback=False)
        self.assertEqual(self.reason()[0], 'deny')
        self.write_config(fallback='true')
        self.assertEqual(self.reason()[0], 'deny')
        self.write_config()
        for changed in (self.run_comment({'Codex': 'UNAVAILABLE', 'Claude': 'PASS'}, reasons={'Codex': 'quota'},
                                         tests=False),
                        self.run_comment({'Codex': 'UNAVAILABLE', 'Claude': 'PASS'}, reasons={'Codex': 'other'}),
                        self.run_comment({'Codex': 'UNAVAILABLE', 'Claude': 'PASS'}, reasons={})):
            self.pr(comments=[self.start(), self.run_verdict('Claude'), changed], head_ref='claude/x')
            self.assertEqual(self.reason()[0], 'deny')
        self.pr(comments=[self.start(), run], head_ref='claude/x')  # no verdict comment for the run id
        self.assertEqual(self.reason()[0], 'deny')

    def test_without_a_run_record_an_outage_of_the_required_reviewer_plus_the_others_pass_and_tests_open_it(self):
        self.pr(comments=[comment('Claude', 'PASS'), self.outage('Codex')], head_ref='claude/x')
        self.assertIsNone(self.decide())
        self.write_config(fallback=False)
        self.assertIn('Missing: Codex', self.reason()[1])
        self.write_config()
        self.pr(comments=[comment('Codex', 'PASS'), self.outage('Claude')], head_ref='claude/x')
        self.assertIsNone(self.decide())  # the required Codex review passed; the Claude outage is beside the point
        self.pr(comments=[comment('Codex', 'PASS'), self.outage('Claude')], head_ref='codex/x')
        self.assertIsNone(self.decide())
        self.pr(comments=[comment('Claude', 'PASS'), self.outage('Claude')], head_ref='claude/x')
        self.assertEqual(self.reason()[0], 'deny')  # evidence must be for the required reviewer

    def test_failed_outage_tests_hold_under_the_one_review_policy(self):
        bad = self.outage('Codex')
        bad['body'] = bad['body'].replace('"exit_code": 0', '"exit_code": 1')
        self.pr(comments=[comment('Claude', 'PASS'), bad], head_ref='claude/x')
        self.assertEqual(self.reason()[0], 'deny')

    def test_a_mistyped_required_reviews_keeps_two_reviews(self):
        for value in (True, False, '1', 0, 3, 1.0, [1], None):
            with self.subTest(value=value):
                self.write_config(reviews=value)
                self.pr(comments=[comment('Codex', 'PASS')], head_ref='claude/x')
                decision, text = self.reason()
                self.assertEqual(decision, 'deny')
                self.assertIn('Missing: Claude', text)
                self.pr(comments=[comment('Codex', 'PASS'), comment('Claude', 'PASS')], head_ref='claude/x')
                self.assertIsNone(self.decide())

    def test_the_default_policy_wants_both_reviews_whatever_the_head_branch(self):
        self.write_config(reviews=None)
        for head_ref in ('claude/x', 'codex/x'):
            self.pr(comments=[comment('Codex', 'PASS')], head_ref=head_ref)
            self.assertIn('Missing: Claude', self.reason()[1])


if __name__ == '__main__':
    unittest.main()
