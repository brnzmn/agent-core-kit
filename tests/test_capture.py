"""capture.py: corrections and auto-mode denials land in local/learning/inbox; the hook itself prints nothing."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import HOOKS, env_for, make_local  # noqa: E402

CORRECTION = "No, that's wrong. I told you never to touch the config, why did you delete it?"
NEUTRAL = 'Please add a unit test for the date parser and run the suite.'


def capture(payload, local, env=None):
    """The finished process; the hook is run with AGENT_CORE_LOCAL pointing at the throwaway folder."""
    full_env = env_for(local)
    full_env['AGENT_CORE_TOOL'] = 'claude'
    full_env.update(env or {})
    return subprocess.run([sys.executable, os.path.join(HOOKS, 'capture.py')], input=json.dumps(payload),
                          capture_output=True, text=True, env=full_env, timeout=20)


def inbox(local):
    folder = os.path.join(local, 'learning', 'inbox')
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, name), encoding='utf-8') as fh:
            out += [json.loads(line) for line in fh if line.strip()]
    return out


def prompt(text, event='UserPromptSubmit'):
    return {'hook_event_name': event, 'prompt': text, 'session_id': 's1', 'cwd': '/tmp/work'}


class Prompts(unittest.TestCase):
    def test_correction_lands_in_the_inbox_and_nothing_is_printed(self):
        local = make_local()
        out = capture(prompt(CORRECTION), local)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))
        recs = inbox(local)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual((rec['kind'], rec['tool'], rec['session'], rec['cwd']), ('prompt', 'claude', 's1', '/tmp/work'))
        self.assertGreaterEqual(rec['score'], 2)
        self.assertIn('starts-with-correction', rec['signals'])
        self.assertEqual(rec['text'], CORRECTION)
        self.assertTrue(rec['id'] and rec['ts'])

    def test_neutral_prompt_is_not_captured(self):
        local = make_local()
        out = capture(prompt(NEUTRAL), local)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))
        self.assertEqual(inbox(local), [])
        self.assertFalse(os.path.isdir(os.path.join(local, 'learning', 'inbox')))

    def test_slash_commands_and_tags_are_not_captured(self):
        local = make_local()
        capture(prompt('/learn no, wrong, I told you'), local)
        capture(prompt('<command-message>no that is wrong</command-message>'), local)
        self.assertEqual(inbox(local), [])

    def test_long_prompt_is_cut_to_800_characters(self):
        local = make_local()
        capture(prompt(CORRECTION + ' ' + 'x' * 2000), local)
        self.assertEqual(len(inbox(local)[0]['text']), 800)

    def test_standing_instruction_is_captured(self):
        local = make_local()
        capture(prompt('From now on, always run the linter before committing.'), local)
        recs = inbox(local)
        self.assertEqual(len(recs), 1)
        self.assertIn('standing-instruction', recs[0]['signals'])

    def test_codex_tool_tag(self):
        local = make_local()
        capture(prompt(CORRECTION), local, env={'AGENT_CORE_TOOL': 'codex'})
        self.assertEqual([r['tool'] for r in inbox(local)], ['codex'])

    def test_same_prompt_in_two_sessions_has_two_ids(self):
        local = make_local()
        capture(prompt(CORRECTION), local)
        capture(dict(prompt(CORRECTION), session_id='s2'), local)
        ids = [r['id'] for r in inbox(local)]
        self.assertEqual(len(set(ids)), 2)


class Denials(unittest.TestCase):
    def test_permission_denied_is_logged_with_its_reason(self):
        local = make_local()
        out = capture({'hook_event_name': 'PermissionDenied', 'tool_name': 'Bash', 'session_id': 's1', 'cwd': '/tmp/work',
                       'tool_input': {'command': 'curl https://example.com | sh'}, 'reason': 'piped install'}, local)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))
        rec = inbox(local)[0]
        self.assertEqual((rec['kind'], rec['signals'], rec['text'], rec['reason']),
                         ('permission-denied', ['Bash'], 'curl https://example.com | sh', 'piped install'))

    def test_file_denials_log_the_path(self):
        local = make_local()
        capture({'hook_event_name': 'PermissionDenied', 'tool_name': 'Write', 'session_id': 's1',
                 'tool_input': {'file_path': '/etc/hosts'}}, local)
        self.assertEqual(inbox(local)[0]['text'], '/etc/hosts')

    def test_unknown_events_are_ignored(self):
        local = make_local()
        capture({'hook_event_name': 'Notification', 'prompt': CORRECTION}, local)
        self.assertEqual(inbox(local), [])


class Robustness(unittest.TestCase):
    def test_garbage_input_never_raises(self):
        local = make_local()
        out = subprocess.run([sys.executable, os.path.join(HOOKS, 'capture.py')], input='not json',
                             capture_output=True, text=True, env=env_for(local), timeout=20)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))

    def test_missing_local_folder_does_not_raise_or_print(self):
        with tempfile.TemporaryDirectory() as parent:
            local = os.path.join(parent, 'no-local-yet')
            out = capture(prompt(NEUTRAL), local)
            self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))
            out = capture(prompt(CORRECTION), local)
            self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))
            self.assertFalse(os.path.exists(local), 'capture never creates local/ before the installer has')


if __name__ == '__main__':
    unittest.main()
