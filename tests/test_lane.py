"""Lane ownership: a worktree whose .agent-lane names one tool refuses commits from the other, in both tools' guards;
without a lane file everything is allowed."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN, HOOKS, env_for  # noqa: E402


def git(*args, cwd=None):
    subprocess.run(['git'] + list(args), cwd=cwd, check=True, capture_output=True, text=True)


def decision(tool, command, cwd):
    return verdict(tool, command, cwd)[0]


def verdict(tool, command, cwd):
    """(permissionDecision or None, reason or None) for a Bash command run in cwd by the given tool's guard."""
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': cwd}
    script = ['guard.py'] if tool == 'claude' else ['codex_hook.py', 'pre-bash']
    env = env_for()
    env.pop('AGENT_CORE_TOOL', None)
    out = subprocess.run([sys.executable, os.path.join(HOOKS, script[0])] + script[1:], input=json.dumps(payload),
                         capture_output=True, text=True, env=env, timeout=30).stdout.strip()
    if not out:
        return None, None
    hook = json.loads(out)['hookSpecificOutput']
    return hook['permissionDecision'], hook['permissionDecisionReason']


class Lane(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp()
        self.repo, self.wt, self.free = (os.path.join(base, n) for n in ('repo', 'lane', 'free'))
        git('init', '-q', '-b', 'main', self.repo)
        for k, v in (('user.name', 'T'), ('user.email', 't@example.invalid'), ('commit.gpgsign', 'false')):
            git('config', k, v, cwd=self.repo)
        git('commit', '-q', '--allow-empty', '-m', 'init', cwd=self.repo)
        git('worktree', 'add', '-q', '-b', 'lane', self.wt, cwd=self.repo)
        git('worktree', 'add', '-q', '-b', 'free', self.free, cwd=self.repo)
        out = subprocess.run(['bash', os.path.join(BIN, 'lane.sh'), self.wt, 'codex', 's1'], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_lane_file_is_written_and_kept_out_of_git(self):
        with open(os.path.join(self.wt, '.agent-lane')) as fh:
            text = fh.read()
        for want in ('tool: codex', 'session: s1', 'branch: lane', 'started: '):
            self.assertIn(want, text)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=self.wt, capture_output=True, text=True).stdout
        self.assertEqual(status, '')
        again = subprocess.run(['bash', os.path.join(BIN, 'lane.sh'), self.wt, 'claude'], capture_output=True, text=True)
        self.assertEqual(again.returncode, 1)

    def test_other_tool_cannot_commit_in_the_lane(self):
        self.assertEqual(decision('claude', 'git commit -m x', self.wt), 'deny')
        self.assertEqual(decision('claude', 'git -C %s commit -m x' % self.wt, self.repo), 'deny')
        self.assertEqual(decision('claude', 'cd %s && git merge main' % self.wt, self.repo), 'deny')
        self.assertEqual(decision('claude', 'git commit -m x', os.path.join(self.wt, 'sub')), 'deny')

    def test_codex_cannot_commit_in_a_claude_lane(self):
        claude_wt = os.path.join(os.path.dirname(self.repo), 'claude-lane')
        git('worktree', 'add', '-q', '-b', 'claude-lane', claude_wt, cwd=self.repo)
        out = subprocess.run(['bash', os.path.join(BIN, 'lane.sh'), claude_wt, 'claude', 's2'], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(decision('codex', 'git commit -m x', claude_wt), 'deny')
        self.assertIsNone(decision('claude', 'git commit -m x', claude_wt))

    def test_denial_says_what_to_do_instead(self):
        why = verdict('claude', 'git commit -m x', self.wt)[1]
        self.assertIn('.agent-lane names the other tool', why)
        self.assertIn('handoff', why)

    def test_every_history_changing_command_is_held(self):
        for cmd in ('git merge main', 'git cherry-pick abc1234', 'git revert HEAD', 'git am 0001.patch', 'git rebase main'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision('claude', cmd, self.wt), 'deny')

    def test_agent_core_tool_env_names_the_caller(self):
        payload = {'tool_name': 'Bash', 'tool_input': {'command': 'git commit -m x'}, 'cwd': self.wt}
        for tool, want in (('codex', ''), ('claude', 'deny')):
            out = subprocess.run([sys.executable, os.path.join(HOOKS, 'guard.py')], input=json.dumps(payload),
                                 capture_output=True, text=True, env=env_for(AGENT_CORE_TOOL=tool), timeout=30).stdout
            got = json.loads(out)['hookSpecificOutput']['permissionDecision'] if out.strip() else ''
            self.assertEqual(got, want, tool)

    def test_a_cd_inside_a_group_or_a_child_shell_does_not_move_the_outer_folder(self):
        self.assertEqual(decision('claude', '(cd %s && git commit -m x)' % self.wt, self.free), 'deny')
        self.assertEqual(decision('claude', 'bash -c "cd %s && git commit -m x"' % self.wt, self.free), 'deny')
        self.assertEqual(decision('claude', '(cd %s); git commit -m x' % self.free, self.wt), 'deny')  # still in the lane
        self.assertEqual(decision('claude', 'bash -c "cd %s"; git commit -m x' % self.free, self.wt), 'deny')
        self.assertIsNone(decision('claude', '(cd %s); git commit -m x' % self.wt, self.free))  # the outer folder is free

    def test_owner_and_unmarked_worktrees_are_free(self):
        self.assertIsNone(decision('codex', 'git commit -m x', self.wt))
        self.assertIsNone(decision('claude', 'git commit -m x', self.free))
        self.assertIsNone(decision('codex', 'git commit -m x', self.free))
        self.assertIsNone(decision('claude', 'git status && git diff', self.wt))


if __name__ == '__main__':
    unittest.main()
