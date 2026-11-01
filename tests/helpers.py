"""Run agent-core-kit hooks the way Claude Code does: JSON on stdin, decision JSON on stdout.

Tests never touch the developer's own local/ folder: every hook run gets AGENT_CORE_LOCAL pointing at a throwaway
folder made by make_local(), with a test config (protected_dirs, protected_supabase, user_name) and a rulebook.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(CORE, 'claude', 'hooks')
BIN = os.path.join(CORE, 'bin')
TEST_CONFIG = {
    'user_name': 'Alex',
    'protected_dirs': ['lake-data', '~/vault-data'],
    'protected_supabase': {'abcdefghijklmnopqrst': 'client database: schema changes only through scripts/deploy'},
    'gated_models': ['fable'],
    'pr_gate': {'enabled': True, 'branches': ['main', 'master', 'production'], 'repos': '*'},
}


def make_local(config=None, rulebook=True):
    """A fresh LOCAL folder; returns its path. config=None uses TEST_CONFIG."""
    local = tempfile.mkdtemp(prefix='akit-local-')
    with open(os.path.join(local, 'config.json'), 'w', encoding='utf-8') as fh:
        json.dump(TEST_CONFIG if config is None else config, fh)
    template = os.path.join(CORE, 'templates', 'core', 'AGENTS.md')
    if rulebook and os.path.isfile(template):
        shutil.copyfile(template, os.path.join(local, 'AGENTS.md'))
    return local


LOCAL = make_local()


def env_for(local=None, **extra):
    return dict(os.environ, AGENT_CORE_DIR=CORE, AGENT_CORE_LOCAL=local or LOCAL, **extra)


def run_hook(name, payload, env=None, local=None):
    # Claude Code starts a hook in the session's folder, so the hook's own cwd follows the payload, not the test runner
    where = payload.get('cwd') if os.path.isdir(str(payload.get('cwd'))) else None
    out = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps(payload), cwd=where,
                         capture_output=True, text=True, env=env_for(local, **(env or {})), timeout=20)
    return json.loads(out.stdout) if out.stdout.strip() else None


def bash(command, cwd=None, local=None, env=None):
    """(decision or None, full output) for a Bash command through guard.py."""
    out = run_hook('guard.py', {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': cwd or CORE},
                   env=env, local=local)
    if not out:
        return None, None
    return out['hookSpecificOutput']['permissionDecision'], out


def tool(name, tool_input, cwd=None, local=None):
    out = run_hook('guard.py', {'tool_name': name, 'tool_input': tool_input, 'cwd': cwd or CORE}, local=local)
    return (out['hookSpecificOutput']['permissionDecision'] if out else None), out
