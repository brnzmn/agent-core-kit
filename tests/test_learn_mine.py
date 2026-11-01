"""learn.py mine reads Claude Code and Codex transcripts: both Codex rollout shapes, plain and .zst, only the turns
the user typed, each signal tagged with its tool. The review bundle, prune bundle and status all work on the user's
own files in local/ (rulebook plus personal adapters), never on the shipped adapters, and the bundle tells the
curator not to quote captured text."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN, env_for, make_local  # noqa: E402

CORRECTION = "No, that's wrong, I told you to stage files by name"
RULEBOOK = '# Rules\n\n## Git\n- Stage files by name.\n'  # 4 lines
HAVE_ZSTD = shutil.which('zstd') is not None


def slurp(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def line(kind, payload, ts='2026-10-28T02:00:00Z'):
    return json.dumps({'timestamp': ts, 'type': kind, 'payload': payload})


def meta(sid, **extra):
    return line('session_meta', dict({'id': sid, 'cwd': '/tmp/proj', 'source': 'vscode', 'thread_source': 'user',
                                      'history_mode': 'paginated'}, **extra))


def assistant(text):
    return line('response_item', {'type': 'message', 'role': 'assistant',
                                  'content': [{'type': 'output_text', 'text': text}]})


def user(text):
    return line('event_msg', {'type': 'item_completed',
                              'item': {'type': 'UserMessage', 'content': [{'type': 'text', 'text': text}]}})


class Mine(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='akit-home-')
        self.local = make_local(rulebook=False)
        with open(os.path.join(self.local, 'AGENTS.md'), 'w') as fh:
            fh.write(RULEBOOK)
        self.day = os.path.join(self.home, '.codex', 'sessions', '2026', '10', '28')
        os.makedirs(self.day)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(shutil.rmtree, self.local, True)

    def rollout(self, name, lines, zst=False):
        path = os.path.join(self.day, 'rollout-%s.jsonl' % name)
        with open(path, 'w') as fh:
            fh.write('\n'.join(lines) + '\n')
        if zst:
            subprocess.run(['zstd', '-q', '--rm', path], check=True)

    def learn(self, *args, **extra):
        env = env_for(self.local, HOME=self.home, **extra)
        env.pop('CODEX_HOME', None)
        return subprocess.run([sys.executable, os.path.join(BIN, 'learn.py')] + list(args), capture_output=True,
                              text=True, env=env, timeout=60)

    def inbox(self):
        d = os.path.join(self.local, 'learning', 'inbox')
        if not os.path.isdir(d):
            return []
        return [json.loads(l) for f in sorted(os.listdir(d)) if f.endswith('.jsonl')
                for l in slurp(os.path.join(d, f)).splitlines()]

    def bundle_path(self):
        out = self.learn('bundle')
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.split('bundle: ', 1)[1].split('\n', 1)[0]

    def test_codex_shapes_and_filters(self):
        self.rollout('paginated', [meta('s-pag'), assistant('I ran git add -A.'), user(CORRECTION)])
        if HAVE_ZSTD:
            self.rollout('zst', [meta('s-zst'), assistant('Done, want me to push?'),
                                 user('# Context from my IDE setup:\n\n## Open tabs:\n- a.py\n\n## My request for Codex:\n'
                                      + CORRECTION + ' in this repo')], zst=True)
        self.rollout('legacy', [meta('s-leg', history_mode='legacy'), assistant('Pushed.'),
                                line('event_msg', {'type': 'user_message', 'message': CORRECTION + ' again'})])
        self.rollout('interrupt', [meta('s-int'), assistant('Deleting the folder now.'),
                                   line('event_msg', {'type': 'turn_aborted', 'reason': 'interrupted'}),
                                   user('stop, use trash.py instead')])
        self.rollout('subagent', [meta('s-sub', source={'subagent': {'thread_spawn': {}}}, thread_source='subagent',
                                       parent_thread_id='s-pag'), user(CORRECTION)])
        self.rollout('exec', [meta('s-exec', source='exec'), user(CORRECTION)])
        self.rollout('injected', [meta('s-inj'), user('<heartbeat>\n  ' + CORRECTION + '\n</heartbeat>'),
                                  user('<codex_delegation>' + CORRECTION + '</codex_delegation>')])
        out = self.learn('mine')
        self.assertEqual(out.returncode, 0, out.stderr)
        recs = {r['session']: r for r in self.inbox()}
        self.assertEqual(sorted(recs), sorted(['s-int', 's-leg', 's-pag'] + (['s-zst'] if HAVE_ZSTD else [])))
        self.assertTrue(all(r['tool'] == 'codex' for r in recs.values()))
        self.assertEqual(recs['s-pag']['context'], 'I ran git add -A.')
        if HAVE_ZSTD:
            self.assertTrue(recs['s-zst']['text'].startswith("No, that's wrong"))
        self.assertIn('after-interrupt', recs['s-int']['signals'])
        self.assertEqual(recs['s-int']['context'], 'Deleting the folder now.')
        self.assertEqual(recs['s-pag']['cwd'], '/tmp/proj')

        self.assertIn('mined 0 new', self.learn('mine').stdout)
        with open(os.path.join(self.day, 'rollout-paginated.jsonl'), 'a') as fh:
            fh.write(user("That's not what I asked, wrong branch again") + '\n')
        self.assertIn('mined 1 new', self.learn('mine').stdout)

    def test_zst_rollouts_are_skipped_with_one_note_when_zstd_is_missing(self):
        self.rollout('plain', [meta('s-plain'), assistant('ok'), user(CORRECTION)])
        for name in ('a', 'b'):
            with open(os.path.join(self.day, 'rollout-%s.jsonl.zst' % name), 'wb') as fh:
                fh.write(b'not really zstd')
        no_zstd = tempfile.mkdtemp(prefix='akit-empty-path-')  # nothing on PATH, whatever this machine has installed
        self.addCleanup(shutil.rmtree, no_zstd, True)
        for _ in range(2):  # the note is per run
            out = self.learn('mine', PATH=no_zstd)
            self.assertEqual((out.returncode, out.stderr), (0, ''), out.stderr)
            self.assertEqual([l for l in out.stdout.splitlines() if 'compressed' in l],
                             ['2 compressed Codex sessions skipped: install zstd to include them'])
            self.assertEqual([r['session'] for r in self.inbox()], ['s-plain'])
        offsets = json.loads(slurp(os.path.join(self.local, 'learning', '.state.json')))['mine_offsets']
        self.assertEqual([p for p in offsets if p.endswith('.zst')], [], 'skipped files stay unread for later')

    def test_no_note_when_there_are_no_compressed_sessions(self):
        self.rollout('plain', [meta('s-plain'), assistant('ok'), user(CORRECTION)])
        out = self.learn('mine', PATH=tempfile.gettempdir())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn('compressed', out.stdout)

    def test_claude_signals_are_tagged_claude(self):
        proj = os.path.join(self.home, '.claude', 'projects', '-tmp-proj')
        os.makedirs(proj)
        with open(os.path.join(proj, 'abc.jsonl'), 'w') as fh:
            fh.write(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Done.'}]}}) + '\n')
            fh.write(json.dumps({'type': 'user', 'sessionId': 'abc', 'cwd': '/tmp/proj',
                                 'message': {'content': CORRECTION}}) + '\n')
        self.learn('mine')
        self.assertEqual([(r['tool'], r['session']) for r in self.inbox()], [('claude', 'abc')])

    def test_add_logs_a_manual_lesson_into_local(self):
        out = self.learn('add', 'staged everything / wanted files by name -> stage by name')
        self.assertEqual((out.returncode, out.stdout.strip()), (0, 'logged'))
        recs = self.inbox()
        self.assertEqual([(r['kind'], r['signals']) for r in recs], [('manual', ['manual'])])

    def test_bundle_tags_codex_and_keeps_captured_text_out_of_proposals(self):
        self.rollout('paginated', [meta('s-pag'), assistant('ok'), user(CORRECTION)])
        self.learn('mine')
        text = slurp(self.bundle_path())
        self.assertIn('| codex | mined |', text)
        self.assertIn('personal or client data', text)
        self.assertIn('cite the signal number', text)
        self.assertIn(CORRECTION, text)  # the curator reads the signal; the proposal must not repeat it

    def test_bundle_scope_is_the_users_files_not_the_shipped_adapters(self):
        self.rollout('paginated', [meta('s-pag'), assistant('ok'), user(CORRECTION)])
        self.learn('mine')
        text = slurp(self.bundle_path())
        self.assertIn('- target AGENTS.md#<section>: %s (the rulebook shared by Claude Code and Codex; 4/120 lines)'
                      % os.path.join(self.local, 'AGENTS.md'), text)
        self.assertIn('- target claude.md#<section>: %s (not created yet' % os.path.join(self.local, 'claude.md'), text)
        self.assertIn('- target codex.md#<section>: %s (not created yet' % os.path.join(self.local, 'codex.md'), text)
        self.assertNotIn(os.path.join(os.path.dirname(BIN), 'adapters'), text)
        self.assertIn(os.path.join(self.local, 'learning', 'ledger.md'), text)

        with open(os.path.join(self.local, 'claude.md'), 'w') as fh:
            fh.write('## Mine\n- one\n')
        text = slurp(self.bundle_path())
        self.assertIn('- target claude.md#<section>: %s (personal additions for one tool; 2/60 lines)'
                      % os.path.join(self.local, 'claude.md'), text)
        self.assertIn('- target codex.md#<section>: %s (not created yet' % os.path.join(self.local, 'codex.md'), text)

    def test_bundle_lists_the_repo_files_of_the_signal(self):
        repo = os.path.join(self.home, 'repo')
        os.makedirs(os.path.join(repo, '.git'))
        os.makedirs(os.path.join(repo, '.claude', 'rules'))
        with open(os.path.join(repo, 'AGENTS.md'), 'w') as fh:
            fh.write('# Repo\n- a\n')
        with open(os.path.join(repo, '.claude', 'rules', 'api.md'), 'w') as fh:
            fh.write('- b\n')
        self.rollout('inrepo', [meta('s-repo', cwd=repo), assistant('ok'), user(CORRECTION)])
        self.learn('mine')
        text = slurp(self.bundle_path())
        self.assertIn('- target repo:%s#<section>: 2/150 lines' % os.path.join(repo, 'AGENTS.md'), text)
        self.assertIn('- target repo:%s#<section>: 1/80 lines' % os.path.join(repo, '.claude', 'rules', 'api.md'), text)

    def test_prune_bundle_and_status_use_the_same_files(self):
        with open(os.path.join(self.local, 'codex.md'), 'w') as fh:
            fh.write('## Codex notes\n- one\n')
        ledger = os.path.join(self.local, 'learning', 'ledger.md')
        os.makedirs(os.path.dirname(ledger))
        with open(ledger, 'w') as fh:
            fh.write('# Learning ledger\n\n| date | id | status | action | target | rule | why | review-by |\n'
                     '|---|---|---|---|---|---|---|---|\n'
                     '| 2026-01-02 | 2026-01-01/P1 | applied | add | AGENTS.md#Rules | Old rule | Because | 2026-04-02 |\n')
        out = self.learn('prune-bundle')
        self.assertEqual(out.returncode, 0, out.stderr)
        text = slurp(out.stdout.split('bundle: ', 1)[1].split('\n', 1)[0])
        self.assertIn('2026-01-01/P1 | AGENTS.md#Rules | Old rule | why: Because', text)
        self.assertIn('%s: 4/120 lines' % os.path.join(self.local, 'AGENTS.md'), text)
        self.assertIn('%s: 2/60 lines' % os.path.join(self.local, 'codex.md'), text)
        self.assertNotIn('claude.md', text)
        self.assertNotIn(os.path.join(os.path.dirname(BIN), 'adapters'), text)
        status = self.learn('status').stdout
        self.assertIn('ledger: 1 applied, 0 rejected, 1 due for review', status)
        self.assertIn('AGENTS.md: 4/120 lines', status)
        self.assertIn('codex.md: 2/60 lines', status)
        self.assertNotIn('claude.md', status)

    def test_close_marks_the_bundle_reviewed_so_the_next_bundle_is_empty(self):
        self.rollout('paginated', [meta('s-pag'), assistant('ok'), user(CORRECTION)])
        self.learn('mine')
        bundle = self.bundle_path()
        closed = self.learn('close', os.path.dirname(bundle))
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertIn('closed 1 signal(s)', closed.stdout)
        self.assertIn('nothing to review', self.learn('bundle').stdout)


def load_learn():
    """learn.py imported on its own, leaving sys.modules, sys.path and the environment as they were."""
    saved = (dict(os.environ), list(sys.path), sys.modules.get('common'))
    try:
        spec = importlib.util.spec_from_file_location('learn_under_test', os.path.join(BIN, 'learn.py'))
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


class MetaCallSite(unittest.TestCase):
    def test_a_rollout_whose_meta_needs_a_missing_zstd_is_skipped_not_raised(self):
        learn = load_learn()
        turns = []
        miner = mock.Mock(turn=lambda *a: turns.append(a))
        for failure in (FileNotFoundError(2, 'No such file or directory', 'zstd'), PermissionError(13, 'denied', 'zstd')):
            with mock.patch.object(learn.common, 'codex_meta', side_effect=failure):
                self.assertIsNone(learn.mine_codex('/x/rollout-1.jsonl.zst', [user(CORRECTION)], miner))
        self.assertEqual(turns, [])


if __name__ == '__main__':
    unittest.main()
