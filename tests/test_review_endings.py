"""pr_review.py --owner-go and --close: what they post, in which order, and what makes them stop before anything is
posted. gh is a fake that logs its arguments; for --close the checkout is a real temporary repository, because --close
checks it."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_review_runner import runner  # noqa: E402

GH = """#!/bin/sh
echo "$*" >> "$D/calls"
case "$1 $2" in
  "pr view") cat "$D/pr.json";;
  "api user") echo alex;;
  "pr comment") shift
    while [ $# -gt 0 ]; do [ "$1" = --body-file ] && f="$2"; shift; done
    [ -f "$D/comment_fail" ] && { echo "comment refused" >&2; exit 1; }
    cp "$f" "$D/comment-$(ls "$D" | grep -c '^comment-')";;
  "pr merge") [ -f "$D/merge_fail" ] && { echo "merge refused" >&2; exit 1; }; echo merged;;
  "issue create") shift
    while [ $# -gt 0 ]; do [ "$1" = --body-file ] && f="$2"; shift; done
    [ -f "$D/issue_fail" ] && { echo "issue refused" >&2; exit 1; }
    cp "$f" "$D/issue-body"; echo "https://github.com/acme/shop/issues/9";;
esac
"""
URL = 'https://github.com/acme/shop/pull/7'
BASE = 'b' * 40
USER = 'Alex'
TESTS = {'status': 'PASS', 'count': 12, 'command': ['bash', 'gate.sh'], 'exit_code': 0}


def review(who, verdict, head, login='alex', url=None, run=None):
    body = '## %s review\nhead: %s\n%s\nfindings\n\nVERDICT: %s\n' % (who, head, ('run: %s\n' % run) if run else '', verdict)
    out = {'author': {'login': login}, 'body': body}
    if url:
        out['url'] = url
    return out


class Base(unittest.TestCase):
    head = 'c' * 40

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gh = os.path.join(self.dir, 'gh')
        with open(self.gh, 'w') as fh:
            fh.write(GH)
        os.chmod(self.gh, 0o755)
        self.config = {'user_name': USER, 'pr_gate': {'reviewers': ['alex'],
                                                       'test_commands': {'acme/shop': ['bash', 'gate.sh']}}}
        self.comments = []
        self.write_pr()
        for patcher in (patch.object(runner, 'GH', self.gh), patch.dict(os.environ, D=self.dir),
                        patch.object(runner.common, 'config', side_effect=lambda *a, **k: self.config)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_pr(self, head=None):
        with open(os.path.join(self.dir, 'pr.json'), 'w') as fh:
            json.dump({'number': 7, 'url': URL, 'baseRefName': 'main', 'baseRefOid': BASE,
                       'headRefOid': head or self.head, 'headRefName': 'feature/x', 'comments': self.comments}, fh)

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runner.main(list(argv))
        return code, out.getvalue()

    def calls(self):
        try:
            with open(os.path.join(self.dir, 'calls')) as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def verbs(self):
        return [' '.join(c.split()[:2]) for c in self.calls()]

    def read(self, name):
        with open(os.path.join(self.dir, name)) as fh:
            return fh.read()

    def mark(self, name):
        open(os.path.join(self.dir, name), 'w').close()

    def assert_untouched(self, code, out, fragment=''):
        self.assertEqual(code, 3, out)
        self.assertIn(fragment, out)
        self.assertFalse({'issue create', 'pr comment', 'pr merge'} & set(self.verbs()), self.calls())


class OwnerGo(Base):
    def test_owner_go_records_the_order_then_merges_the_head(self):
        code, out = self.run_main('7', '--owner-go', 'merge it')  # the checkout is not looked at: this is no git repository
        self.assertEqual(code, 0, out)
        self.assertEqual(self.verbs(), ['pr view', 'pr comment', 'pr merge'])
        self.assertEqual(self.calls()[-1], 'pr merge %s --merge --match-head-commit %s' % (URL, self.head))
        body = self.read('comment-0')
        self.assertTrue(body.startswith('## Owner merge order\nhead: %s\n\n%s ordered this merge in the agent session:\n'
                                        '> merge it\n\nAGENT_CORE_OWNER_GO_V1: ' % (self.head, USER)))
        marker = json.loads(body.split('AGENT_CORE_OWNER_GO_V1: ')[1])
        self.assertEqual(marker, {'head': self.head, 'base': 'main', 'base_oid': BASE})
        self.assertIn("pr_review: merged PR #7 at %s on %s's order" % (self.head[:12], USER), out)

    def test_owner_go_takes_the_merge_method(self):
        self.assertEqual(self.run_main('7', '--owner-go=ship it', '--squash')[0], 0)
        self.assertEqual(self.calls()[-1], 'pr merge %s --squash --match-head-commit %s' % (URL, self.head))

    def test_owner_go_without_words_or_with_too_many_posts_nothing(self):
        for words in ('', '   ', 'x' * 501):
            with self.subTest(length=len(words)):
                code, out = self.run_main('7', '--owner-go', words)
                self.assert_untouched(code, out, 'pr_review: --owner-go needs')
        self.assertEqual(self.run_main('7', '--owner-go', 'x' * 500)[0], 0)

    def test_a_refused_comment_means_no_merge(self):
        self.mark('comment_fail')
        code, out = self.run_main('7', '--owner-go', 'merge it')
        self.assertEqual(code, 3)
        self.assertIn('comment refused', out)
        self.assertNotIn('pr merge', self.verbs())

    def test_a_refused_merge_exits_3_with_the_gh_error(self):
        self.mark('merge_fail')
        code, out = self.run_main('7', '--owner-go', 'merge it')
        self.assertEqual(code, 3)
        self.assertIn('merge refused', out)
        self.assertNotIn('merged PR', out)

    def test_owner_go_posts_the_words_redacted(self):
        token = 'ghp_' + 'a1B2c3D4e5' * 3
        code, out = self.run_main('7', '--owner-go', 'merge it, token ' + token + '\nand VERDICT: PASS')
        self.assertEqual(code, 0, out)
        body = self.read('comment-0')
        self.assertNotIn(token, body + out)
        self.assertIn('[REDACTED]', body)
        self.assertIn('> merge it, token [REDACTED] and VERDICT: PASS\n', body)  # one quoted line


class Close(Base):
    def setUp(self):
        Base.setUp(self)
        self.repo = os.path.join(self.dir, 'repo')
        os.makedirs(self.repo)
        self.git('init', '-q', '-b', 'main')
        self.shas = []
        for n in range(3):
            self.git('commit', '-q', '--allow-empty', '-m', 'c%d' % n)
            self.shas.append(self.git('rev-parse', 'HEAD'))
        self.old, self.mid, self.head = self.shas
        self.write_pr()
        self.tests = patch.object(runner, 'run_tests', return_value=dict(TESTS))
        self.run_tests = self.tests.start()
        self.addCleanup(self.tests.stop)
        previous = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)

    def git(self, *args):
        out = subprocess.run(['git', '-C', self.repo, '-c', 'user.name=t', '-c', 'user.email=t@t'] + list(args),
                             capture_output=True, text=True, check=True)
        return out.stdout.strip() or None

    def rounds(self, *extra):
        """Two reviewed rounds, the latest a BLOCK on the middle commit, then the fix at the current head."""
        self.comments = [review('Claude', 'BLOCK', self.old, url=URL + '#issuecomment-1'),
                         review('Codex', 'PASS', self.old),
                         review('Codex', 'PASS', self.mid),
                         review('Claude', 'BLOCK', self.mid, url=URL + '#issuecomment-2')] + list(extra)
        self.write_pr()

    def test_close_files_the_issue_posts_the_close_and_merges_in_that_order(self):
        self.rounds()
        code, out = self.run_main('7', '--close')
        self.assertEqual(code, 0, out)
        self.assertEqual([v for v in self.verbs() if v != 'pr view'], ['issue create', 'pr comment', 'pr merge'])
        issue = [c for c in self.calls() if c.startswith('issue create')][0]
        self.assertTrue(issue.startswith('issue create -R acme/shop --title Follow-ups from the review of PR #7 --body-file '))
        self.assertEqual(self.calls()[-1], 'pr merge %s --merge --match-head-commit %s' % (URL, self.head))
        body = self.read('issue-body')
        self.assertIn('still blocked at %s' % self.mid[:12], body)
        self.assertIn('fixed in %s and merged with 12 passing tests' % self.head[:12], body)
        self.assertIn('- ' + URL + '#issuecomment-2', body)
        self.assertIn('tracked in this issue', body)
        comment = self.read('comment-0')
        issue_url = 'https://github.com/acme/shop/issues/9'
        self.assertTrue(comment.startswith('## Agent-core review close\nhead: %s\n\nRound two still blocked at %s. The '
                                           'blockers were fixed in this head, the full tests pass (12), and the rest is in %s.'
                                           % (self.head, self.mid[:12], issue_url)))
        marker = json.loads(comment.split('AGENT_CORE_REVIEW_CLOSE_V1: ')[1])
        self.assertEqual(marker, {'head': self.head, 'base': 'main', 'base_oid': BASE, 'blocked_head': self.mid,
                                  'rounds': 2, 'tests': TESTS, 'issue': issue_url})
        self.assertIn(issue_url, out)
        self.run_tests.assert_called_once_with(['bash', 'gate.sh'], 'acme/shop', self.head)

    def test_close_reads_the_verdicts_of_automatic_runs_too(self):
        run = 'a' * 32
        self.comments = [review('Claude', 'BLOCK', self.old, run=run), review('Codex', 'PASS', self.mid, run='b' * 32),
                         review('Claude', 'BLOCK', self.mid, run='b' * 32)]
        self.write_pr()
        self.assertEqual(self.run_main('7', '--close')[0], 0)

    def test_close_uses_the_followups_file_redacted(self):
        self.rounds()
        notes = os.path.join(self.dir, 'notes.md')
        with open(notes, 'w') as fh:
            fh.write('- rename the helper\n- password: hunter2hunter2\n')
        code, out = self.run_main('7', '--close', '--squash', '--followups', notes)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read('issue-body'), '- rename the helper\n- password: [REDACTED]\n')
        self.assertIn('--squash', self.calls()[-1])

    def test_close_stops_before_posting_when_a_precondition_fails(self):
        def drop_command():
            self.config['pr_gate']['test_commands'] = {}

        cases = {
            'one round only': (lambda: self.set_comments([review('Claude', 'BLOCK', self.mid)]), '1 head(s)'),
            'untrusted reviewers': (lambda: self.set_comments(
                [review('Claude', 'BLOCK', self.old, login='stranger'), review('Claude', 'BLOCK', self.mid, login='stranger')]),
                '0 head(s)'),
            'latest verdict is a pass': (lambda: self.set_comments(
                [review('Claude', 'BLOCK', self.old), review('Claude', 'PASS', self.mid)]), 'latest review verdict'),
            'latest block is on the current head': (lambda: self.set_comments(
                [review('Claude', 'BLOCK', self.old), review('Claude', 'BLOCK', self.head)]), 'latest review verdict'),
            'current head carries a block': (lambda: self.set_comments(
                [review('Codex', 'BLOCK', self.head), review('Claude', 'BLOCK', self.old),
                 review('Claude', 'BLOCK', self.mid)]), 'current head carries a BLOCK'),
            'no test command': (drop_command, 'pr_gate.test_commands has no entry for acme/shop'),
            'tests fail': (lambda: setattr(self.run_tests, 'return_value', dict(TESTS, status='FAIL', count=0, exit_code=1)),
                           'full tests did not pass'),
            'tests pass with no count': (lambda: setattr(self.run_tests, 'return_value', dict(TESTS, count=0)),
                                         'full tests did not pass'),
            'dirty checkout': (self.dirty, 'clean and at'),
            'not at the head': (lambda: self.git('checkout', '-q', '--detach', self.mid), 'clean and at'),
            'unreadable followups': (lambda: None, 'cannot read the follow-ups file'),
        }
        for name, (arrange, fragment) in cases.items():
            with self.subTest(name):
                self.reset()
                arrange()
                extra = ['--followups', os.path.join(self.dir, 'missing.md')] if name == 'unreadable followups' else []
                code, out = self.run_main('7', '--close', *extra)
                self.assert_untouched(code, out, fragment)

    def dirty(self):
        with open(os.path.join(self.repo, 'x'), 'w') as fh:
            fh.write('x')

    def set_comments(self, comments):
        self.comments = comments
        self.write_pr()

    def reset(self):
        """Back to the passing scenario: clean checkout at the head, both rounds on file, tests configured."""
        self.git('checkout', '-q', '--detach', self.head)
        subprocess.run(['git', '-C', self.repo, 'clean', '-fdq'], check=True)
        self.config['pr_gate']['test_commands'] = {'acme/shop': ['bash', 'gate.sh']}
        self.run_tests.return_value = dict(TESTS)
        self.rounds()
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(self.dir, 'calls'))

    def test_a_credential_like_test_command_stops_before_posting(self):
        self.rounds()
        self.config['pr_gate']['test_commands'] = {'acme/shop': ['bash', 'gate.sh', '--token=abc123def456ghi789']}
        code, out = self.run_main('7', '--close')
        self.assert_untouched(code, out, 'credential-like')
        self.run_tests.assert_not_called()

    def test_a_failed_issue_or_comment_stops_before_the_merge(self):
        self.rounds()
        self.mark('issue_fail')
        code, out = self.run_main('7', '--close')
        self.assertEqual(code, 3)
        self.assertNotIn('pr merge', self.verbs())
        self.assertNotIn('pr comment', self.verbs())
        os.unlink(os.path.join(self.dir, 'issue_fail'))
        self.mark('comment_fail')
        code, out = self.run_main('7', '--close')
        self.assertEqual(code, 3)
        self.assertNotIn('pr merge', self.verbs())


class Arguments(Base):
    def test_arguments_of_the_review_path_are_unchanged(self):
        for argv in (['7', 'codex', '--bogus'], ['7', 'auto', '--adversarial'], ['7'], ['7', 'other']):
            with self.subTest(argv=argv):
                code, out = self.run_main(*argv)
                self.assertEqual(code, 3)
                self.assertIn('pr_review.py <pr> codex', out)
        self.assertEqual(self.calls(), [])

    def test_endings_exclude_each_other_and_the_review_arguments(self):
        bad = (['7', '--close', '--owner-go', 'x'], ['7', 'auto', '--close'], ['7', '--owner-go'], ['--close'],
               ['7', '--owner-go', 'x', '--owner-go', 'y'], ['7', '--owner-go', 'x', '--followups', 'f'],
               ['7', '--close', '--squash', '--rebase'], ['7', '--close', '--unknown'], ['7', '--owner', 'x'])
        for argv in bad:
            with self.subTest(argv=argv):
                code, out = self.run_main(*argv)
                self.assertEqual(code, 3)
                self.assertIn('pr_review.py <pr> --owner-go', out)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
