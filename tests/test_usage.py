"""usage.py reads Claude's /usage text and Codex's rollout rate_limits, names a missing number instead of guessing,
and routes new lanes by the threshold in config (routing.claude_week_limit_pct)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN, TEST_CONFIG, make_local  # noqa: E402

USAGE = ('Current session: 6%% used · resets Oct 28 at 5:49am\nCurrent week (all models): %d%% used · resets Oct 28 at '
         '12:59pm\nCurrent week (Fable): 70%% used')


class Usage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bin = os.path.join(self.dir, 'bin')
        os.makedirs(self.bin)
        self.codex = os.path.join(self.dir, 'codex')

    def fake_claude(self, week):
        reply = os.path.join(self.dir, 'reply.json')
        with open(reply, 'w') as fh:
            json.dump({'result': USAGE % week}, fh)
        with open(os.path.join(self.bin, 'claude'), 'w') as fh:
            fh.write('#!/bin/sh\ncat "%s"\n' % reply)
        os.chmod(os.path.join(self.bin, 'claude'), 0o755)

    def rollout(self, used):
        day = os.path.join(self.codex, 'sessions', '2026', '10', '28')
        os.makedirs(day, exist_ok=True)
        ev = {'timestamp': '2026-10-28T01:00:00Z', 'type': 'event_msg', 'payload': {'type': 'token_count',
              'rate_limits': {'primary': {'used_percent': used, 'window_minutes': 10080, 'resets_at': time.time() + 3600},
                              'plan_type': 'pro'}}}
        with open(os.path.join(day, 'rollout-x.jsonl'), 'w') as fh:
            fh.write(json.dumps(ev) + '\n')

    def run_usage(self, config=None, *args):
        local = make_local(dict(TEST_CONFIG, **(config or {})))
        env = dict(os.environ, PATH=self.bin + ':/usr/bin:/bin', CODEX_HOME=self.codex, HOME=self.dir,
                   AGENT_CORE_LOCAL=local)
        env.pop('AGENT_CORE_DIR', None)
        out = subprocess.run([sys.executable, os.path.join(BIN, 'usage.py')] + list(args), capture_output=True, text=True,
                             env=env, timeout=15)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout) if '--json' in args else out.stdout

    def test_past_the_limit_routes_to_codex(self):
        self.fake_claude(94)
        self.rollout(4.0)
        got = self.run_usage(None, '--json')
        self.assertEqual((got['claude']['week'], got['claude']['session'], got['codex']['week']), (94.0, 6.0, 4.0))
        self.assertIn('go to Codex', got['route'])

    def test_below_the_limit_stays_and_both_high_stops(self):
        self.fake_claude(40)
        self.rollout(90.0)
        self.assertIn('stay on Claude', self.run_usage(None, '--json')['route'])
        self.fake_claude(85)
        route = self.run_usage(None, '--json')['route']
        self.assertIn('both past 80%', route)
        self.assertIn('tell Alex', route)

    def test_threshold_comes_from_config(self):
        self.fake_claude(60)
        self.rollout(1.0)
        self.assertIn('stay on Claude', self.run_usage(None, '--json')['route'])
        got = self.run_usage({'routing': {'claude_week_limit_pct': 50}}, '--json')
        self.assertIn('go to Codex', got['route'])
        self.assertIn('past 50%', got['route'])
        self.assertEqual(got['limit_pct'], 50)

    def test_unusable_threshold_falls_back_to_80(self):
        self.fake_claude(85)
        self.rollout(1.0)
        got = self.run_usage({'routing': {'claude_week_limit_pct': 'soon'}}, '--json')
        self.assertEqual(got['limit_pct'], 80)
        self.assertIn('go to Codex', got['route'])

    def test_no_user_name_says_the_user(self):
        self.fake_claude(95)
        self.rollout(95.0)
        route = self.run_usage({'user_name': ''}, '--json')['route']
        self.assertIn('tell the user', route)

    def test_missing_numbers_are_named(self):
        got = self.run_usage(None, '--json')
        self.assertIn('not on PATH', got['claude']['error'])
        self.assertIn('no Codex sessions folder', got['codex']['error'])
        self.assertIn('unknown', got['route'])

    def test_claude_without_a_weekly_figure_is_named(self):
        reply = os.path.join(self.dir, 'reply.json')
        with open(reply, 'w') as fh:
            json.dump({'result': 'Current session: 3% used'}, fh)
        with open(os.path.join(self.bin, 'claude'), 'w') as fh:
            fh.write('#!/bin/sh\ncat "%s"\n' % reply)
        os.chmod(os.path.join(self.bin, 'claude'), 0o755)
        got = self.run_usage(None, '--json')
        self.assertIn('no weekly figure', got['claude']['error'])
        self.assertIn('unknown', got['route'])

    def test_text_output_has_three_lines(self):
        self.fake_claude(94)
        self.rollout(4.0)
        lines = self.run_usage().strip().splitlines()
        self.assertEqual([l.split(':')[0] for l in lines], ['Claude', 'Codex', 'Route'])
        self.assertIn('week 94% used', lines[0])
        self.assertIn('plan pro', lines[1])


if __name__ == '__main__':
    unittest.main()
