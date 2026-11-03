"""GitHub option values and opaque request bodies cannot bypass the review gate."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from helpers import CORE, HOOKS


class GitHubRequestArguments(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.cwd = Path(self.scratch.name)
        (self.cwd / 'config.json').write_text(json.dumps({'pr_gate': {
            'enabled': True, 'branches': ['main'], 'repos': ['acme/shop'], 'reviewers': ['reviewer']}}))
        self.gh = self.cwd / 'gh'
        self.gh.write_text(
            '#!' + sys.executable + '\nimport json,sys\n'
            'args=sys.argv[1:]\n'
            'if args[:2]==["api","user"]: print("reviewer"); sys.exit()\n'
            'repo="acme/shop"\n'
            'if "-R" in args: repo=args[args.index("-R")+1]\n'
            'print(json.dumps({"number":7,"url":"https://github.com/"+repo+"/pull/7",'
            '"baseRefName":"main","headRefOid":"' + 'a' * 40 + '","comments":[],"reviews":[]}))\n')
        self.gh.chmod(0o755)

    def decide(self, command):
        payload = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': str(self.cwd)}
        result = subprocess.run([sys.executable, str(Path(HOOKS) / 'guard.py')], input=json.dumps(payload),
                                text=True, capture_output=True, timeout=20,
                                env=dict(os.environ, AGENT_CORE_DIR=CORE, AGENT_CORE_GH=str(self.gh),
                                         AGENT_CORE_LOCAL=str(self.cwd)))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout) if result.stdout.strip() else {}
        return output.get('hookSpecificOutput', {}).get('permissionDecision')

    def test_merge_option_values_cannot_select_another_repository(self):
        for option, value in (('--body', '-Racme/lib'), ('--subject', '--repo=acme/lib'),
                              ('-b', '-Racme/lib'), ('--body-file', '-Racme/lib')):
            command = 'gh pr merge 7 --squash %s %s' % (option, shlex.quote(value))
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide('gh pr merge 7 --repo acme/lib --squash --body ordinary'))

    def test_output_option_values_cannot_change_api_methods(self):
        endpoint = 'repos/acme/shop/pulls/7/merge'
        for output in ("--template '-XGET'", "-t '-XGET'", "--jq '-XGET'", "-q '-XGET'",
                       "--header 'Example:-XGET'"):
            with self.subTest(output=output):
                self.assertEqual(self.decide('gh api -X PUT %s %s' % (endpoint, output)), 'deny')
        for command in ('gh api %s --template --input' % endpoint,
                        'gh api -X GET %s --template -XDELETE' % endpoint,
                        'gh api -X GET %s --jq .mergeable' % endpoint):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_graphql_file_and_stdin_bodies_are_checked(self):
        mutation = self.cwd / 'mutation.json'
        query = self.cwd / 'query.json'
        mutation.write_text(json.dumps({'query': 'mutation { mergePullRequest(input:{pullRequestId:"x"}) { clientMutationId } }'}))
        query.write_text(json.dumps({'query': 'query { viewer { login } }'}))
        for command in ('gh api graphql --input mutation.json', 'gh api graphql --input=mutation.json',
                        'cat mutation.json | gh api graphql --input -',
                        'sed s/query/mutation/ query.json | gh api graphql --input -',
                        'gh api graphql --input missing.json',
                        'gh api graphql -F query=@mutation.json'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        for command in ('gh api graphql --input query.json',
                        'cat query.json | gh api graphql --input -',
                        "gh api graphql -f 'query=query { viewer { login } }'"):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_uninspectable_comment_bodies_are_held(self):
        (self.cwd / 'ordinary.txt').write_text('A normal progress comment.\n')
        for command in ("sed 's/DRAFT/PASS/' ordinary.txt | gh pr comment 7 --body-file -",
                        'gh pr comment 7 --body-file -', 'gh issue comment 7 --body-file missing.txt',
                        "sed 's/DRAFT/PASS/' ordinary.txt | gh api repos/o/r/issues/7/comments -F body=@-",
                        'gh api repos/o/r/issues/7/comments --input missing.json'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        for command in ('gh pr comment 7 --body-file ordinary.txt',
                        'cat ordinary.txt | gh pr comment 7 --body-file -',
                        "echo ordinary | gh pr comment 7 --body-file -",
                        "gh pr comment 7 -b 'A normal progress comment.'",
                        "gh api repos/o/r/issues/7/comments -f body=@someone",
                        "gh api repos/o/r/issues/7/comments --jq '.[] | select(.body | contains(\"VERDICT: PASS\"))'"):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_body_files_written_earlier_cannot_be_trusted_as_snapshots(self):
        (self.cwd / 'body.txt').write_text('A normal progress comment.\n')
        (self.cwd / 'query.json').write_text(json.dumps({'query': 'query { viewer { login } }'}))
        for command in ('printf replacement > body.txt; gh pr comment 7 --body-file body.txt',
                        'cp source.txt body.txt; cat body.txt | gh pr comment 7 --body-file -',
                        'cp mutation.json query.json; gh api graphql --input query.json'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide('echo preparing; gh pr comment 7 --body-file body.txt'))

    def test_curl_expanded_method_and_upload_options_stay_unverified(self):
        endpoint = 'https://api.github.com/repos/o/r/pulls/7/merge'
        for option in ("--expand-request '{{method}}'", "--expand-upload-file '{{path}}'",
                       "--expand-data '{{body}}'", "--expand-url '{{url}}'"):
            with self.subTest(option=option):
                self.assertEqual(self.decide('curl --variable method=PUT %s %s' % (option, endpoint)), 'deny')
        self.assertIsNone(self.decide('curl --variable harmless=example -X GET ' + endpoint))


if __name__ == '__main__':
    unittest.main()
