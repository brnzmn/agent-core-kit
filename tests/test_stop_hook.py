"""The Stop hook pushes back once on step lists, offers, own-pick questions and open tasks, and stays quiet about open
tasks while work is in flight; config can switch it off."""
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


def stop(text, calls=(), active=False, local=None, extra=None, lines=()):
    """The block reason, or None. extra: more payload fields; lines: raw transcript entries written before the reply."""
    path = transcript(text, calls)
    if lines:
        with open(path, encoding='utf-8') as fh:
            body = fh.read()
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(json.dumps(e) for e in lines) + '\n' + body)
    try:
        out = run_hook('stop_steps.py', dict({'transcript_path': path, 'stop_hook_active': active}, **(extra or {})),
                       local=local)
    finally:
        os.unlink(path)
    return out['reason'] if out and out.get('decision') == 'block' else None


OPEN = [('TodoWrite', {'todos': [{'content': 'deploy', 'status': 'pending'}]})]


def launch(call, result):
    """A tool result that started background work, the way Claude Code records it."""
    return {'type': 'user', 'timestamp': '2099-01-01T00:00:00.000Z', 'toolUseResult': result, 'message': {
        'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call, 'content': 'started'}]}}


def notice(call, task, status='completed'):
    body = '<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n' % (task, call)
    body += ('<status>%s</status>\n' % status if status else '<event>line</event>\n') + '</task-notification>'
    return {'type': 'queue-operation', 'operation': 'enqueue', 'content': body}


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

    def test_a_report_with_three_bold_labels_passes(self):
        for text in ('**Deploy.** Shipped.\n**Check.** Tests pass.\n**Update.** README done.',
                     '- **Restart:** the bot came back in 4s.\n- **Push:** branch is on origin.\n- **Merge:** PR 12 merged.',
                     '1. **Stop hook.** It now stays quiet.\n2. **Save.** Config is written.\n3. **Install.** It is in place.'):
            self.assertIsNone(stop(text), text)

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


class OwnPick(unittest.TestCase):
    def test_question_with_a_marked_pick_blocks(self):
        for text in ('Two ways to fix it:\n- Fix and run round 4 (my pick)\n- Skip round 4\n\nWhich one?',
                     'Round two still blocks.\n\nHow do you want to go on?\n1. Fix, merge, file the rest (Recommended)\n'
                     '2. Ask for a third round',
                     "I'd go with the queue. Is that fine with you?",
                     'I recommend keeping the old parser for now. Shall we do that, or switch today?'):
            self.assertIn('your own pick', stop(text) or '', text)

    def test_question_without_a_pick_passes(self):
        for text in ('Which database is this for, Postgres or SQLite?',
                     'The export has two date columns.\n\nWhich one is the order date?\n- created_at\n- placed_at'):
            self.assertIsNone(stop(text), text)

    def test_pick_stated_in_a_report_that_does_not_end_on_a_question_passes(self):
        self.assertIsNone(stop('I recommend the queue and built it that way.\n\nMerged and live.'))

    def test_a_recommendation_above_an_unrelated_question_for_the_user_passes(self):
        for text in ('Switched to the recommended Node 22 image and redeployed. Is the bot still logged in on your phone?',
                     'I recommend we revisit caching next week.\n\nWhat time works for the demo?',
                     'The vendor marks the edge runtime as recommended. Did the email from the bank arrive?',
                     'I suggest we wait.\n\nHow did the call go?',
                     "Done with the migration.\n\n- 30 tests pass\n- Deployed to staging\n- I'd recommend a cache later\n\n"
                     'When are you back at your desk?',
                     'Status:\n- Installed the recommended version 3.2\n- Ran the suite\n\nCan you see the new tab in the app?',
                     'Findings:\n1. Leak in the worker\n2. My suggestion: rewrite later\n\nWhat do you see on your phone?',
                     '## Report\n- Tests pass\n- My recommendation: keep the cache\n\nDid you store the token in the vault?',
                     "Shipped the fix. I'd recommend pinning the SDK later. Is the projector free tonight?",
                     "Fixed. I'd suggest a retry later. Do you have the key?",
                     'Fixed. My recommendation is a retry. Can you see the email?'):
            self.assertIsNone(stop(text), text)

    def test_his_own_decisions_stay_questions(self):
        for text in ('A residential proxy fixes it for about $40 a month. I recommend it. Pay for it?',
                     'I recommend removing the old rows. Delete the 2019 archive table?',
                     'The update needs the bot down for five minutes. I recommend tonight. Take it down now or at 02:00?',
                     'Two logo directions:\n- Monogram (my pick)\n- Wordmark\n\nWhich one?',
                     'I recommend sending it today. Send the email as you now?'):
            self.assertIsNone(stop(text), text)

    def test_small_spend_is_the_agents_to_decide(self):
        self.assertIn('your own pick', stop('The larger instance is $3 more. I recommend it. Go with it?') or '')

    def test_needs_you_block_passes(self):
        self.assertIsNone(stop('**Needs you:** pick the plan. I recommend Pro.\n- Pro (Recommended)\n- Team\n\nWhich?'))


class InFlight(unittest.TestCase):
    def test_open_tasks_push_when_nothing_runs(self):
        self.assertIn('deploy', stop('Built it.', OPEN, extra={'background_tasks': [], 'session_crons': []}) or '')

    def test_quiet_while_a_background_task_runs(self):
        running = [{'id': 'b1', 'type': 'shell', 'status': 'running', 'description': 'gate', 'command': 'sleep 60'}]
        self.assertIsNone(stop('Gate is running.', OPEN, extra={'background_tasks': running, 'session_crons': []}))

    def test_quiet_while_a_wake_up_is_scheduled(self):
        cron = [{'id': 'c1', 'schedule': '*/20 * * * *', 'recurring': False, 'prompt': 'check the deploy'}]
        self.assertIsNone(stop('Deploy started.', OPEN, extra={'background_tasks': [], 'session_crons': cron}))

    def test_a_finished_or_malformed_task_in_the_list_does_not_count(self):
        for tasks in ([{'id': 'b1', 'type': 'shell', 'status': 'completed', 'description': 'gate'}],
                      [{'id': 'b1', 'type': 'shell'}], ['b1'], [None], [{'id': 'b1', 'status': 'success'}]):
            self.assertIsNotNone(stop('Built it.', OPEN, extra={'background_tasks': tasks, 'session_crons': []}), tasks)

    def test_unclosed_notification_tags_do_not_slow_the_hook(self):
        junk = {'type': 'user', 'message': {'role': 'user', 'content': '<task-notification> x ' * 20000}}
        shell = launch('toolu_1', {'stdout': '', 'backgroundTaskId': 'bq1'})
        import time
        began = time.time()
        self.assertIsNone(stop('Waiting on it.', OPEN, lines=[junk, shell]))
        self.assertLess(time.time() - began, 5)

    def test_steps_and_offers_still_block_while_something_runs(self):
        running = {'background_tasks': [{'id': 'b1', 'type': 'shell', 'status': 'running'}], 'session_crons': []}
        self.assertIn('offer', stop('Gate is running. Want me to deploy after?', OPEN, extra=running) or '')
        self.assertIn('instructions', stop('1. Open Vercel\n2. Click Settings\n3. Paste the token', OPEN,
                                           extra=running) or '')

    def test_older_claude_code_reads_launches_from_the_transcript(self):
        shell = launch('toolu_1', {'stdout': '', 'backgroundTaskId': 'bq1'})
        agent = launch('toolu_2', {'isAsync': True, 'status': 'async_launched', 'agentId': 'a1'})
        watch = launch('toolu_3', {'taskId': 'm1', 'timeoutMs': 20000, 'persistent': False})
        for started in (shell, agent, watch):
            self.assertIsNone(stop('Waiting on it.', OPEN, lines=[started]), started)
        self.assertIsNotNone(stop('Built it.', OPEN, lines=[shell, notice('toolu_1', 'bq1')]))
        self.assertIsNotNone(stop('Built it.', OPEN, lines=[agent, notice('toolu_2', 'a1', 'failed')]))
        self.assertIsNone(stop('Watching.', OPEN, lines=[watch, notice('', 'm1', status=None)]), 'a monitor event is not its end')
        self.assertIsNotNone(stop('Built it.', OPEN, lines=[watch, notice('toolu_3', 'm1')]))

    def test_a_stopped_task_and_a_stale_launch_do_not_count(self):
        shell = launch('toolu_1', {'stdout': '', 'backgroundTaskId': 'bq1'})
        killed = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'name': 'TaskStop', 'input': {'task_id': 'bq1'}}]}}
        self.assertIsNotNone(stop('Built it.', OPEN, lines=[shell, killed]))
        stale = dict(shell, timestamp='2020-01-01T00:00:00.000Z')
        self.assertIsNotNone(stop('Built it.', OPEN, lines=[stale]))

    def test_a_failed_launch_is_not_work_in_flight(self):
        refused = {'type': 'user', 'timestamp': '2099-01-01T00:00:00.000Z', 'toolUseResult': 'Error: denied', 'message': {
            'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_9', 'content': 'denied'}]}}
        self.assertIsNotNone(stop('Built it.', OPEN, lines=[refused]))


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
        self.assertIn('gives the user 3 instructions', stop(self.STEPS, local=local))
        self.assertIn("the user's standing authorization", stop('Fixed it. Want me to open a PR?', local=local))

    def test_offer_reason_names_the_standing_authorization_and_spend_limit(self):
        offer = stop('Fixed it. Want me to open a PR?')
        self.assertIn('standing authorization in the global instructions', offer)
        self.assertIn('$5 per action', offer)
        self.assertIn('Needs you:', offer)
        local = make_local(dict(TEST_CONFIG, spend_limit_usd=12))
        self.assertIn('$12', stop('Fixed it. Want me to open a PR?', local=local))
        self.assertIn('over $12', stop('Fixed it. Want me to open a PR?', local=local))
        self.assertIn('gives Alex 3 instructions', stop(self.STEPS, local=local))

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
