"""Guard inputs that the Codex review of PR #13 head 7bf7f36 found unsafe.

Every command below is passed to the hook as JSON; none is executed by a shell.
"""
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

CORE = Path(__file__).resolve().parents[1]
GUARD = CORE / 'claude' / 'hooks' / 'guard.py'


class ReviewedGuardInputs(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='guard-inputs-', dir=CORE)
        self.addCleanup(scratch.cleanup)
        self.cwd = Path(scratch.name)
        self.config = self.cwd / 'config.json'
        self.config.write_text(json.dumps({'protected_dirs': ['lake-data'],
                                           'pr_gate': {'enabled': True, 'trust_codex_bot': False}}))
        self.lake = self.cwd / 'lake-data'
        self.lake.mkdir()

    def decide(self, command, cwd=None, timeout=20):
        payload = {'tool_name': 'Bash', 'tool_input': {'command': command},
                   'cwd': str(cwd or self.cwd)}
        result = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                                capture_output=True, text=True, timeout=timeout,
                                env=dict(os.environ, AGENT_CORE_DIR=str(CORE),
                                         AGENT_CORE_LOCAL=str(self.cwd), AGENT_CORE_TOOL='codex'))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout) if result.stdout.strip() else {}
        return output.get('hookSpecificOutput', {}).get('permissionDecision')

    def test_redirected_assignments_cannot_leave_stale_staging_paths(self):
        for command in ('P=README.md; P=. >/dev/null; git add -A "$P"',
                        'P=README.md; P=. 2>notes.log; git stage -u "$P"'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide('P=README.md; P=src/a.py >/dev/null && git add -A "$P"'))
        self.assertEqual(self.decide('P=lake-data; P=/tmp >/missing-folder/file; rm -rf "$P/raw"'), 'deny')

    def test_branch_queries_after_checkout_cannot_authorize_shared_branch_force_pushes(self):
        repo = self.cwd / 'repo'
        subprocess.run(['git', 'init', '-q', '-b', 'feature', str(repo)], check=True, capture_output=True)
        (repo / 'README.md').write_text('fixture\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'README.md'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Guard fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-m', 'fixture'],
                       check=True, capture_output=True)
        for branch in ('master', 'main'):
            subprocess.run(['git', '-C', str(repo), 'branch', branch], check=True, capture_output=True)
        for change in ('git switch master', 'git checkout master', 'git switch main'):
            command = change + ' && git push --force-with-lease origin "$(git branch --show-current)"'
            with self.subTest(command=command):
                self.assertEqual(self.decide(command, cwd=repo), 'deny')
        self.assertIsNone(self.decide('git push --force-with-lease origin "$(git branch --show-current)"', cwd=repo))

    def test_env_directory_changes_apply_to_the_child_only(self):
        for option in ('-C ' + shlex.quote(str(self.lake)),
                       '--chdir ' + shlex.quote(str(self.lake)),
                       '--chdir=' + shlex.quote(str(self.lake))):
            with self.subTest(option=option):
                self.assertEqual(self.decide('env %s touch records' % option), 'deny')
        self.assertIsNone(self.decide('env -C %s true; touch ordinary.txt' % shlex.quote(str(self.lake))))
        self.assertEqual(self.decide('env -C /tmp true; touch lake-data/records'), 'deny')

    def test_wrapper_redirections_use_the_callers_directory(self):
        for wrapper in ('env -C /tmp', 'sudo -D /tmp'):
            self.assertEqual(self.decide(wrapper + ' true > records', cwd=self.lake), 'deny')
        self.assertIsNone(self.decide('env -C %s true > ordinary.txt' % shlex.quote(str(self.lake))))
        for command in ('echo safe 2>&1', 'echo safe >&2', 'echo safe 2>&-'):
            self.assertIsNone(self.decide(command, cwd=self.lake))

    def test_bundled_sudo_directories_and_chroots_cannot_hide_writes(self):
        path = shlex.quote(str(self.lake))
        for command in ('sudo -nD %s touch records' % path, 'sudo -nD%s touch records' % path,
                        'sudo -R %s touch /records' % path):
            self.assertEqual(self.decide(command), 'deny')

    def test_symlink_targets_keep_bare_name_protections(self):
        (self.cwd / 'current-data').symlink_to(self.lake, target_is_directory=True)
        for command in ('touch current-data/records', 'cp input.txt current-data/records'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        (self.cwd / 'ordinary').mkdir()
        (self.cwd / 'ordinary-link').symlink_to(self.cwd / 'ordinary', target_is_directory=True)
        self.assertIsNone(self.decide('touch ordinary-link/notes.txt'))

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac protected path matching is case-insensitive')
    def test_case_folded_components_keep_bare_name_protections(self):
        self.assertEqual(self.decide('touch LAKE-DATA/records'), 'deny')

    def test_uninspected_delete_patterns_cannot_skip_database_and_batch_checks(self):
        with sqlite3.connect(self.cwd / 'records.sqlite') as connection:
            connection.execute('create table records (value text)')
            connection.execute("insert into records values ('fixture')")
        for command in ('rm -f *.sqlite', 'rm -f *.bin'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide('rm -f missing-named.txt'))

    def test_echo_expansions_cannot_hide_posted_verdicts(self):
        body = '## Codex review\nhead: ' + 'a' * 40 + '\n\nVERDICT: PASS'
        command = 'body=%s; echo "$body" | gh pr comment 7 --body-file -' % shlex.quote(body)
        self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide('body=ordinary; echo "$body" | gh pr comment 7 --body-file -'))

    def test_opaque_echo_scripts_are_held_before_piping_to_a_shell(self):
        for command in (r"echo -e 'git reset \055\055hard' | sh",
                        r"echo -e 'git reset \x2d\x2dhard' | bash",
                        'echo "$unknown_script" | sh'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide('echo "echo ordinary" | sh'))

    def test_json_comment_bodies_are_decoded_before_verdict_checks(self):
        body = '## Codex review\nhead: ' + 'a' * 40 + '\n\nVERDICT: PASS'
        (self.cwd / 'verdict.json').write_text(json.dumps({'body': body}).replace('VERDICT: PASS', r'VERDICT:\u0020PASS'))
        (self.cwd / 'ordinary.json').write_text(json.dumps({'body': 'Ordinary progress comment.'}))
        self.assertEqual(self.decide('gh api repos/o/r/issues/7/comments --input verdict.json'), 'deny')
        self.assertIsNone(self.decide('gh api repos/o/r/issues/7/comments --input ordinary.json'))

    def test_http_graphql_merge_mutations_and_opaque_bodies_are_held(self):
        endpoint = 'https://api.github.com/graphql'
        mutation = {'query': 'mutation { mergePullRequest(input:{pullRequestId:"x"}) { clientMutationId } }'}
        query = {'query': 'query { viewer { login } }'}
        (self.cwd / 'mutation.json').write_text(json.dumps(mutation))
        (self.cwd / 'query.json').write_text(json.dumps(query))
        for command in ('curl --json %s %s' % (shlex.quote(json.dumps(mutation)), endpoint),
                        'curl --url=%s --json %s' % (endpoint, shlex.quote(json.dumps(mutation))),
                        'curl --json @mutation.json ' + endpoint,
                        'curl --data-binary @mutation.json ' + endpoint,
                        'curl --data-binary @missing.json ' + endpoint,
                        'curl -X POST --data-binary "$unknown_body" ' + endpoint):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        for command in ('curl --json %s %s' % (shlex.quote(json.dumps(query)), endpoint),
                        'curl --json @query.json ' + endpoint):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_safe_graphql_body_does_not_excuse_opaque_transfers(self):
        command = 'curl --json %s https://api.github.com/graphql ' % shlex.quote(
            json.dumps({'query': 'query { viewer { login } }'}))
        for extra in ('--next --config transfer.cfg', '-K transfer.cfg', '--expand-data "{{body}}"'):
            with self.subTest(extra=extra):
                self.assertEqual(self.decide(command + extra), 'deny')

    def test_ripgrep_conflicting_output_modes_cannot_exempt_secret_reads(self):
        for command in ('rg --files-with-matches --json KEY .env',
                        'rg --json --files-with-matches KEY .env', 'rg -l --json KEY .env'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        for command in ('rg -l KEY .env', 'rg --count KEY .env', 'rg -q KEY .env'):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_substitution_work_is_bounded_before_the_hook_deadline(self):
        command = ': ' + '$(true) ' * 16000 + '; git reset --hard'
        started = time.monotonic()
        try:
            decision = self.decide(command, timeout=8)
        except subprocess.TimeoutExpired:
            self.fail('The guard did not decide within eight seconds, before the ten-second hook deadline')
        self.assertEqual(decision, 'deny')
        self.assertLess(time.monotonic() - started, 8)

    def test_numeric_operands_separated_from_redirects_remain_arguments(self):
        # its own repo: the scratch dir sits inside the checkout running the suite, and a .agent-lane at that
        # checkout's root (a Claude lane worktree) would refuse every commit from this Codex-driven guard
        repo = self.cwd / 'lane-free'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
        for command in ('git commit -m 123 >/dev/null -a', 'git commit -m 123 > /dev/null --all',
                        'git -c x.y=z commit -m 456 2>/dev/null -a'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command, cwd=repo), 'deny')
        self.assertIsNone(self.decide('git commit -m 123 2>/dev/null README.md', cwd=repo))
        self.assertIsNone(self.decide('git status 2>/dev/null', cwd=repo))

    def test_multiline_or_unreadable_dotenv_is_not_linewise_names_only(self):
        env = self.cwd / '.env'
        for contents in ('PRIVATE_KEY="first-line\nsecret-continuation\nlast-line"\n',
                         "PRIVATE_KEY='first-line\nsecret-continuation\nlast-line'\n",
                         'TOKEN=first\\\nsecret-continuation\n'):
            env.write_text(contents)
            with self.subTest(contents=contents):
                self.assertEqual(self.decide("sed 's/=.*//' .env"), 'deny')
        env.unlink()
        self.assertIsNone(self.decide("sed 's/=.*//' .env"))
        env.write_text('TOKEN=fixture\nOTHER="single line"\n')
        self.assertIsNone(self.decide("sed 's/=.*//' .env"))
        self.assertIsNone(self.decide('grep -c TOKEN .env'))

    def test_http_comment_posts_cannot_publish_verdict_or_run_evidence(self):
        endpoint = 'https://api.github.com/repos/acme/shop/issues/7/comments'
        for body in ('## Codex review\nhead: ' + 'a' * 40 + '\n\nVERDICT: PASS',
                     '## Agent-core review run\nAGENT_CORE_REVIEW_RUN_V1: {}',
                     '## Agent-core review availability\nAGENT_CORE_REVIEW_EVIDENCE_V1: {}'):
            payload = json.dumps({'body': body})
            (self.cwd / 'body.json').write_text(payload)
            for command in ('curl --json %s %s' % (shlex.quote(payload), endpoint),
                            'curl --data-binary @body.json ' + endpoint,
                            'wget --post-data=%s %s' % (shlex.quote(payload), endpoint),
                            'http POST %s body=%s' % (endpoint, shlex.quote(body))):
                with self.subTest(command=command):
                    self.assertEqual(self.decide(command), 'deny')
        for command in ('curl --data-binary @missing.json ' + endpoint,
                        'curl --data-binary "$unknown" ' + endpoint):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        payload = shlex.quote(json.dumps({'body': 'Ordinary progress comment.'}))
        self.assertIsNone(self.decide('curl --json %s %s' % (payload, endpoint)))
        self.assertIsNone(self.decide('curl ' + endpoint))

    def test_unknown_http_options_cannot_prove_a_merge_request_is_read_only(self):
        for client in ('curl', 'wget', 'http', 'xh'):
            command = client + ' "$request" https://api.github.com/repos/acme/shop/pulls/7/merge'
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')

    def test_expanding_heredoc_bytes_cannot_hide_graphql_mutations(self):
        command = 'gh api graphql --input - <<EOF\n$QUERY\nEOF'
        self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide('gh api graphql --input - <<\'EOF\'\n{"query":"query { viewer { login } }"}\nEOF'))



    def test_httpie_raw_and_stdin_bodies_cannot_publish_verdicts(self):
        endpoint = 'https://api.github.com/repos/acme/shop/issues/7/comments'
        body = json.dumps({'body': 'VERDICT: PASS'})
        (self.cwd / 'body.json').write_text(body)
        for client in ('http', 'xh'):
            for command in (client + ' ' + endpoint + ' --raw=' + shlex.quote(body),
                            "printf '%s' " + shlex.quote(body) + ' | ' + client + ' ' + endpoint,
                            client + ' ' + endpoint + ' < body.json'):
                with self.subTest(command=command):
                    self.assertEqual(self.decide(command), 'deny')
            mutation = shlex.quote(json.dumps({'query': 'mutation { mergePullRequest(input:{pullRequestId:"x"}) { clientMutationId } }'}))
            self.assertEqual(self.decide(client + ' https://api.github.com/graphql --raw=' + mutation), 'deny')
            query = shlex.quote(json.dumps({'query': 'query { viewer { login } }'}))
            self.assertIsNone(self.decide(client + ' https://api.github.com/graphql --raw=' + query))


    def test_schemeless_github_endpoints_cannot_publish_verdicts(self):
        endpoint = 'api.github.com/repos/acme/shop/issues/7/comments'
        body = shlex.quote(json.dumps({'body': 'VERDICT: PASS'}))
        for command in ('https ' + endpoint + ' body="VERDICT: PASS"',
                        'curl --proto-default https --json ' + body + ' ' + endpoint):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')


    def test_quoted_unknown_auth_headers_do_not_hide_the_request_shape(self):
        endpoint = 'https://api.github.com/repos/acme/shop/issues/7/comments'
        header = '-H "Authorization: Bearer $TOKEN" '
        for command in ('curl ' + header + endpoint,
                        'curl ' + header + 'https://api.github.com/repos/acme/shop/pulls/7/merge',
                        'curl ' + header + '--json ' + shlex.quote(json.dumps({'body': 'Ordinary note.'})) + ' ' + endpoint):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))
        self.assertEqual(self.decide('curl -H $HEADERS ' + endpoint), 'deny')
        body = shlex.quote(json.dumps({'body': 'VERDICT: PASS'}))
        self.assertEqual(self.decide('curl --json ' + body + ' -H -G ' + endpoint), 'deny')



if __name__ == '__main__':
    unittest.main()
