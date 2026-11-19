"""A plain push of an open PR's head onto a branch the PR gate covers is the back door the review gate closes.
The repository is a real temporary one; gh is a fake that answers the open-PR list."""
import json
import os
import subprocess
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import TEST_CONFIG, make_local, run_hook  # noqa: E402

REMOTE = 'git@github.com:acme/shop.git'


class DeployPush(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp())
        self.pulls = os.path.join(self.dir, 'pulls.json')
        self.gh = os.path.join(self.dir, 'gh')
        with open(self.gh, 'w') as fh:
            fh.write('#!/bin/sh\necho "$*" >> "%s/calls"\n[ -f "%s/slow" ] && sleep 30\n[ -f "%s/fail" ] && { echo boom >&2; exit 1; }\n'
                     'cat "%s"\n' % (self.dir, self.dir, self.dir, self.pulls))
        os.chmod(self.gh, 0o755)
        self.repo = os.path.join(self.dir, 'repo')
        os.makedirs(self.repo)
        self.git('init', '-q', '-b', 'main')
        self.git('remote', 'add', 'origin', REMOTE)
        self.base = self.commit('base')
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.git('checkout', '-q', '-b', 'feature')
        self.pr_head = self.commit('pr work')
        self.set_pulls(self.pr_head)
        self.local = make_local()

    def git(self, *args):
        out = subprocess.run(['git', '-C', self.repo, '-c', 'user.name=t', '-c', 'user.email=t@t'] + list(args),
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def commit(self, message):
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def set_pulls(self, *heads):
        with open(self.pulls, 'w') as fh:
            json.dump([{'number': 40 + n, 'head': {'sha': sha}} for n, sha in enumerate(heads)], fh)

    def config(self, **gate):
        """Use a local/ folder whose pr_gate is the test default with the given keys changed."""
        self.local = make_local(config=dict(TEST_CONFIG, pr_gate=dict(TEST_CONFIG['pr_gate'], **gate)))

    def push(self, command='git push origin HEAD:main'):
        out = run_hook('guard.py', {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': self.repo},
                       env={'AGENT_CORE_GH': self.gh}, local=self.local)
        return out['hookSpecificOutput'] if out else None

    def assert_denied(self, command='git push origin HEAD:main', number=40, branch='main'):
        out = self.push(command)
        self.assertEqual(out and out['permissionDecision'], 'deny', out)
        reason = out['permissionDecisionReason']
        self.assertIn('puts the head of open PR #%d onto %s, a gated branch of acme/shop' % (number, branch), reason)
        self.assertIn('gh pr merge %d' % number, reason)
        self.assertIn('pr_review.py %d --owner-go "<a whole sentence of theirs, word for word>"' % number, reason)
        self.assertIn("Alex's order", reason)

    def test_pushing_the_head_of_an_open_pr_is_denied(self):
        self.assert_denied()
        self.assert_denied('git push origin feature:main')
        self.assert_denied('git push origin HEAD:refs/heads/main')
        self.assert_denied('git push origin %s:main' % self.pr_head)

    def test_pushing_on_top_of_the_head_of_an_open_pr_is_denied(self):
        self.commit('one more')
        self.assert_denied()

    def test_every_gated_branch_counts(self):
        self.git('update-ref', 'refs/remotes/origin/production', self.base)
        self.assert_denied('git push origin HEAD:production', branch='production')

    def test_the_current_branch_named_main_is_the_destination_without_a_colon(self):
        self.git('checkout', '-q', '-B', 'main')
        self.assert_denied('git push origin main')
        self.assert_denied('git push origin')
        self.assert_denied('git push')

    def test_a_branch_the_gate_does_not_cover_is_allowed(self):
        self.assertIsNone(self.push('git push origin feature'))
        self.assertIsNone(self.push('git push origin HEAD:feature'))
        self.assertIsNone(self.push('git push origin HEAD'))
        self.assertIsNone(self.push('git push -u origin feature'))

    def test_new_commits_that_belong_to_no_open_pr_are_allowed(self):
        self.git('checkout', '-q', '-B', 'main', self.base)
        self.commit('local fix')
        self.assertIsNone(self.push('git push origin main'))
        self.assertIsNone(self.push())
        self.set_pulls('f' * 40)  # a PR head this clone has never seen
        self.assertIsNone(self.push())
        with open(self.pulls, 'w') as fh:
            fh.write('[]')
        self.assertIsNone(self.push())

    def test_a_pr_head_already_in_the_remote_branch_is_not_a_back_door(self):
        self.git('update-ref', 'refs/remotes/origin/main', self.pr_head)
        self.commit('after the merge')
        self.assertIsNone(self.push())

    def test_a_missing_remote_tracking_branch_counts_as_not_merged(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.commit('one more')
        self.assert_denied()

    def test_it_never_holds_a_push_it_cannot_check(self):
        open(os.path.join(self.dir, 'fail'), 'w').close()
        self.assertIsNone(self.push())
        os.unlink(os.path.join(self.dir, 'fail'))
        with open(self.pulls, 'w') as fh:
            fh.write('{"message": "Not Found"}')
        self.assertIsNone(self.push())
        with open(self.pulls, 'w') as fh:
            fh.write('not json')
        self.assertIsNone(self.push())
        for odd in ('[{"head": 5}]', '[{"number": 40, "head": "abc"}]', '[5, null, {"head": null}]'):
            with open(self.pulls, 'w') as fh:
                fh.write(odd)
            self.assertIsNone(self.push(), odd)  # an entry shaped oddly is no reason to hold the push
        self.set_pulls(self.pr_head)
        open(os.path.join(self.dir, 'slow'), 'w').close()
        self.assertIsNone(self.push())  # gh outlives the hook's gh budget

    def test_repositories_the_gate_does_not_cover_are_left_alone(self):
        self.config(repos=['acme/other'])
        self.assertIsNone(self.push())  # a repo the gate does not list
        self.config(repos=['ACME/Shop'])
        self.assert_denied()  # the listing ignores case
        self.config()
        self.git('remote', 'set-url', 'origin', 'https://example.com/acme/shop.git')
        self.assertIsNone(self.push())  # not GitHub
        self.git('remote', 'remove', 'origin')
        self.assertIsNone(self.push())  # no remote

    def test_a_branch_moved_onto_the_pr_head_earlier_in_the_command_is_pushed_alone(self):
        self.git('checkout', '-q', 'main')
        for command in ('git merge --ff-only feature && git push origin main', 'git pull --ff-only . feature && git push',
                        'git fetch origin pull/40/head && git push origin FETCH_HEAD:main',
                        'cd %s && git rebase feature && git push origin HEAD:main' % self.repo):
            with self.subTest(command):
                out = self.push(command)
                self.assertEqual(out and out['permissionDecision'], 'deny', out)
                self.assertIn('Run the push in its own call', out['permissionDecisionReason'])
                self.assertIn('open PR #40', out['permissionDecisionReason'])

    def test_a_chained_push_that_cannot_reach_a_gated_branch_or_a_pr_is_allowed(self):
        self.git('checkout', '-q', 'main')
        self.assertIsNone(self.push('git pull --rebase && git push origin feature'))
        self.assertIsNone(self.push('git fetch origin && git push origin HEAD:other'))
        with open(self.pulls, 'w') as fh:
            fh.write('[]')
        self.assertIsNone(self.push('git pull --ff-only && git push origin main'))
        self.set_pulls(self.pr_head)
        self.git('update-ref', 'refs/remotes/origin/main', self.pr_head)
        self.assertIsNone(self.push('git pull --ff-only && git push origin main'))

    def test_pushing_every_branch_includes_the_gated_branch(self):
        self.git('branch', '-f', 'main', self.pr_head)
        for command in ('git push --all origin', 'git push origin --branches', 'git push --all'):
            with self.subTest(command):
                self.assert_denied(command)
        self.git('branch', '-f', 'main', self.base)
        self.assertIsNone(self.push('git push --all origin'))

    def test_a_saved_upstream_or_refspec_names_the_destination(self):
        self.git('config', 'push.default', 'upstream')
        self.git('config', 'branch.feature.remote', 'origin')
        self.git('config', 'branch.feature.merge', 'refs/heads/main')
        self.assert_denied('git push')
        self.git('config', '--unset', 'push.default')
        self.git('config', 'remote.origin.push', 'refs/heads/feature:refs/heads/main')
        self.assert_denied('git push origin')
        self.git('config', 'remote.origin.push', 'refs/heads/feature:refs/heads/feature')
        self.assertIsNone(self.push('git push origin'))

    def test_the_url_can_stand_in_for_the_remote_name(self):
        self.assert_denied('git push https://github.com/acme/shop.git HEAD:main')

    def test_a_dry_run_pushes_nothing(self):
        self.assertIsNone(self.push('git push --dry-run origin HEAD:main'))

    def test_the_other_push_rules_decide_first(self):
        out = self.push('git push --force origin HEAD:main')
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('Force-pushing or deleting main/master is blocked', out['permissionDecisionReason'])
        self.assertIsNone(self.push('git push --force-with-lease origin HEAD:feature'))
        out = self.push('git push --delete origin main')
        self.assertIn('Force-pushing or deleting main/master is blocked', out['permissionDecisionReason'])

    def test_the_gate_switch_turns_the_check_off(self):
        self.config(enabled=False)
        self.assertIsNone(self.push())


if __name__ == '__main__':
    unittest.main()
