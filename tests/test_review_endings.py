"""pr_review.py --owner-go: what it posts, in which order, and what makes it stop before anything is posted.
gh is a fake that logs its arguments."""
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
  "pr comment") shift
    while [ $# -gt 0 ]; do [ "$1" = --body-file ] && f="$2"; shift; done
    [ -f "$D/comment_fail" ] && { echo "comment refused" >&2; exit 1; }
    cp "$f" "$D/comment-$(ls "$D" | grep -c '^comment-')";;
  "pr merge") [ -f "$D/merge_fail" ] && { echo "merge refused" >&2; exit 1; }; echo merged;;
esac
"""
URL = 'https://github.com/acme/shop/pull/7'
BASE = 'b' * 40
HEAD = 'c' * 40
USER = 'Alex'


class OwnerGo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gh = os.path.join(self.dir, 'gh')
        with open(self.gh, 'w') as fh:
            fh.write(GH)
        os.chmod(self.gh, 0o755)
        with open(os.path.join(self.dir, 'pr.json'), 'w') as fh:
            json.dump({'number': 7, 'url': URL, 'baseRefName': 'main', 'baseRefOid': BASE, 'headRefOid': HEAD,
                       'headRefName': 'feature/x', 'comments': []}, fh)
        for patcher in (patch.object(runner, 'GH', self.gh), patch.dict(os.environ, D=self.dir),
                        patch.object(runner.common, 'config', return_value={'user_name': USER})):
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def test_owner_go_records_the_order_then_merges_the_head(self):
        code, out = self.run_main('7', '--owner-go', 'merge it')  # the checkout is not looked at: this is no git repository
        self.assertEqual(code, 0, out)
        self.assertEqual(self.verbs(), ['pr view', 'pr comment', 'pr merge'])
        self.assertEqual(self.calls()[-1], 'pr merge %s --merge --match-head-commit %s' % (URL, HEAD))
        body = self.read('comment-0')
        self.assertTrue(body.startswith('## Owner merge order\nhead: %s\n\n%s ordered this merge in the agent session:\n'
                                        '> merge it\n\nAGENT_CORE_OWNER_GO_V1: ' % (HEAD, USER)))
        marker = json.loads(body.split('AGENT_CORE_OWNER_GO_V1: ')[1])
        self.assertEqual(marker, {'head': HEAD, 'base': 'main', 'base_oid': BASE})
        self.assertIn("pr_review: merged PR #7 at %s on %s's order" % (HEAD[:12], USER), out)

    def test_owner_go_takes_the_merge_method(self):
        self.assertEqual(self.run_main('7', '--owner-go=ship it', '--squash')[0], 0)
        self.assertEqual(self.calls()[-1], 'pr merge %s --squash --match-head-commit %s' % (URL, HEAD))

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

    # arguments

    def test_arguments_of_the_review_path_are_unchanged(self):
        for argv in (['7', 'codex', '--bogus'], ['7', 'auto', '--adversarial'], ['7'], ['7', 'other']):
            with self.subTest(argv=argv):
                code, out = self.run_main(*argv)
                self.assertEqual(code, 3)
                self.assertIn('pr_review.py <pr> codex', out)
        self.assertEqual(self.calls(), [])

    def test_owner_go_excludes_the_review_arguments_and_repeats(self):
        bad = (['7', 'auto', '--owner-go', 'x'], ['7', '--owner-go'], ['--owner-go', 'x'],
               ['7', '--owner-go', 'x', '--owner-go', 'y'], ['7', '--owner-go', 'x', '--squash', '--rebase'],
               ['7', '--owner-go', 'x', '--unknown'], ['7', '--owner', 'x'])
        for argv in bad:
            with self.subTest(argv=argv):
                code, out = self.run_main(*argv)
                self.assertEqual(code, 3)
                self.assertIn('pr_review.py <pr> --owner-go', out)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
