#!/usr/bin/env python3
"""agent-core hooks for Codex: one thin adapter in front of the same scripts Claude Code runs.

  codex_hook.py pre-bash        PreToolUse Bash -> guard.py (deny with the alternative; a lone rm -r becomes trash.py),
                                as tool codex for the .agent-lane check
  codex_hook.py pre-patch       PreToolUse apply_patch -> budget.py, once per file the patch touches
  codex_hook.py stop            Stop -> stop_steps.py, with Codex update_plan calls read as the task list; does nothing
                                when config stop_hook is false
  codex_hook.py session-start   SessionStart -> session_start.py (plain stdout becomes developer context)
  codex_hook.py prompt          UserPromptSubmit -> capture.py, signals tagged tool=codex; only turns the user typed
                                (not exec, subagent or guardian sessions, heartbeats or delegations), as learn.py mine

Codex sends the same JSON shape as Claude Code for these events (checked on codex-cli 0.163): the shell tool is
tool_name "Bash" with tool_input.command a string; edits are tool_name "apply_patch" with the raw patch in
tool_input.command. Decisions go back in Claude's format, which Codex reads. Prints nothing to allow; its own
errors never block a call (they go to learning/hook-errors.log).
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common  # noqa: E402

FILE_OP = re.compile(r'^\*\*\* (Add|Update|Delete) File: (.+)$')
MOVE_TO = re.compile(r'^\*\*\* Move to: (.+)$')


def run(script, payload, extra_env=None):
    out = subprocess.run([sys.executable, os.path.join(HERE, script)], input=json.dumps(payload), capture_output=True,
                         text=True, timeout=20, env=dict(os.environ, **(extra_env or {})))
    return out.stdout.strip()


def patch_files(patch):
    """[(op, path, hunks)] from an apply_patch body; hunks are lists of (' '|'-'|'+', text)."""
    files, cur = [], None
    for line in patch.split('\n'):
        m = FILE_OP.match(line)
        if m:
            cur = [m.group(1), m.group(2).strip(), [[]]]
            files.append(cur)
            continue
        if cur is None or line.startswith('*** End'):
            continue
        m = MOVE_TO.match(line)
        if m:
            cur[1] = m.group(1).strip()
        elif line.startswith('@@'):
            cur[2].append([])
        elif line[:1] in (' ', '-', '+'):
            cur[2][-1].append((line[0], line[1:]))
        elif line == '':
            cur[2][-1].append((' ', ''))
    return [(op, path, [h for h in hunks if h]) for op, path, hunks in files]


def apply_hunks(old, hunks):
    """The file after the patch. A hunk whose context is not found adds its new lines at the end, which still
    counts lines and finds added log entries correctly."""
    lines, pos = old.split('\n'), 0
    for hunk in hunks:
        before = [t for k, t in hunk if k in ' -']
        after = [t for k, t in hunk if k in ' +']
        at = next((i for i in range(pos, len(lines) - len(before) + 1) if lines[i:i + len(before)] == before), None)
        if at is None or not before:
            lines += [t for k, t in hunk if k == '+']
            continue
        lines[at:at + len(before)] = after
        pos = at + len(after)
    return '\n'.join(lines)


def pre_patch(data):
    patch = str((data.get('tool_input') or {}).get('command') or '')
    cwd = data.get('cwd') or os.getcwd()
    for op, path, hunks in patch_files(patch):
        if op == 'Delete':
            continue
        full = os.path.join(cwd, os.path.expanduser(path))
        if op == 'Add':
            new = '\n'.join(t for k, t in (hunks[0] if hunks else []) if k == '+') + '\n'
        else:
            try:
                new = apply_hunks(common.read(full), hunks)
            except OSError:
                new = '\n'.join(t for h in hunks for k, t in h if k == '+')
        out = run('budget.py', {'tool_name': 'Write', 'tool_input': {'file_path': full, 'content': new}, 'cwd': cwd})
        if out:
            return out
    return ''


def plan_events(path):
    """Codex update_plan calls as Claude TodoWrite events, so stop_steps sees open tasks the same way."""
    events = []
    try:
        with open(os.path.expanduser(path), encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if 'update_plan' not in line:
                    continue
                try:
                    p = json.loads(line).get('payload') or {}
                    args = p.get('arguments') if 'arguments' in p else p.get('input')
                    args = json.loads(args) if isinstance(args, str) else args
                except ValueError:
                    continue
                if p.get('name') != 'update_plan' or not isinstance(args, dict):
                    continue
                todos = [{'content': i.get('step') or '', 'status': i.get('status') or 'pending'}
                         for i in args.get('plan') or [] if isinstance(i, dict)]
                events.append({'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'name': 'TodoWrite', 'input': {'todos': todos}}]}})
    except OSError:
        pass
    return events


def stop(data):
    import stop_steps
    if data.get('stop_hook_active') or not common.config().get('stop_hook', True):
        return ''
    events = plan_events(data['transcript_path']) if data.get('transcript_path') else []
    hit = stop_steps.decide(data.get('last_assistant_message') or '', events)
    if not hit:
        return ''
    return json.dumps({'decision': 'block', 'reason': stop_steps.reason(*hit)})


def prompt(data):
    meta = common.codex_meta(data['transcript_path']) if data.get('transcript_path') else None
    if meta and not common.codex_typed(meta):
        return ''
    text = common.codex_user_text(str(data.get('prompt') or ''))
    if not text:
        return ''
    return run('capture.py', dict(data, prompt=text), {'AGENT_CORE_TOOL': 'codex'})


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    try:
        if mode == 'pre-bash':
            out = run('guard.py', data, {'AGENT_CORE_TOOL': 'codex'}) if data.get('tool_name') == 'Bash' else ''
        elif mode == 'pre-patch':
            out = pre_patch(data) if data.get('tool_name') == 'apply_patch' else ''
        elif mode == 'stop':
            out = stop(data)
        elif mode == 'session-start':
            out = run('session_start.py', data)
        elif mode == 'prompt':
            out = prompt(data)
        else:
            out = ''
        if out:
            print(out)
    except Exception as exc:
        common.log_error('codex_hook ' + mode, exc)


if __name__ == '__main__':
    main()
