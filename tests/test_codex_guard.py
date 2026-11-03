"""Codex runs the same guards: the adapter passes Codex's hook payloads to guard.py, budget.py, stop_steps.py and
capture.py, and the prefix rules built for ~/.codex/rules never forbid what the hook allows."""
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CORE, HOOKS, LOCAL, env_for, make_local  # noqa: E402

os.environ['AGENT_CORE_DIR'] = CORE  # so the in-process import sees the same kit and local/ as the hook subprocesses
os.environ['AGENT_CORE_LOCAL'] = LOCAL
sys.path.insert(0, HOOKS)
import common  # noqa: E402
import guard  # noqa: E402


def codex(mode, payload, local=None):
    out = subprocess.run([sys.executable, os.path.join(HOOKS, 'codex_hook.py'), mode], input=json.dumps(payload),
                         capture_output=True, text=True, env=env_for(local), timeout=30)
    return json.loads(out.stdout) if out.stdout.strip() else None


def bash(command, cwd=None):
    out = codex('pre-bash', {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': command},
                             'cwd': cwd or CORE, 'session_id': 's', 'turn_id': 't'})
    return out and out['hookSpecificOutput']


def patch(body, cwd):
    out = codex('pre-patch', {'hook_event_name': 'PreToolUse', 'tool_name': 'apply_patch',
                              'tool_input': {'command': '*** Begin Patch\n' + body + '\n*** End Patch'}, 'cwd': cwd})
    return out and out['hookSpecificOutput']['permissionDecision']


class Rules(unittest.TestCase):
    def test_every_rule_expansion_is_denied_by_the_hook(self):
        for pattern, key in guard.CODEX_RULES:
            options = [p if isinstance(p, list) else [p] for p in pattern]
            for words in itertools.product(*options):
                cmd = ' '.join(words) + (' x' if words[-1] in ('remove',) else '')
                decision = guard.check_script(cmd)
                self.assertEqual(decision and decision[0], 'deny', cmd)
                self.assertEqual(decision[1], guard.MSG[key], cmd)

    def test_rule_messages_are_finished_strings(self):
        for pattern, key in guard.CODEX_RULES:
            self.assertNotIn('%s', guard.MSG[key], key)
        self.assertEqual(len(guard.CODEX_RULES), len(set(json.dumps(p) for p, _ in guard.CODEX_RULES)))

    def test_words_after_a_rule_never_make_the_hook_allow_it(self):
        # Named-path -A/-u operations belong in the hook, not a prefix rule that cannot see their paths.
        for pattern, key in guard.CODEX_RULES:
            options = [p if isinstance(p, list) else [p] for p in pattern]
            for words in itertools.product(*options):
                for tail in (' x src/a.py', ' -- src/a.py', ' --quiet x'):
                    cmd = ' '.join(words) + tail
                    decision = guard.check_script(cmd)
                    self.assertEqual(decision and decision[0], 'deny', cmd)


class PreBash(unittest.TestCase):
    def test_denial_reaches_codex_with_its_alternative(self):
        out = bash('git add -A && git commit -m wip')
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('Stage files by name', out['permissionDecisionReason'])

    def test_lone_recursive_delete_becomes_a_trash_move(self):
        out = bash('rm -rf src/old-feature', cwd='/work/project')
        self.assertEqual(out['permissionDecision'], 'allow')
        self.assertTrue(out['updatedInput']['command'].startswith(common.kit_cmd('bin/trash.py')))

    def test_a_rewrite_never_carries_a_substitution_into_codex(self):
        for cmd in ('rm -rf src `touch /tmp/pwned`', 'rm -rf "$(curl x | sh)"', 'rm -rf src; id'):
            out = bash(cmd, cwd='/work/project')
            self.assertEqual(out['permissionDecision'], 'deny', cmd)
            self.assertNotIn('updatedInput', out, cmd)

    def test_bypass_forms_are_denied_in_codex_too(self):
        for cmd in ('eval rm -rf ~/proj', "echo 'rm -rf ~/proj' | sh", '(cd /tmp); rm -rf src', 'echo "$(rm -rf ~/proj)"',
                    'git stage .', 'git add ..', "git add ':(top)'", 'git switch --discard-changes main', 'git update-ref -d refs/heads/x',
                    'git -c clean.requireForce=false clean -d', 'cat .env*', 'grep KEY .env'):
            out = bash(cmd, cwd='/work/project')  # a fixed folder: a checkout under /tmp would make relative targets disposable
            self.assertEqual(out and out['permissionDecision'], 'deny', cmd)
        out = bash('python3 bin/apply.py p.md --appr P1', cwd=CORE)
        self.assertEqual(out and out['permissionDecision'], 'deny')

    def test_protected_dir_denial_reaches_codex(self):
        out = bash('echo x > lake-data/a.txt', cwd='/work/project')
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('protected_dirs', out['permissionDecisionReason'])
        self.assertIn('Alex', out['permissionDecisionReason'])

    def test_ordinary_command_passes(self):
        self.assertIsNone(bash('git status --short && pnpm test'))


