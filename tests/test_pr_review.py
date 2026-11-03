"""pr_review.py: the verdict comment the merge gate reads, the exit codes, and argument handling.
gh, node, codex and claude are fakes on PATH: nothing here touches the network or a real CLI."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN  # noqa: E402

PR_REVIEW = os.path.join(BIN, 'pr_review.py')

FAKE_GH = '''#!/bin/sh
echo "$@" >> "$FAKE/gh-calls"
case "$1 $2" in
  "pr view")
    [ -f "$FAKE/pr.json" ] || { echo "no pull requests found" >&2; exit 1; }
    cat "$FAKE/pr.json" ;;
  "pr comment")
    [ -f "$FAKE/comment-fail" ] && { echo "HTTP 403: resource not accessible" >&2; exit 1; }
    number="${3##*/}"; cp "$5" "$FAKE/comment-$number.md" ;;
  *) echo "unexpected gh call: $*" >&2; exit 2 ;;
esac
'''
FAKE_NODE = '''#!/bin/sh
echo "$@" >> "$FAKE/node-calls"
case "$*" in *session-lifecycle-hook*) cat > /dev/null; exit 0 ;; esac
cat "$FAKE/codex.json"
'''
FAKE_CODEX = '#!/bin/sh\nexit 0\n'
FAKE_CLAUDE = '#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE/claude-args"\ncat > "$FAKE/committed.diff"\ncat "$FAKE/claude.txt"\n'


# Fake credentials, assembled at run time so this file holds nothing a secret scanner would flag.
FAKE_SECRETS = {
    'github personal token': 'gh' + 'p_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8',
    'github oauth token': 'gh' + 'o_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8',
    'github user token': 'gh' + 'u_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8',
    'github server token': 'gh' + 's_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8',
    'github fine-grained token': 'github' + '_pat_' + '11AAAAAAA0abcdefghijkl_mnopqrstuvwxyz0123456789ABCDEFGHIJ',
    'anthropic key': 'sk-' + 'ant-api03-AbCdEf123456_GhIjKl789012-MnOpQr345678',
    'openai key': 'sk-' + 'proj-abcdefghij1234567890ABCDEFGHIJ',
    'aws access key id': 'AK' + 'IAIOSFODNN7EXAMPLE',
    'slack token': 'xo' + 'xb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx',
}
FAKE_KEYED = {  # the whole line is quoted in the review; the value after the name must go, the name stays
    'password = "Tr0ub4dor3Tr0ub4dor3Tr0ub4dor3"': 'password = "[REDACTED]"',
    'API_TOKEN: 9f8e7d6c5b4a39281706f5e4d3c2b1a0': 'API_TOKEN: [REDACTED]',
    'client_secret=abcDEF1234567890abcDEF': 'client_secret=[REDACTED]',
    'aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY': 'aws_secret_access_key = [REDACTED]',
    '"apiKey": "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA=="': '"apiKey": "[REDACTED]"',
}
FAKE_PEM = ('-----' + 'BEGIN RSA PRIVATE KEY' + '-----\nMIIEowIBAAKCAQEAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n'
            'yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy\n-----' + 'END RSA PRIVATE KEY' + '-----')
CLEAN_REVIEW = ("src/auth.py:42 `token = request.headers.get('X')` is logged; see key_manager.py. The task-runner-"
                "configuration-loader and disk-encryption-settings-and-more are fine. Commit "
                "0123456789abcdef0123456789abcdef01234567 fixes token_count = 12345678901234567890 and key: "
                "src/components/Button.tsx. monkey = 'no'.")


def run(cmd, cwd, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


def load_module():
    """pr_review.py imported on its own, leaving sys.modules, sys.path and the environment as they were."""
    saved = (dict(os.environ), list(sys.path), sys.modules.get('common'))
    try:
        spec = importlib.util.spec_from_file_location('pr_review_under_test', PR_REVIEW)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved[0])
        sys.path[:] = saved[1]
        if saved[2] is None:
            sys.modules.pop('common', None)
        else:
            sys.modules['common'] = saved[2]


class PrReview(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fake = os.path.join(self.dir, 'fake')
        self.bin = os.path.join(self.fake, 'bin')
        os.makedirs(self.bin)
        for name, body in (('gh', FAKE_GH), ('node', FAKE_NODE), ('codex', FAKE_CODEX), ('claude', FAKE_CLAUDE)):
            path = os.path.join(self.bin, name)
            with open(path, 'w') as fh:
                fh.write(body)
            os.chmod(path, 0o755)
        origin, self.work = os.path.join(self.dir, 'origin.git'), os.path.join(self.dir, 'work')
        run(['git', 'init', '-q', '--bare', '-b', 'main', origin], self.dir)
        run(['git', 'clone', '-q', origin, self.work], self.dir)
        for k, v in (('user.name', 'T'), ('user.email', 't@example.invalid'), ('commit.gpgsign', 'false')):
            run(['git', 'config', k, v], self.work)
        run(['git', 'checkout', '-q', '-b', 'main'], self.work)
        self.commit('base.txt', 'base\n')
        run(['git', 'push', '-q', 'origin', 'main'], self.work)
        self.base = run(['git', 'rev-parse', 'HEAD'], self.work)
        run(['git', 'config', 'url.file://' + origin + '.insteadOf', 'https://github.com/acme/shop.git'], self.work)
        run(['git', 'checkout', '-q', '-b', 'feature'], self.work)
        self.commit('feature.txt', 'feature\n')
        self.head = run(['git', 'rev-parse', 'HEAD'], self.work)
        self.pr(self.head)
        self.claude_says('No blocking issues.\n\nVERDICT: PASS\n')

    def commit(self, name, text):
        with open(os.path.join(self.work, name), 'w') as fh:
            fh.write(text)
        run(['git', 'add', name], self.work)
        run(['git', 'commit', '-q', '-m', name], self.work)

    def pr(self, head, number=7, base='main'):
        with open(os.path.join(self.fake, 'pr.json'), 'w') as fh:
            json.dump({'number': number, 'baseRefName': base, 'headRefOid': head, 'baseRefOid': self.base,
                       'url': 'https://github.com/acme/shop/pull/%d' % number}, fh)

    def claude_says(self, text):
        with open(os.path.join(self.fake, 'claude.txt'), 'w') as fh:
            fh.write(text)

    def codex_says(self, stdout='', result=None):
        data = {'codex': {'status': 0, 'stdout': stdout}}
        if result is not None:
            data['result'] = result
        with open(os.path.join(self.fake, 'codex.json'), 'w') as fh:
            json.dump(data, fh)

    def review(self, *args, path=None):
        env = dict(os.environ, PATH=path or self.bin + os.pathsep + os.environ['PATH'], FAKE=self.fake)
        env.pop('AGENT_CORE_GH', None)
        env.pop('CLAUDECODE', None)
        return subprocess.run([sys.executable, PR_REVIEW] + list(args), cwd=self.work, env=env, capture_output=True,
                              text=True, timeout=60)

    def narrow_path(self, *tools):
        """PATH of the fake gh and codex plus symlinks to just these real tools, so a missing node or claude is missing
        even on a machine that has them installed."""
        links = os.path.join(self.dir, 'links')
        os.makedirs(links, exist_ok=True)
        for name in tools:
            real = shutil.which(name)
            self.assertIsNotNone(real, name)
            if not os.path.lexists(os.path.join(links, name)):
                os.symlink(real, os.path.join(links, name))
        return self.bin + os.pathsep + links

    def comment(self, number=7):
        path = os.path.join(self.fake, 'comment-%d.md' % number)
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return fh.read()

    def calls(self, name):
        path = os.path.join(self.fake, name)
        if not os.path.exists(path):
            return ''
        with open(path) as fh:
            return fh.read()

    # --- the comment the merge gate reads

    def test_claude_pass_is_posted_in_the_gate_format(self):
        out = self.review('7', 'claude')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        body = self.comment()
        self.assertTrue(body.startswith('## Claude review\nhead: %s\n\n' % self.head), body)
        self.assertIn('No blocking issues.', body)
        self.assertTrue(body.endswith('\n\nVERDICT: PASS\n'), body)
        self.assertEqual(len(re.findall(r'^VERDICT: ', body, re.M)), 1)
        self.assertNotIn('(saved to', body)
        self.assertIn('## Claude review', out.stdout)

    def test_claude_block_exits_1(self):
        self.claude_says('src/a.py:3 drops every row on retry.\n\nVERDICT: BLOCK\n')
        out = self.review('7', 'claude')
        self.assertEqual(out.returncode, 1)
        body = self.comment()
        self.assertIn('drops every row on retry', body)
        self.assertTrue(body.endswith('\n\nVERDICT: BLOCK\n'))

    def test_claude_without_a_verdict_posts_nothing(self):
        self.claude_says('I looked at it and it seems fine.\n')
        out = self.review('7', 'claude')
        self.assertEqual(out.returncode, 3)
        self.assertIn('no verdict posted', out.stdout)
        self.assertIsNone(self.comment())

    def test_claude_review_loads_no_settings_files(self):
        self.review('7', 'claude')
        args = self.calls('claude-args').split('\n')
        self.assertIn('--setting-sources', args)
        self.assertEqual(args[args.index('--setting-sources') + 1], '')
        self.assertIn('--strict-mcp-config', args)
        self.assertNotIn('--mcp-config', args)
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertIn('dontAsk', args)
        self.assertIn('--tools', args)
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertNotIn('--allowedTools', args)
        self.assertIn('+feature', self.calls('committed.diff'))
        self.assertTrue(any(self.base in a and self.head in a for a in args))

    def test_codex_p1_blocks_and_p2_passes(self):
        self.codex_says('[P1] feature.txt:1 loses data when the file is empty.')
        out = self.review('7', 'codex')
        self.assertEqual(out.returncode, 1, out.stdout)
        body = self.comment()
        self.assertTrue(body.startswith('## Codex review\nhead: %s\n\n' % self.head))
        self.assertTrue(body.endswith('\n\nVERDICT: BLOCK\n'))
        self.assertIn('review --wait --json --base ' + self.base, self.calls('node-calls'))
        self.assertNotIn('adversarial', self.calls('node-calls'))

        self.codex_says('[P2] feature.txt:1 name could be clearer.')
        out = self.review('7', 'codex')
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertTrue(self.comment().endswith('\n\nVERDICT: PASS\n'))

    def test_codex_adversarial_uses_the_structured_verdict(self):
        self.codex_says('', {'verdict': 'needs-attention', 'findings': [{'title': 'race'}]})
        out = self.review('7', 'codex', '--adversarial')
        self.assertEqual(out.returncode, 1, out.stdout)
        body = self.comment()
        self.assertTrue(body.startswith('## Codex adversarial review\nhead: %s\n\n' % self.head))
        self.assertIn('"race"', body)
        self.assertTrue(body.endswith('\n\nVERDICT: BLOCK\n'))
        self.assertIn('adversarial-review --wait --json --base ' + self.base, self.calls('node-calls'))

        self.codex_says('Nothing found.', {'verdict': 'approve'})
        out = self.review('7', 'codex', '--adversarial')
        self.assertEqual(out.returncode, 0)
        self.assertTrue(self.comment().endswith('\n\nVERDICT: PASS\n'))

    def test_codex_plugin_session_is_torn_down(self):
        self.codex_says('[P3] fine.')
        self.review('7', 'codex')
        calls = self.calls('node-calls').splitlines()
        self.assertEqual(len(calls), 2)
        self.assertIn('codex-companion.mjs', calls[0])
        self.assertIn('session-lifecycle-hook.mjs SessionEnd', calls[1])

    def test_codex_output_that_is_not_json_posts_nothing(self):
        with open(os.path.join(self.fake, 'codex.json'), 'w') as fh:
            fh.write('Error: not signed in\n')
        out = self.review('7', 'codex')
        self.assertEqual(out.returncode, 3)
        self.assertIn('review incomplete', out.stdout)
        self.assertIsNone(self.comment())

    def test_codex_empty_review_posts_nothing(self):
        self.codex_says('')
        out = self.review('7', 'codex')
        self.assertEqual(out.returncode, 3)
        self.assertIsNone(self.comment())

    def test_long_review_is_cut_so_the_verdict_survives(self):
        self.claude_says('x' * 70000 + '\n\nVERDICT: BLOCK\n')
        out = self.review('7', 'claude')
        self.assertEqual(out.returncode, 1)
        body = self.comment()
        self.assertLess(len(body), 65536)
        self.assertIn('review truncated', body)
        self.assertTrue(body.endswith('\n\nVERDICT: BLOCK\n'))

    # --- secrets never reach the comment

    def test_each_kind_of_secret_is_redacted_before_posting(self):
        for kind, secret in FAKE_SECRETS.items():
            self.claude_says('config.py hardcodes %s next to the retry loop.\n\nVERDICT: BLOCK\n' % secret)
            out = self.review('7', 'claude')
            body = self.comment()
            self.assertEqual(out.returncode, 1, kind)
            self.assertNotIn(secret, body, kind)
            self.assertNotIn(secret[:12], body, kind)
            self.assertNotIn(secret[:12], out.stdout, kind)
            self.assertIn('hardcodes [REDACTED] next to the retry loop.', body, kind)
            self.assertIn('pr_review: redacted 1 secret-like string(s)', out.stdout, kind)
            self.assertTrue(body.endswith('\n\nVERDICT: BLOCK\n'), kind)

    def test_values_after_token_secret_password_key_are_redacted_and_the_name_stays(self):
        for line, shown in FAKE_KEYED.items():
            self.claude_says('Found `%s` in the diff.\n\nVERDICT: BLOCK\n' % line)
            out = self.review('7', 'claude')
            body = self.comment()
            self.assertIn('Found `%s` in the diff.' % shown, body, line)
            self.assertIn('redacted 1 secret-like', out.stdout, line)

    def test_private_key_block_is_redacted(self):
        self.claude_says('Committed key:\n%s\nRotate it.\n\nVERDICT: BLOCK\n' % FAKE_PEM)
        out = self.review('7', 'claude')
        body = self.comment()
        self.assertNotIn('MIIEow', body)
        self.assertNotIn('PRIVATE KEY', body)
        self.assertIn('Committed key:\n[REDACTED]\nRotate it.', body)
        self.assertIn('redacted 1 secret-like', out.stdout)

    def test_the_count_covers_every_redaction_and_the_verdict_survives(self):
        secrets = list(FAKE_SECRETS.values()) + list(FAKE_KEYED) + [FAKE_PEM]
        self.claude_says('Findings:\n' + '\n'.join('- %s' % x for x in secrets) + '\n\nVERDICT: BLOCK\n')
        out = self.review('7', 'claude')
        body = self.comment()
        self.assertIn('pr_review: redacted %d secret-like string(s)' % len(secrets), out.stdout)
        self.assertEqual(body.count('[REDACTED]'), len(secrets))
        self.assertEqual(len(re.findall(r'^VERDICT: ', body, re.M)), 1)
        self.assertTrue(body.endswith('\n\nVERDICT: BLOCK\n'))

    def test_codex_reviews_are_redacted_too(self):
        secret = FAKE_SECRETS['github personal token']
        self.codex_says('[P2] logs %s on failure' % secret)
        out = self.review('7', 'codex')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn(secret, self.comment())
        self.assertIn('logs [REDACTED] on failure', self.comment())

    def test_a_secret_cannot_survive_as_a_prefix_at_the_length_cut(self):
        secret = FAKE_SECRETS['github personal token']
        self.claude_says('x ' * 29990 + 'leaked ' + secret + ' ' + 'y ' * 20000 + '\n\nVERDICT: PASS\n')
        self.review('7', 'claude')
        body = self.comment()
        self.assertIn('review truncated', body)
        self.assertNotIn(secret[:8], body)

    def test_nothing_to_redact_says_so_and_leaves_the_text_alone(self):
        self.claude_says(CLEAN_REVIEW + '\n\nVERDICT: PASS\n')
        out = self.review('7', 'claude')
        self.assertIn('pr_review: redacted 0 secret-like string(s)', out.stdout)
        self.assertIn(CLEAN_REVIEW, self.comment())
        self.assertNotIn('[REDACTED]', self.comment())

    def test_a_review_that_did_not_finish_prints_no_secret_either(self):
        secret = FAKE_SECRETS['anthropic key']
        self.claude_says('The diff has %s but I ran out of time.\n' % secret)
        out = self.review('7', 'claude')
        self.assertEqual(out.returncode, 3)
        self.assertNotIn(secret, out.stdout)
        self.assertIsNone(self.comment())

    # --- preconditions and failures

    def test_stale_checkout_is_refused(self):
        self.pr('b' * 40)
        out = self.review('7', 'claude')
        self.assertEqual(out.returncode, 3)
        self.assertIn('clean and at the unchanged PR head', out.stdout)
        self.assertIsNone(self.comment())
        self.assertEqual(self.calls('claude-args'), '')

    def test_gh_failure_is_reported(self):
        os.unlink(os.path.join(self.fake, 'pr.json'))
        out = self.review('7', 'claude')
        self.assertEqual(out.returncode, 3)
        self.assertIn('gh pr view failed: no pull requests found', out.stdout)

    def test_a_failed_comment_is_reported_without_claiming_a_published_verdict(self):
        open(os.path.join(self.fake, 'comment-fail'), 'w').close()
        out = self.review('7', 'claude')
        self.assertEqual(out.returncode, 3)
        self.assertIn('posting the comment failed', out.stdout)
        self.assertNotIn('VERDICT: PASS', out.stdout)

    def test_missing_claude_cli_posts_nothing(self):
        os.unlink(os.path.join(self.bin, 'claude'))
        path = self.narrow_path('git', 'bash', 'awk', 'mktemp', 'dirname', 'env', 'cat', 'cp', 'grep', 'tr', 'tail')
        out = self.review('7', 'claude', path=path)
        self.assertEqual(out.returncode, 3)
        self.assertIn('claude CLI not found', out.stdout)
        self.assertIsNone(self.comment())

    def test_missing_node_posts_nothing(self):
        os.unlink(os.path.join(self.bin, 'node'))
        out = self.review('7', 'codex', path=self.narrow_path('git', 'bash', 'cat', 'cp'))
        self.assertEqual(out.returncode, 3)
        self.assertIn('node CLI not found', out.stdout)
        self.assertIsNone(self.comment())

    # --- arguments

    def test_bad_arguments_print_usage_and_call_nothing(self):
        for args in ([], ['7'], ['7', 'gemini'], ['7', 'claude', '--adversarial'], ['7', 'codex', '--force'],
                     ['7', 'codex', 'extra']):
            out = self.review(*args)
            self.assertEqual(out.returncode, 3, args)
            self.assertIn('pr_review.py <pr> codex [--adversarial]', out.stdout)
        self.assertEqual(self.calls('gh-calls'), '')

    def test_pr_argument_reaches_gh_as_given(self):
        self.pr(self.head, number=12)
        out = self.review('https://github.com/acme/shop/pull/12', 'claude')
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn('pr view https://github.com/acme/shop/pull/12 --json', self.calls('gh-calls'))
        self.assertIsNotNone(self.comment(12))

    # --- the pure functions

    def test_verdict_comment_format(self):
        mod = load_module()
        body = mod.verdict_comment('Claude review', 'a' * 40, '  findings here  \n', 'PASS')
        self.assertEqual(body, '## Claude review\nhead: %s\n\nfindings here\n\nVERDICT: PASS\n' % ('a' * 40))

    def test_redact_counts_replacements_and_is_idempotent(self):
        mod = load_module()
        text, n = mod.redact('a %s b %s' % (FAKE_SECRETS['slack token'], 'token=9f8e7d6c5b4a39281706f5e4d3c2b1a0'))
        self.assertEqual((text, n), ('a [REDACTED] b token=[REDACTED]', 2))
        self.assertEqual(mod.redact(text), (text, 0))
        self.assertEqual(mod.redact(CLEAN_REVIEW), (CLEAN_REVIEW, 0))
        self.assertEqual(mod.redact('https://deploy:hunter2pass@git.example.com/x.git'),
                         ('https://[REDACTED]@git.example.com/x.git', 1))

    def test_redact_stays_fast_on_hostile_input(self):
        mod = load_module()
        for text in ('a-' * 50000, '-----BEGIN PRIVATE KEY-----' * 3000, 'key=1 ' * 20000, 'token' + '=' * 100000):
            start = time.time()
            mod.redact(text)
            self.assertLess(time.time() - start, 5)

    def test_parse_args(self):
        mod = load_module()
        self.assertEqual(mod.parse_args(['7', 'codex']), ('7', 'codex', False))
        self.assertEqual(mod.parse_args(['7', 'codex', '--adversarial']), ('7', 'codex', True))
        self.assertEqual(mod.parse_args(['--adversarial', '7', 'codex']), ('7', 'codex', True))
        self.assertEqual(mod.parse_args(['feature-branch', 'claude']), ('feature-branch', 'claude', False))
        self.assertIsNone(mod.parse_args(['7', 'claude', '--adversarial']))
        self.assertIsNone(mod.parse_args(['7']))
        self.assertIsNone(mod.parse_args(['7', 'codex', '-x']))


if __name__ == '__main__':
    unittest.main()
