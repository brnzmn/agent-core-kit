"""The Stop hook pushes back once on step lists, offers, and open tasks without a blocker; config can switch it off."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import HOOKS, TEST_CONFIG, env_for, make_local, run_hook  # noqa: E402


def transcript(text, calls=()):
    events = [{'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'name': n, 'input': i}]}} for n, i in calls]
    events.append({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}})
    fh = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
    fh.write('\n'.join(json.dumps(e) for e in events) + '\n')
    fh.close()
    return fh.name


def stop(text, calls=(), active=False, local=None):
    path = transcript(text, calls)
    try:
        out = run_hook('stop_steps.py', {'transcript_path': path, 'stop_hook_active': active}, local=local)
    finally:
        os.unlink(path)
    return out['reason'] if out and out.get('decision') == 'block' else None


class Steps(unittest.TestCase):
    def test_numbered_steps_block(self):
        self.assertIn('instructions', stop('Do this:\n1. Open Vercel\n2. Click Settings\n3. Paste the token'))

    def test_prose_imperatives_block(self):
        text = ('Almost there. Open the Supabase dashboard and go to API settings. Then copy the service key.\n'
                'Paste it into .env.local. You will need to restart the dev server after that.')
        self.assertIsNotNone(stop(text))

    def test_bullets_block(self):
        self.assertIsNotNone(stop('- Run `pnpm build`\n- Then deploy with vercel\n- Check the URL'))

    def test_code_blocks_and_reports_pass(self):
        self.assertIsNone(stop('Shipped.\n```bash\nrun a\nopen b\nclick c\n```\nAdded the hook, ran the gate, merged.'))

    def test_report_style_text_passes(self):
        text = ('**Stop hook.** It now also blocks a turn that ends with:\n- 3 or more instructions;\n'
                '- open tasks with no named blocker.\n\nRun against 153 recent sessions, it would have pushed back '
                'on 23.\n**Deploy:** done.')
        self.assertIsNone(stop(text))

    def test_named_need_passes(self):
        self.assertIsNone(stop('Done except billing.\nNeeds you: open Stripe\n1. Open x\n2. Click y\n3. Paste z'))


class Offers(unittest.TestCase):
    def test_offers_block(self):
        for text in ('Fixed the bug. Want me to open a PR?', 'All green. Should I deploy it?',
                     'Ready to ship, say the word.', 'Branch pushed. Reply go and I will merge.',
                     'Done. Let me know if you want me to add tests.'):
            self.assertIn('offer', stop(text) or '', text)

    def test_offer_earlier_in_message_is_fine(self):
        self.assertIsNone(stop('You asked whether I should rename it; I did.\n\nRenamed and merged.'))

    def test_needs_you_passes(self):
        self.assertIsNone(stop('Needs you: should I send this email as you to the landlord?'))

    def test_question_offering_alternatives_is_not_an_offer(self):
        for text in ('Should I use Postgres or SQLite here?', '**Should I use Postgres or SQLite here?**',
                     'Schema drafted. Should I use Postgres, SQLite or DuckDB for storage?',
                     'Both work. Do you want me to keep the old API, or replace it outright?',
                     'Two readings of the spec.\n\nShould I treat the date as UTC or as local time?',
                     'Cache layer is in. Shall I cache per user or per session?'):
            self.assertIsNone(stop(text), text)

    def test_question_followed_by_a_list_of_options_is_not_an_offer(self):
        for text in ('Two ways to store this. Should I go with one of these?\n- Postgres\n- SQLite',
                     'The cache can live in three places. Which should I pick:\n1. Redis\n2. In-process\n3. Disk',
                     'Should I use:\n* Postgres (hosted)\n* SQLite (file)\n'):
            self.assertIsNone(stop(text), text)

    def test_plain_offers_still_block_next_to_choice_wording(self):
        for text in ('Should I deploy it?', 'Want me to run the migration?', 'Migrated. Want me to run the migration?',
                     'If the build fails or the tests flake, should I stop?',
                     'Should I deploy it, yes or no?',
                     'Pushed. Should I deploy it? Or should I merge it?',
                     'Shall I cache per user or per session? Or should I go the other way?',
                     'Done. Let me know if you want me to add tests or docs.',
                     'Ready to ship, say the word or reply go.'):
            self.assertIn('offer', stop(text) or '', text)

    def test_blocked_and_needs_you_escapes_still_let_offers_through(self):
        self.assertIsNone(stop('Blocked: no token.\nShould I deploy it?'))
        self.assertIsNone(stop('Done.\nNeeds you: want me to run the migration on prod?'))

    def test_choice_question_does_not_hide_open_tasks_or_steps(self):
        calls = [('TodoWrite', {'todos': [{'content': 'deploy', 'status': 'pending'}]})]
        self.assertIn('deploy', stop('Should I use Postgres or SQLite here?', calls) or '')
        self.assertIn('instructions', stop('1. Open Vercel\n2. Click Settings\n3. Paste the token\n\n'
                                           'Should I use Postgres or SQLite here?') or '')


class Tasks(unittest.TestCase):
    def test_open_todos_block(self):
        calls = [('TodoWrite', {'todos': [{'content': 'write tests', 'status': 'completed'},
                                          {'content': 'deploy', 'status': 'pending'}]})]
        self.assertIn('deploy', stop('Wrote the tests.', calls) or '')

    def test_task_tools(self):
        calls = [('TaskCreate', {'subject': 'a'}), ('TaskCreate', {'subject': 'b'}),
                 ('TaskUpdate', {'taskId': '1', 'status': 'completed'})]
        self.assertIn('b', stop('Did a.', calls) or '')
        calls.append(('TaskUpdate', {'taskId': '2', 'status': 'completed'}))
        self.assertIsNone(stop('Did both.', calls))

    def test_desktop_plan(self):
        plan = ('mcp__ccd_turn__update_plan', {'items': [{'content': 'one', 'status': 'in_progress'},
                                                         {'content': 'two', 'status': 'pending'}]})
        self.assertIsNotNone(stop('Did one.', [plan, ('mcp__ccd_turn__update_plan', {'advance': True})]))
        self.assertIsNone(stop('Did both.', [plan, ('mcp__ccd_turn__update_plan', {'advance': True}),
                                             ('mcp__ccd_turn__wrap_up', {'recap': 'x'})]))
        self.assertIsNone(stop('Did both.', [plan, ('mcp__ccd_turn__update_plan', {'done': [1, 2]})]))

    def test_blocked_line_passes(self):
        calls = [('TodoWrite', {'todos': [{'content': 'deploy', 'status': 'pending'}]})]
        self.assertIsNone(stop('Built it.\nBlocked: Vercel token expired; set VERCEL_TOKEN in the keychain.', calls))


class OneRetry(unittest.TestCase):
    def test_second_stop_in_a_turn_passes(self):
        self.assertIsNone(stop('Want me to deploy?', active=True))


class Config(unittest.TestCase):
    STEPS = 'Do this:\n1. Open Vercel\n2. Click Settings\n3. Paste the token'

    def test_reasons_address_the_configured_user(self):
        for text in (self.STEPS, 'Fixed it. Want me to open a PR?'):
            reason = stop(text)
            self.assertIn('Alex', reason)

    def test_unnamed_user_is_the_user(self):
        local = make_local(dict(TEST_CONFIG, user_name=''))
        self.assertIn('instructions for the user', stop(self.STEPS, local=local))
        self.assertIn("the user's standing authorization", stop('Fixed it. Want me to open a PR?', local=local))

    def test_offer_reason_names_the_standing_authorization_and_spend_limit(self):
        offer = stop('Fixed it. Want me to open a PR?')
        self.assertIn('standing authorization in the global instructions', offer)
        self.assertIn('$5 per action', offer)
        self.assertIn('Needs you:', offer)
        local = make_local(dict(TEST_CONFIG, spend_limit_usd=12))
        self.assertIn('$12', stop('Fixed it. Want me to open a PR?', local=local))
        self.assertIn('over $12', stop(self.STEPS, local=local))

    def test_stop_hook_false_never_blocks(self):
        local = make_local(dict(TEST_CONFIG, stop_hook=False))
        calls = [('TodoWrite', {'todos': [{'content': 'deploy', 'status': 'pending'}]})]
        for text in (self.STEPS, 'Fixed it. Want me to open a PR?', 'Built it.'):
            self.assertIsNone(stop(text, calls, local=local), text)

    def test_stop_hook_true_still_blocks(self):
        local = make_local(dict(TEST_CONFIG, stop_hook=True))
        self.assertIsNotNone(stop(self.STEPS, local=local))

    def test_missing_local_folder_still_works_and_stays_quiet_on_errors(self):
        with tempfile.TemporaryDirectory() as parent:
            local = os.path.join(parent, 'no-local-yet')
            path = transcript('Built it.')
            try:
                out = subprocess.run([sys.executable, os.path.join(HOOKS, 'stop_steps.py')],
                                     input=json.dumps({'transcript_path': path}), capture_output=True, text=True,
                                     env=env_for(local), timeout=20)
            finally:
                os.unlink(path)
            self.assertEqual((out.returncode, out.stdout, out.stderr), (0, '', ''))
            self.assertIn('the user', stop('Fixed it. Want me to open a PR?', local=local))
            self.assertFalse(os.path.exists(local))


if __name__ == '__main__':
    unittest.main()