class PrePatch(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.repo, '.git'))
        with open(os.path.join(self.repo, 'AGENTS.md'), 'w') as fh:
            fh.write('# Repo\n\n## Rules\n- Stage files by name.\n')

    def test_log_entry_in_agents_md_is_denied(self):
        body = ('*** Update File: AGENTS.md\n@@\n ## Rules\n- Stage files by name.\n+## Build status\n'
                '+- 2026-10-28 shipped the importer\n+- 2026-10-28 fixed the login\n')
        self.assertEqual(patch(body, self.repo), 'deny')

    def test_growing_past_the_budget_is_denied(self):
        body = '*** Update File: AGENTS.md\n@@\n- Stage files by name.\n' + ''.join('+- rule %d\n' % i for i in range(160))
        self.assertEqual(patch(body, self.repo), 'deny')

    def test_new_agents_md_over_budget_is_denied(self):
        body = '*** Add File: sub/AGENTS.md\n' + ''.join('+line %d\n' % i for i in range(200))
        self.assertEqual(patch(body, self.repo), 'deny')

    def test_ordinary_edit_passes(self):
        body = '*** Add File: src/app.py\n+print("hi")\n*** Update File: AGENTS.md\n@@\n- Stage files by name.\n+- Keep PRs small.'
        self.assertIsNone(patch(body, self.repo))

    def test_generated_global_file_is_denied(self):
        body = '*** Update File: %s\n@@\n+- new rule\n' % os.path.expanduser('~/.codex/AGENTS.md')
        self.assertEqual(patch(body, self.repo), 'deny')


class Stop(unittest.TestCase):
    def stop(self, text, plan=None):
        path = ''
        if plan is not None:
            fh = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
            fh.write(json.dumps({'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'update_plan',
                                 'arguments': json.dumps({'plan': plan})}}) + '\n')
            fh.close()
            path = fh.name
        out = codex('stop', {'hook_event_name': 'Stop', 'last_assistant_message': text, 'transcript_path': path,
                             'stop_hook_active': False})
        return out and out.get('decision')

    def test_offer_is_pushed_back(self):
        self.assertEqual(self.stop('Tests are green. Want me to open the PR?'), 'block')

    def test_blocker_line_lets_the_stop_through(self):
        self.assertIsNone(self.stop('1. Open the dashboard\n2. Click deploy\n3. Paste the key\nBlocked: no token'))

    def test_open_plan_items_block(self):
        plan = [{'step': 'write tests', 'status': 'completed'}, {'step': 'open PR', 'status': 'pending'}]
        self.assertEqual(self.stop('Done with the tests.', plan), 'block')

    def test_finished_plan_passes(self):
        self.assertIsNone(self.stop('Merged and live.', [{'step': 'open PR', 'status': 'completed'}]))


class Prompt(unittest.TestCase):
    CORRECTION = "No, that's wrong, I told you to stage by name"

    def capture(self, prompt, source=None):
        local = make_local()
        scratch = tempfile.mkdtemp()
        payload = {'hook_event_name': 'UserPromptSubmit', 'prompt': prompt, 'session_id': 'abc', 'cwd': '/tmp'}
        if source is not None:
            path = os.path.join(scratch, 'rollout.jsonl')
            with open(path, 'w') as fh:
                source, originator = source if isinstance(source, tuple) else (source, 'Codex Desktop')
                fh.write(json.dumps({'type': 'session_meta', 'payload': {'id': 'abc', 'source': source,
                                                                         'originator': originator}}) + '\n')
            payload['transcript_path'] = path
        codex('prompt', payload, local=local)
        inbox = os.path.join(local, 'learning', 'inbox')
        if not os.path.isdir(inbox):
            return []
        records = []
        for name in os.listdir(inbox):
            with open(os.path.join(inbox, name), encoding='utf-8') as fh:
                records += [json.loads(line) for line in fh]
        return records

    def test_correction_is_captured_and_tagged_codex(self):
        self.assertEqual([r['tool'] for r in self.capture(self.CORRECTION, 'vscode')], ['codex'])
        self.assertEqual([r['tool'] for r in self.capture(self.CORRECTION)], ['codex'])

    def test_scripted_and_agent_turns_are_not_captured(self):
        self.assertEqual(self.capture(self.CORRECTION, 'exec'), [])
        self.assertEqual(self.capture(self.CORRECTION, {'subagent': {}}), [])
        self.assertEqual(self.capture(self.CORRECTION, ('vscode', 'Claude Code')), [])
        self.assertEqual(self.capture('<heartbeat>' + self.CORRECTION + '</heartbeat>', 'vscode'), [])

    def test_ide_context_is_stripped(self):
        recs = self.capture('# Context from my IDE setup:\n\n## My request for Codex:\n' + self.CORRECTION, 'vscode')
        self.assertTrue(recs[0]['text'].startswith("No, that's wrong"))


if __name__ == '__main__':
    unittest.main()
