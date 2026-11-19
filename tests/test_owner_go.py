"""pr_review.py --owner-go runs only on words the user wrote in the session. The transcripts are written by the tests."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CORE, TEST_CONFIG, run_hook  # noqa: E402

USER = TEST_CONFIG['user_name']
SCRIPT = os.path.join(CORE, 'bin', 'pr_review.py')
ORDER = 'python3 %s 7 --owner-go "merge it"' % SCRIPT
QUESTION = 'Should I merge it now?'


def human(text, **extra):
    return dict({'type': 'user', 'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': text}}, **extra)


def asks(tool_id='q1'):
    return {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': tool_id, 'name': 'AskUserQuestion', 'input': {'questions': [{'question': QUESTION}]}}]}}


def answer(value, tool_id='q1'):
    return {'type': 'user', 'toolUseResult': {'questions': [{'question': QUESTION}], 'answers': {QUESTION: value}},
            'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tool_id, 'content': 'answered'}]}}


def queued(text, kind='human'):
    return {'type': 'attachment', 'attachment': {'type': 'queued_command', 'origin': {'kind': kind}, 'prompt': text}}


def rollout(kind, payload):
    return json.dumps({'type': kind, 'payload': payload})


class Hook(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def transcript(self, events):
        path = os.path.join(self.dir, 'session.jsonl')
        with open(path, 'w') as fh:
            for event in events:
                fh.write((event if isinstance(event, str) else json.dumps(event)) + '\n')
        return path

    def decide(self, command, events=None, env=None, transcript=None):
        payload = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': self.dir}
        if events is not None:
            payload['transcript_path'] = self.transcript(events)
        if transcript:
            payload['transcript_path'] = transcript
        out = run_hook('guard.py', payload, env=dict({'AGENT_CORE_TOOL': 'claude'}, **(env or {})))
        return out['hookSpecificOutput'] if out else None

    def assert_allowed(self, command, events, **kw):
        self.assertIsNone(self.decide(command, events, **kw))

    def assert_asks(self, command, events, **kw):
        out = self.decide(command, events, **kw)
        self.assertEqual(out and out['permissionDecision'], 'ask', out)
        self.assertIn("could not be read as %s's order from this session" % USER, out['permissionDecisionReason'])
        return out

    def assert_sent_back(self, command, events, **kw):
        out = self.decide(command, events, **kw)
        self.assertEqual(out and out['permissionDecision'], 'deny', out)
        self.assertIn('only part of a sentence %s wrote' % USER, out['permissionDecisionReason'])
        return out

    # the user's words

    def test_words_the_user_typed_are_allowed(self):
        self.assert_allowed(ORDER, [human('merge it')])
        self.assert_allowed(ORDER, [human([{'type': 'text', 'text': 'MERGE   it.'}])])
        self.assert_allowed('python3 %s 7 --owner-go "ok merge it now"' % SCRIPT, [human('Ok merge it now!')])
        self.assert_allowed(ORDER, [human('“merge it”')])
        self.assert_allowed(ORDER, [{'type': 'user', 'message': {'content': 'merge it'}}])  # older Claude Code: no origin

    def test_words_typed_while_the_agent_worked_are_allowed(self):
        self.assert_allowed(ORDER, [queued('merge it, please')])

    def test_words_in_a_button_the_user_tapped_are_allowed(self):
        self.assert_allowed(ORDER, [asks(), answer('Yes, merge it')])
        self.assert_allowed(ORDER, [asks(), answer(['Yes, merge it', 'and delete the branch'])])

    def test_words_anywhere_else_ask(self):
        agent_wrote = [
            ('tool result', {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'the user said merge it'}]}}),
            ('assistant text', {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'They said merge it'}]}}),
            ('task notification', queued('merge it', kind='task-notification')),
            ('task notification entry', human('merge it', origin={'kind': 'task-notification'})),
            ('compaction summary', human('merge it', isCompactSummary=True)),
            ('meta entry', human('merge it', isMeta=True)),
            ('subagent entry', human('merge it', isSidechain=True)),
            ('hook feedback', {'type': 'user', 'message': {'content': 'Stop hook feedback: merge it'}}),
            ('tagged entry', {'type': 'user', 'message': {'content': '<system-reminder>merge it</system-reminder>'}}),
            ('entry with a result object', human('merge it', toolUseResult={'stdout': 'merge it'})),
            ('part of a longer word', human('emerge item')),
            ('the question text', None),
        ]
        for name, event in agent_wrote:
            with self.subTest(name):
                events = [asks(), answer('No')] if event is None else [event]
                self.assert_asks(ORDER, events)

    def test_an_answer_needs_the_question_to_be_asked_earlier(self):
        self.assert_asks(ORDER, [answer('Yes, merge it')])
        self.assert_asks(ORDER, [asks('q2'), answer('Yes, merge it', 'q1')])
        self.assert_asks(ORDER, [asks(), answer('[User did not answer: merge it]')])

    def test_no_transcript_or_an_unreadable_one_asks(self):
        self.assert_asks(ORDER, None)
        self.assert_asks(ORDER, None, transcript=os.path.join(self.dir, 'missing.jsonl'))
        self.assert_asks(ORDER, ['not json', '[1, 2]', '"user"', '{"type": "user"', '{"type":"user","message":5}'])

    def test_a_transcript_that_is_not_a_file_asks(self):
        self.assert_asks(ORDER, None, transcript='/dev/zero')
        self.assert_asks(ORDER, None, transcript=self.dir)

    def test_words_shorter_than_two_characters_are_never_found(self):
        self.assert_asks('python3 %s 7 --owner-go "x"' % SCRIPT, [human('x marks it')])

    def test_codex_cannot_ask_so_unverified_words_are_denied(self):
        out = self.decide(ORDER, [{'type': 'assistant', 'message': {'content': 'merge it'}}], env={'AGENT_CORE_TOOL': 'codex'})
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn("could not be read as %s's order from this session: \"merge it\"" % USER,
                      out['permissionDecisionReason'])
        out = self.decide(ORDER, None, env={'AGENT_CORE_TOOL': 'codex'})
        self.assertEqual(out['permissionDecision'], 'deny')

    def test_codex_rollout_of_a_session_the_user_drove(self):
        meta = rollout('session_meta', {'id': 's', 'cwd': '/p', 'source': 'vscode', 'thread_source': 'user'})
        env = {'AGENT_CORE_TOOL': 'codex'}
        self.assert_allowed(ORDER, [meta, rollout('event_msg', {'type': 'user_message', 'message': 'go on, merge it'})],
                            env=env)
        self.assert_allowed(ORDER, [meta, rollout('event_msg', {'type': 'item_completed', 'item': {
            'type': 'UserMessage', 'content': [{'type': 'text', 'text': 'merge it'}]}})], env=env)
        for name, events in (
                ('agent text', [meta, rollout('response_item', {'type': 'message', 'role': 'assistant',
                                                                'content': [{'type': 'output_text', 'text': 'merge it'}]})]),
                ('delegation', [meta, rollout('event_msg', {'type': 'user_message',
                                                            'message': '<codex_delegation>merge it'})]),
                ('exec session', [rollout('session_meta', {'id': 's', 'source': 'exec'}),
                                  rollout('event_msg', {'type': 'user_message', 'message': 'merge it'})]),
                ('no session_meta', [rollout('event_msg', {'type': 'user_message', 'message': 'merge it'})])):
            with self.subTest(name):
                self.assertEqual(self.decide(ORDER, events, env=env)['permissionDecision'], 'deny')

    # the command

    def test_words_that_are_not_one_literal_are_denied(self):
        for command in ('python3 %s 7 --owner-go "$X"' % SCRIPT, 'python3 %s 7 --owner-go "$(cat /tmp/msg)"' % SCRIPT,
                        'python3 %s 7 --owner-go `cat /tmp/msg`' % SCRIPT, 'python3 %s 7 --owner-go' % SCRIPT,
                        'python3 %s 7 --owner-go ""' % SCRIPT, 'python3 %s 7 --owner-go="$X"' % SCRIPT,
                        'python3 %s 7 --owner-go=' % SCRIPT):
            with self.subTest(command):
                out = self.decide(command, [human('merge it')])
                self.assertEqual(out['permissionDecision'], 'deny', out)
                self.assertIn('literal', out['permissionDecisionReason'])

    def test_a_variable_set_earlier_in_the_command_is_a_literal(self):
        self.assert_allowed('X="merge it" && python3 %s 7 --owner-go "$X"' % SCRIPT, [human('merge it')])

    def test_every_spelling_of_the_call_is_checked(self):
        events = [human('unrelated')]
        for command in ('cd repo && ' + ORDER, 'env FOO=1 ' + ORDER, 'sudo -n ' + ORDER, 'bash -c \'%s\'' % ORDER,
                        'FOO=1 ' + ORDER, 'nohup ' + ORDER + ' &', 'python3 -u %s 7 --owner-go="merge it"' % SCRIPT,
                        '/usr/bin/python3 %s 7 --owner-go "merge it" --squash' % SCRIPT,
                        'cd /tmp && %s 7 --owner-go "merge it"' % SCRIPT,
                        'python3 ~/kit/bin/pr_review.py 7 --squash --owner-go "merge it"',
                        'echo go | xargs -I{} ' + ORDER, 'python3 %s https://github.com/a/b/pull/7 --owner-go "merge it"' % SCRIPT):
            with self.subTest(command):
                self.assert_asks(command, events)
                self.assert_allowed(command, [human('merge it')])

    def test_other_pr_review_calls_and_mentions_are_left_alone(self):
        events = [human('unrelated')]
        for command in ('python3 %s 7 auto' % SCRIPT, 'grep -n owner-go %s' % SCRIPT, 'grep -e --owner-go -- %s' % SCRIPT,
                        'git commit -m "pr_review.py --owner-go merges"', 'python3 other.py 7 --owner-go "merge it"',
                        'echo usage: pr_review.py 7 --owner-go "merge it"',
                        'printf "%%s\\n" pr_review.py 7 --owner-go "merge it"'):
            with self.subTest(command):
                self.assertIsNone(self.decide(command, events))

    def test_an_unverified_order_does_not_hide_what_follows_it(self):
        events = [human('unrelated')]
        for tail, reason in ((' && git push --force origin main', 'Force-pushing or deleting main/master is blocked'),
                             ('; gh pr merge 9 --admin', 'gh pr merge'), (' && rm -rf ~/.ssh', 'trash')):
            with self.subTest(tail):
                out = self.decide(ORDER + tail, events)
                self.assertEqual(out['permissionDecision'], 'deny', out)
                self.assertIn(reason, out['permissionDecisionReason'])
        out = self.decide('bash -c \'%s\' && git push --force origin main' % ORDER, events)
        self.assertEqual(out['permissionDecision'], 'deny', out)
        self.assert_asks(ORDER + ' && echo done', events)

    def test_the_users_words_with_a_later_deny_still_deny(self):
        out = self.decide(ORDER + ' && git push --force origin main', [human('merge it')])
        self.assertEqual(out['permissionDecision'], 'deny', out)

    # the matching rule

    def test_a_fragment_of_a_refusal_is_not_an_order(self):
        for said, words in (("don't merge it yet, the tests are red", 'merge it'), ('no, do not merge PR 7', 'merge PR 7'),
                            ('Do not merge it until I say so', 'merge it'), ('never merge it without a review', 'merge it'),
                            ("I won't merge it", 'merge it'), ('Don’t merge it', 'merge it')):
            with self.subTest(said):
                self.assert_sent_back('python3 %s 7 --owner-go "%s"' % (SCRIPT, words), [human(said)])

    def test_a_whole_refusal_is_never_an_order(self):
        for said in ("don't merge it yet", 'Do not merge it until I say so', 'dont merge it', 'never merge that',
                     'wait with the merge', 'hold the merge for now', 'Don’t merge it'):
            with self.subTest(said):
                self.assert_asks('python3 %s 7 --owner-go "%s"' % (SCRIPT, said), [human(said)])

    def test_a_part_of_a_sentence_is_sent_back_and_the_whole_sentence_passes(self):
        for said in ('ok so merge it and deploy and tell me', 'we should merge it after the fix lands',
                     'bump it to 1.5 and merge it', 'please merge it', 'merge it later'):
            with self.subTest(said):
                self.assert_sent_back(ORDER, [human(said)])
                self.assert_allowed('python3 %s 7 --owner-go "%s"' % (SCRIPT, said), [human(said)])
        self.assert_sent_back('python3 %s 7 --owner-go "5 and merge it"' % SCRIPT, [human('bump it to 1.5 and merge it')])
        self.assert_sent_back(ORDER, [human('merge it and deploy, then tell me')])
        self.assert_allowed('python3 %s 7 --owner-go "merge it and deploy"' % SCRIPT,
                            [human('Merge it and deploy, then tell me.')])
        meta = rollout('session_meta', {'id': 's', 'cwd': '/p', 'source': 'vscode', 'thread_source': 'user'})
        out = self.decide(ORDER, [meta, rollout('event_msg', {'type': 'user_message', 'message': 'please merge it'})],
                          env={'AGENT_CORE_TOOL': 'codex'})
        self.assertEqual(out['permissionDecision'], 'deny')
        self.assertIn('only part of a sentence %s wrote' % USER, out['permissionDecisionReason'])

    def test_the_latest_message_with_the_words_decides(self):
        self.assert_sent_back(ORDER, [human('merge it'), human("don't merge it yet")])
        self.assert_allowed(ORDER, [human("don't merge it yet"), human('ok, merge it')])
        self.assert_allowed(ORDER, [human('merge it'), human('and deploy afterwards')])

    def test_a_whole_clause_of_a_longer_message_is_an_order(self):
        for said in ('no worries, merge it', 'Merge it!', "I don't mind the delay.\nMerge it", 'Merge it, please'):
            with self.subTest(said):
                self.assert_allowed(ORDER, [human(said)])
        self.assert_allowed('python3 %s 7 --owner-go "yes, merge it"' % SCRIPT, [human('Yes, merge it.')])

    def test_one_short_word_is_not_an_order(self):
        for words, said in (('yes', 'yes'), ('go', 'go'), ('ok', 'ok'), ('go', 'what does "go" mean here?')):
            with self.subTest(words):
                self.assert_asks('python3 %s 7 --owner-go "%s"' % (SCRIPT, words), [human(said)])
        self.assert_allowed('python3 %s 7 --owner-go "merge"' % SCRIPT, [human('merge')])

    def test_two_orders_in_one_command_both_need_the_users_words(self):
        command = ORDER + ' && python3 %s 8 --owner-go "ship the other"' % SCRIPT
        self.assert_asks(command, [human('merge it')])
        self.assert_allowed(command, [human('merge it'), human('ship the other')])
        self.assert_sent_back(command, [human('merge it'), human('ship the other one')])


if __name__ == '__main__':
    unittest.main()
