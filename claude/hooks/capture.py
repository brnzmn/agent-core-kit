#!/usr/bin/env python3
"""agent-core capture hook for UserPromptSubmit and PermissionDenied.

Logs likely corrections and auto-mode denials to local/learning/inbox (inside the kit's personal folder), tagged
with the tool (AGENT_CORE_TOOL, set to codex by codex_hook.py; claude otherwise).
Prints nothing: anything this hook prints on UserPromptSubmit would be added to
Claude's context.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    try:
        if not os.path.isdir(common.LOCAL):
            return
        event = data.get('hook_event_name') or ''
        base = {'session': data.get('session_id') or '', 'cwd': data.get('cwd') or '',
                'tool': os.environ.get('AGENT_CORE_TOOL') or 'claude'}
        if event == 'UserPromptSubmit':
            text = data.get('prompt') or ''
            score, signals = common.score_prompt(text)
            if score >= 2:
                common.append_inbox(dict(base, kind='prompt', score=score, signals=signals, text=text[:800]))
        elif event == 'PermissionDenied':
            tool_input = data.get('tool_input') or {}
            summary = tool_input.get('command') or tool_input.get('file_path') or json.dumps(tool_input)[:300]
            common.append_inbox(dict(base, kind='permission-denied', score=1, signals=[data.get('tool_name') or ''],
                                     text=str(summary)[:400], reason=str(data.get('reason') or '')[:300]))
    except Exception as exc:
        common.log_error('capture', exc)


if __name__ == '__main__':
    main()
