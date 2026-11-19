"""The question-box hook: a box that puts the agent's own pick to the user is denied, unless the choice is the user's."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CORE, TEST_CONFIG, make_local, run_hook  # noqa: E402


def box(*questions):
    return {'tool_name': 'AskUserQuestion', 'tool_input': {'questions': list(questions)}}


def q(question, *options, header='Next'):
    return {'question': question, 'header': header, 'multiSelect': False,
            'options': [{'label': label, 'description': text} for label, text in options]}


def ask(payload, local=None):
    out = run_hook('ask_pick.py', payload, local=local)
    return out['hookSpecificOutput'] if out else None


ROUND = q('Round two still blocks. How do we go on?', ('Fix, merge, file the rest (Recommended)', 'No third round'),
          ('Run round three', 'Another paid review'))


class AskPick(unittest.TestCase):
    def test_a_box_with_a_recommended_option_is_denied_and_names_it(self):
        out = ask(box(ROUND))
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('Fix, merge, file the rest (Recommended)', out['permissionDecisionReason'])
        self.assertIn('take the pick', out['permissionDecisionReason'])

    def test_a_pick_in_the_description_or_the_question_counts(self):
        self.assertIsNotNone(ask(box(q('Which queue?', ('Redis', 'My pick: already running here'), ('SQS', 'New account')))))
        self.assertIsNotNone(ask(box(q("I'd go with the nightly run. OK?", ('Yes', 'Nightly'), ('No', 'Hourly')))))

    def test_a_box_without_a_pick_passes(self):
        self.assertIsNone(ask(box(q('Which column is the order date?', ('created_at', 'Set on insert'),
                                    ('placed_at', 'Set at checkout')))))

    def test_his_own_decisions_pass_with_a_pick(self):
        for question in (q('Pay for the residential proxy?', ('Yes, $40 a month (Recommended)', 'Fixes the 429s'), ('No', 'Keep failing')),
                         q('Remove the 2019 archive?', ('Delete it (Recommended)', '12 GB'), ('Keep it', 'No change')),
                         q('When does the bot go down for the update?', ('Now (Recommended)', 'Five minutes of downtime'), ('02:00', 'Tonight')),
                         q('Which logo?', ('Monogram (Recommended)', 'Compact'), ('Wordmark', 'Readable')),
                         q('Announce the launch today?', ('Yes (Recommended)', 'Post is drafted'), ('Wait', 'Next week'))):
            self.assertIsNone(ask(box(question)), question['question'])

    def test_design_questions_pass_and_a_named_need_always_passes(self):
        look = q('Which look should I mock up first?', ('Gallery, art-led (Recommended)', 'Big artwork'),
                 ('Clean grid', 'Like a streaming home screen'), header='Look')
        self.assertIsNone(ask(box(look)))
        tone = q('Which tone for the landing page copy?', ('Plain (Recommended)', 'Short sentences'), ('Playful', 'Jokes'))
        self.assertIsNone(ask(box(tone)))
        named = dict(ROUND, question='Needs you: ' + ROUND['question'])
        self.assertIsNone(ask(box(named)))
        self.assertIn('begin the question with "Needs you:"', ask(box(ROUND))['permissionDecisionReason'])

    def test_one_picked_question_denies_a_box_of_several(self):
        real = q('Which column is the order date?', ('created_at', 'Set on insert'), ('placed_at', 'Set at checkout'))
        out = ask(box(real, ROUND))
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('ask only those', out['permissionDecisionReason'])

    def test_plan_mode_and_other_tools_pass(self):
        self.assertIsNone(ask(dict(box(ROUND), permission_mode='plan')))
        self.assertIsNone(ask({'tool_name': 'Bash', 'tool_input': {'command': 'echo "(Recommended)"'}}))

    def test_malformed_input_never_blocks(self):
        for payload in ({'tool_name': 'AskUserQuestion'}, {'tool_name': 'AskUserQuestion', 'tool_input': 'x'},
                        {'tool_name': 'AskUserQuestion', 'tool_input': {'questions': [None, 3, {'options': 'x'}]}}):
            self.assertIsNone(ask(payload), payload)

    def test_config_switch_turns_it_off(self):
        self.assertIsNone(ask(box(ROUND), local=make_local(dict(TEST_CONFIG, question_hook=False))))
        self.assertIsNotNone(ask(box(ROUND), local=make_local(dict(TEST_CONFIG, question_hook=True))))

    def test_the_hook_is_wired_for_the_two_boxes_only_and_fails_open(self):
        with open(os.path.join(CORE, 'claude', 'settings.json'), encoding='utf-8') as fh:
            entries = [(e.get('matcher'), h) for e in json.load(fh)['hooks']['PreToolUse'] for h in e['hooks']
                       if 'ask_pick.py' in h['command']]
        self.assertEqual([m for m, _ in entries], ['AskUserQuestion|mcp__ccd_turn__wrap_up'])
        self.assertTrue(entries[0][1]['command'].endswith('|| true'))


def step(ask, *labels, **more):
    return {'tool_name': 'mcp__ccd_turn__wrap_up', 'tool_input': dict(
        {'recap': 'Did the work.', 'ask': ask, 'actions': [{'label': l} for l in labels]}, **more)}


class NextStepBox(unittest.TestCase):
    def test_an_ask_about_the_agents_own_next_step_is_denied(self):
        for ask, labels in (('Fix the two review findings and run a fourth round?', ('Fix, run round 4', 'Fix, merge, no round')),
                            ('Merge PR 12 and deploy, with the auth fix as a follow-up?', ('Merge and deploy', 'Fix it first')),
                            ('Create the two service accounts and switch both jobs on?', ('Yes, create them', 'Not now')),
                            ('Build the import for the shared files?', ('Build it here', 'Skip it')),
                            ('PR 12 still blocks after two rounds: fix the three items and re-review, or merge as is?',
                             ('Fix and re-review', 'Merge as is')),
                            ('Fix the pagination bug and the typo, then merge and deploy?', ('Fix, merge, deploy', 'Review first'))):
            out = ask_box(step(ask, *labels))
            self.assertEqual(out and out['permissionDecision'], 'deny', ask)
            self.assertIn('Needs you:', out['permissionDecisionReason'])

    def test_what_only_the_user_can_answer_passes(self):
        for ask, labels in (('Does the dashboard still show the old figure?', ('Still shows it', 'Updated now')),
                            ('When is the test device free for a 20-minute run?', ('Now', 'Later today')),
                            ('After five minutes, did it reconnect?', ('It reconnected', 'It did not')),
                            ('Is the token stored?', ('Stored it',)),
                            ('Have you filled in the form on the portal?', ('Filled it in',)),
                            ('Tell me when the test device is free for a 3-minute run?', ('Free now', 'Later tonight'))):
            self.assertIsNone(ask_box(step(ask, *labels)), ask)

    def test_asking_leave_for_its_own_step_is_still_its_own_step(self):
        for ask in ('Do you want me to fix it and run round four?', 'Would you like me to deploy?',
                    'Can I merge PR 12 now?', 'Could I go ahead and merge?', 'Is it OK to merge and deploy?',
                    'Fix the findings, is that OK?', 'How about I merge it now?', 'Did you want me to fix the 3 findings first?',
                    'Should I rerun the review?', 'May I use the test device now? It takes a few seconds.'):
            self.assertIsNotNone(ask_box(step(ask, 'Yes', 'No')), ask)
        self.assertIsNone(ask_box(step('Needs you: may I use the test device now? It takes a few seconds.', 'Yes, now', 'After I finish')))

    def test_his_own_decisions_pass(self):
        for ask, labels in (('Swap the key on the server and restart the bot?', ('Yes, swap it', 'No, leave it')),
                            ('Upgrade Docker now (restarts the other containers too)?', ('Yes, upgrade it', 'Not now')),
                            ('Stop the staging worker to make room?', ('Stop it', 'Leave it')),
                            ('Buy the larger plan for $40 a month?', ('Yes, buy it', 'Not now')),
                            ('Delete the 2019 archive table?', ('Delete it', 'Keep it'))):
            self.assertIsNone(ask_box(step(ask, *labels)), ask)

    def test_a_named_need_passes_and_a_request_for_his_action_needs_the_name(self):
        self.assertIsNotNone(ask_box(step('Post the question in the team channel now?', 'Posted it', 'Later')))
        self.assertIsNone(ask_box(step('Needs you: post the question in the team channel, so I can read the reply.', 'Posted it')))
        self.assertIsNone(ask_box(step('**Needs you:** pick the plan.', 'Pro', 'Team')))

    def test_a_box_without_an_ask_passes(self):
        self.assertIsNone(ask_box({'tool_name': 'mcp__ccd_turn__wrap_up', 'tool_input': {'recap': 'Merged and live.'}}))
        self.assertIsNone(ask_box(step('', 'OK')))

    def test_a_marked_pick_is_denied_even_in_question_form(self):
        self.assertIsNotNone(ask_box(step('Which queue? I recommend Redis.', 'Redis', 'SQS')))

    def test_plan_mode_and_malformed_input_pass(self):
        self.assertIsNone(ask_box(dict(step('Fix it and run round four?', 'Yes'), permission_mode='plan')))
        for bad in ({'tool_name': 'mcp__ccd_turn__wrap_up'}, {'tool_name': 'mcp__ccd_turn__wrap_up', 'tool_input': []},
                    {'tool_name': 'mcp__ccd_turn__wrap_up', 'tool_input': {'ask': 3, 'actions': 'x'}},
                    {'tool_name': 'mcp__ccd_turn__wrap_up', 'tool_input': {'ask': 'Fix it?', 'actions': [None, 3]}}):
            ask_box(bad)  # must not raise or print garbage; any decision is acceptable for the last one


def ask_box(payload):
    out = run_hook('ask_pick.py', payload)
    return out['hookSpecificOutput'] if out else None


if __name__ == '__main__':
    unittest.main()
