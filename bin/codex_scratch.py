#!/usr/bin/env python3
"""A throwaway Codex home for tests: nothing it does can reach ~/.codex, GitHub or an MCP server.

  codex_scratch.py make <dir> [--guards] [--full-access]
                                                  build <dir>/{codex-home,home,repo,remote.git,sentinels};
                                                  --guards adds the kit's hooks (this checkout's scripts) and rules,
                                                  --full-access uses danger-full-access, for runs that must commit
                                                  (workspace-write cannot create index.lock in a linked worktree)
  codex_scratch.py exec <dir> [--hooks] -- <prompt>   run `codex exec --json` there; events go to <dir>/runs/
  codex_scratch.py app <dir> [--hooks] -- <prompt>    one turn through `codex app-server` (the protocol the desktop app
                                                  and the Claude plugin use), so the session is not an exec one
  codex_scratch.py count                          how many scratch Codex runs this machine has logged

The config keeps only the model, effort, personality and feature flags from ~/.codex/config.toml: no MCP servers,
plugins, apps (connectors), notify hooks or trusted projects. Sandbox is workspace-write, approvals never. HOME
points into <dir>/home, so ~/.agents/skills there is the scratch copy. auth.json is copied (mode 600) so the sign-in
works; the run never writes back to ~/.codex. --hooks passes --dangerously-bypass-hook-trust, which is allowed only
in a scratch home like this one (`app --hooks` sets the app-server's bypass_hook_trust override on its thread).
Scratch runs count against the Codex plan's limits.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('AGENT_CORE_DIR', CORE)
sys.path.insert(0, os.path.join(CORE, 'claude', 'hooks'))
import common  # noqa: E402

REAL = os.environ.get('CODEX_HOME') or os.path.join(common.HOME, '.codex')
KEEP = ('model', 'model_reasoning_effort', 'personality')
LOG = os.path.join(common.LEARN, 'codex-scratch-runs.log')  # inside local/, which the kit never commits
CODEX = shutil.which('codex')


def toml_value(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v))


def read_real_config():
    """The settings worth copying from the real config.toml: tomllib on Python 3.11+, else a plain-text reader for the
    few top-level strings and [features] booleans this script keeps. Empty when the file is missing."""
    try:
        with open(os.path.join(REAL, 'config.toml'), 'rb') as fh:
            raw = fh.read()
    except OSError:
        return {}
    if tomllib:
        try:
            return tomllib.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}
    out, table = {}, None
    for line in raw.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        head = re.match(r'^\[([^\]]+)\]$', line)
        if head:
            table = head.group(1).strip()
            continue
        kv = re.match(r'^([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*(#.*)?$', line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2)
        if val in ('true', 'false'):
            val = val == 'true'
        elif re.match(r'^"[^"]*"$', val):
            val = val[1:-1]
        else:
            continue
        if table is None and key in KEEP:
            out[key] = val
        elif table == 'features' and isinstance(val, bool):
            out.setdefault('features', {})[key] = val
    return out


def scratch_config(extra='', full_access=False):
    real = read_real_config()
    lines = ['# scratch Codex home made by agent-core-kit/bin/codex_scratch.py', 'approval_policy = "never"',
             'sandbox_mode = "%s"' % ('danger-full-access' if full_access else 'workspace-write')]
    lines += ['%s = %s' % (k, toml_value(real[k])) for k in KEEP if k in real]
    feats = {k: v for k, v in (real.get('features') or {}).items() if isinstance(v, bool)}
    # apps and plugins bring their own MCP servers (ChatGPT connectors); off here
    feats.update(memories=False, apps=False, plugins=False, remote_plugin=False)
    lines += ['', '[features]'] + ['%s = %s' % (k, toml_value(v)) for k, v in sorted(feats.items())]
    return '\n'.join(lines) + '\n' + extra


def git(*args, cwd=None):
    subprocess.run(['git'] + list(args), cwd=cwd, check=True, capture_output=True, text=True)


def add_guards(ch):
    """The kit's Codex hooks and rules, pointing at this checkout's scripts so a branch is tested before install."""
    sys.path.insert(0, os.path.join(CORE, 'bin'))
    import build
    build.build(quiet=True)
    os.makedirs(os.path.join(ch, 'rules'), exist_ok=True)
    for rules in glob.glob(os.path.join(CORE, 'build', 'codex', 'rules', '*.rules')):
        shutil.copyfile(rules, os.path.join(ch, 'rules', os.path.basename(rules)))
    hooks = json.dumps({'hooks': build.codex_hooks()}).replace(build.CODEX_HOOK_SCRIPT,
                                                               os.path.join(CORE, 'claude', 'hooks', 'codex_hook.py'))
    with open(os.path.join(ch, 'hooks.json'), 'w') as fh:
        fh.write(hooks)


def make(d, guards=False, full_access=False):
    d = os.path.abspath(d)
    ch, home, repo, remote, sent = (os.path.join(d, x) for x in ('codex-home', 'home', 'repo', 'remote.git', 'sentinels'))
    for p in (ch, home, sent, os.path.join(d, 'runs')):
        os.makedirs(p, exist_ok=True)
    repo_real = os.path.realpath(d) + '/repo'
    with open(os.path.join(ch, 'config.toml'), 'w') as fh:
        fh.write(scratch_config('\n[projects."%s"]\ntrust_level = "trusted"\n' % repo_real, full_access))
    if CODEX is None:
        print('codex_scratch: the codex CLI is not on PATH; the folders are built but nothing can run.', file=sys.stderr)
    if os.path.exists(os.path.join(REAL, 'auth.json')):
        shutil.copyfile(os.path.join(REAL, 'auth.json'), os.path.join(ch, 'auth.json'))
        os.chmod(os.path.join(ch, 'auth.json'), 0o600)
    if not os.path.isdir(remote):
        git('init', '-q', '--bare', '-b', 'main', remote)
    if not os.path.isdir(repo):
        git('init', '-q', '-b', 'main', repo)
        for k, v in (('user.name', 'Scratch'), ('user.email', 'scratch@example.invalid'), ('commit.gpgsign', 'false')):
            git('config', k, v, cwd=repo)
        with open(os.path.join(repo, 'README.md'), 'w') as fh:
            fh.write('# scratch repo\n')
        git('add', 'README.md', cwd=repo)
        git('commit', '-q', '-m', 'init', cwd=repo)
        git('remote', 'add', 'origin', remote, cwd=repo)
        git('push', '-q', 'origin', 'main', cwd=repo)
    if guards:
        add_guards(ch)  # builds first, so build/codex/AGENTS.md exists below
    built = os.path.join(CORE, 'build', 'codex', 'AGENTS.md')
    if os.path.exists(built):
        shutil.copyfile(built, os.path.join(ch, 'AGENTS.md'))
    with open(os.path.join(home, '.gitconfig'), 'w') as fh:
        fh.write('[user]\n\tname = Scratch\n\temail = scratch@example.invalid\n[init]\n\tdefaultBranch = main\n')
    print(json.dumps({'dir': d, 'CODEX_HOME': ch, 'HOME': home, 'repo': repo, 'remote': remote, 'sentinels': sent}))
    return 0


def env_for(d):
    d = os.path.abspath(d)
    env = dict(os.environ, CODEX_HOME=os.path.join(d, 'codex-home'), HOME=os.path.join(d, 'home'),
               AGENT_CORE_DIR=os.environ.get('AGENT_CORE_DIR', CORE), SCRATCH_SENTINELS=os.path.join(d, 'sentinels'))
    env.pop('CLAUDECODE', None)
    return env


def run(d, prompt, hooks=False, extra=()):
    d = os.path.abspath(d)
    if CODEX is None:
        print('codex_scratch: the codex CLI is not on PATH.', file=sys.stderr)
        return 2
    stamp = time.strftime('%Y%m%d-%H%M%S')
    events, last = os.path.join(d, 'runs', stamp + '.jsonl'), os.path.join(d, 'runs', stamp + '.last.txt')
    cmd = [CODEX, 'exec', '--json', '-C', os.path.join(d, 'repo'), '-s', 'workspace-write', '-o', last]
    if hooks:
        cmd.append('--dangerously-bypass-hook-trust')
    cmd += list(extra) + ['--', prompt]
    with open(events, 'w') as out:
        rc = subprocess.run(cmd, env=env_for(d), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            timeout=1800).returncode
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a') as fh:
        fh.write('%s rc=%d %s\n' % (stamp, rc, d))
    print(json.dumps({'rc': rc, 'events': events, 'last_message': last}))
    return rc


def app(d, prompt, hooks=False):
    """initialize, thread/start, turn/start over stdio JSON-RPC; waits for turn/completed. Approval requests are
    declined (the scratch config never asks for one)."""
    d = os.path.abspath(d)
    if CODEX is None:
        print('codex_scratch: the codex CLI is not on PATH.', file=sys.stderr)
        return 2
    stamp = time.strftime('%Y%m%d-%H%M%S')
    events = os.path.join(d, 'runs', stamp + '.app.jsonl')
    proc = subprocess.Popen([CODEX, 'app-server'], env=env_for(d), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    out, rc, next_id = open(events, 'w'), 1, [0]

    def send(method, params=None, notify=False):
        msg = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notify:
            next_id[0] += 1
            msg['id'] = next_id[0]
        proc.stdin.write(json.dumps(msg) + '\n')
        return msg.get('id')

    def wait(want_id=None, want_method=None, deadline=None):
        for raw in proc.stdout:
            out.write(raw)
            msg = json.loads(raw)
            if 'id' in msg and 'method' in msg:  # a request from the server, e.g. an approval
                proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'decision': 'decline'}})
                                 + '\n')
                continue
            if want_id is not None and msg.get('id') == want_id:
                if 'error' in msg:
                    raise RuntimeError(json.dumps(msg['error'])[:300])
                return msg.get('result') or {}
            if want_method and msg.get('method') == want_method:
                return msg.get('params') or {}
            if time.time() > deadline:
                raise TimeoutError(want_method or want_id)
        raise RuntimeError('app-server exited')

    try:
        end = time.time() + 1800
        wait(send('initialize', {'clientInfo': {'name': 'agent-core-kit-scratch', 'version': '1'}}), deadline=end)
        send('initialized', notify=True)
        start = {'cwd': os.path.join(d, 'repo')}
        if hooks:  # the app-server form of --dangerously-bypass-hook-trust; scratch homes only
            start['config'] = {'bypass_hook_trust': True}
        thread = wait(send('thread/start', start), deadline=end)['thread']['id']
        wait(send('turn/start', {'threadId': thread, 'input': [{'type': 'text', 'text': prompt}]}), deadline=end)
        done = wait(want_method='turn/completed', deadline=end)
        rc = 0 if (done.get('turn') or {}).get('status') in (None, 'completed') else 1
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.terminate()
        out.close()
    with open(LOG, 'a') as fh:
        fh.write('%s rc=%d app %s\n' % (stamp, rc, d))
    print(json.dumps({'rc': rc, 'events': events, 'thread': thread}))
    return rc


def main(argv):
    if len(argv) >= 2 and argv[0] == 'make':
        return make(argv[1], '--guards' in argv, '--full-access' in argv)
    if len(argv) >= 3 and argv[0] == 'exec':
        d, rest = argv[1], argv[2:]
        hooks = '--hooks' in rest
        rest = [a for a in rest if a != '--hooks']
        extra = rest[:rest.index('--')] if '--' in rest else []
        prompt = ' '.join(rest[rest.index('--') + 1:] if '--' in rest else rest)
        return run(d, prompt, hooks, extra)
    if len(argv) >= 3 and argv[0] == 'app':
        rest = argv[2:]
        prompt = ' '.join(rest[rest.index('--') + 1:] if '--' in rest else [a for a in rest if a != '--hooks'])
        return app(argv[1], prompt, '--hooks' in rest[:rest.index('--')] if '--' in rest else '--hooks' in rest)
    if argv[:1] == ['count']:
        n = sum(1 for _ in open(LOG)) if os.path.exists(LOG) else 0
        print('%d scratch Codex run(s) logged in %s' % (n, LOG))
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
