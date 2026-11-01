#!/usr/bin/env python3
"""agent-core Stop hook: push back once when a turn ends by handing work to the user.

Blocks the stop when the last assistant message
- has 3 or more instructions to the user (run, open, click, log in, paste, "you need to", ...),
  numbered, bulleted or written as prose;
- ends on an offer instead of the work ("want me to", "should I", "say the word", "reply go"); a question that
  offers alternatives ("Should I use Postgres or SQLite?", or a question followed by a list of options) is a
  clarifying question and passes;
- or the task list (TodoWrite, TaskCreate/TaskUpdate, the desktop app's plan) still has open
  items and the message names no blocker.
A line starting with "Blocked:" or "Needs you:" names the blocker and lets the stop through.
Lets the stop through when stop_hook_active is set, so it nudges at most once per turn, and does nothing at all
when config stop_hook is false.
Its own errors never block; they go to learning/hook-errors.log.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

VERBS = (r'run|open|click|log ?in|sign ?in|paste|go to|navigate|visit|copy|enter|type|install|select|choose|create|'
         r'add|set|press|enable|turn on|download|upload|restart|export|update|change|delete|remove|replace|edit|'
         r'check|confirm|approve|grant|allow|launch|start|stop|quit|reboot|execute|deploy|push|merge|reply|send|'
         r'drag|tap|switch|toggle|configure|generate|rotate|save')
LEAD = r'^\s*(?:\d+[.)]\s+|[-*+]\s+)?\**(?:(?:then|next|finally|first|after that|now|and)[,:]?\s+)?'
STEP = re.compile(LEAD + r'(?:(?:you(?:\'ll| will)? (?:need|have|want) to|you should|you must|you can now|please)\s+)?'
                  r'(' + VERBS + r')\b', re.I)
SENTENCE = re.compile(r'\n|(?<=[.!?])\s+')
LABEL = re.compile(r'^\s*(?:\d+[.)]\s+|[-*+]\s+)?\*\*[^*]+[.:]\*\*')  # "**Stop hook.** It now ..."
NARRATION = re.compile(r',\s+(it|this|that|they|we|i|he|the \w+)\s+(would|was|is|has|had|will|did)\b', re.I)


def is_step(unit):
    """An instruction to the user: starts with a capitalised imperative, not a label or a narrated result."""
    m = STEP.match(unit)
    first = re.search(r'[A-Za-z]', re.sub(r'^\s*(?:\d+[.)]\s+|[-*+]\s+)', '', unit))
    return bool(m and first and first.group(0).isupper() and not LABEL.match(unit) and not NARRATION.search(unit))
OFFER = re.compile(r"\b(want me to|would you like me to|do you want me to|should i|shall i|say the word|"
                   r"reply (with )?[\"'`*]*go\b|just say (the word|go|yes)|let me know if you('d| would)? "
                   r"(like|want)|if you('d| would) like,? i can|happy to (do|take|run|help)|ready when you are)", re.I)
OR_WORD = re.compile(r'\sor\s', re.I)
YES_NO = re.compile(r'\b(yes|y)\s+or\s+(no|n)\b', re.I)
LIST_ITEM = re.compile(r'^\s*(?:[-*+\u2022]|\d+[.)]|\(?[a-d][.)])\s+\S')
BLOCKER = re.compile(r'^\s*[-*]?\s*\**(blocked|blocker|needs you)\**\s*:', re.I | re.M)
CODE = re.compile(r'```.*?(```|\Z)', re.S)

REASONS = {
    'steps': ('This turn ends on %d instructions for {user}. Execute every step you can yourself. If one is truly '
              'impossible (purchase over ${limit}, a credential only they hold, a message sent as them), keep only '
              'that one on a line starting "Needs you:" with the exact setting or credential that would unblock it.'),
    'offer': ('This turn ends on an offer ("%s"). Do the work instead of offering it; {user}\'s standing '
              'authorization in the global instructions covers what it lists, and spend up to ${limit} per action. '
              'Only if it needs {user} (deleting data you did not create, spend over ${limit}, a message sent as '
              'them, a first public launch, an honesty or safety question) ask once on a line starting "Needs you:".'),
    'tasks': ('%d task(s) are still open: %s. Finish them now, or, if something outside your control stops you, '
              'say so on a line starting "Blocked:" with what would unblock it.'),
}


def reason(key, detail):
    """The block reason for a decide() hit, addressed to the user named in config, with their spend limit."""
    def esc(value):
        return str(value).replace('%', '%%')
    template = REASONS[key].replace('{user}', esc(common.user())).replace('{limit}', esc(common.config().get('spend_limit_usd', 5)))
    return template % (detail if isinstance(detail, tuple) else (detail,))


def visible(text):
    return CODE.sub('', text or '')


def tail_paragraph(text):
    paras = [p for p in re.split(r'\n\s*\n', visible(text).strip()) if p.strip()]
    return paras[-1] if paras else ''


def sentence_at(par, pos):
    """(the sentence of par that holds pos, where pos falls inside it, the text after it)."""
    start = 0
    for m in re.finditer(r'(?<=[.!?])\s+|\n(?=\s*(?:[-*+\u2022]|\d+[.)]|\(?[a-d][.)])\s)', par):
        if m.end() <= pos:
            start = m.end()
        else:
            return par[start:m.start()], pos - start, par[m.end():]
    return par[start:], pos - start, ''


def is_clarifying(par, pos):
    """A question that offers alternatives to choose from is a clarifying question, not an offer of work: the
    sentence has " or " after the offer phrase, or the question introduces a list of options that ends the message.
    "Yes or no" is not a choice."""
    sentence, at, rest = sentence_at(par, pos)
    closing = sentence.rstrip()
    if re.search(r'\?[\s*_"\')\]]*$', closing) and OR_WORD.search(YES_NO.sub('', sentence[at:])):
        return True
    if re.search(r'[?:][\s*_"\')\]]*$', closing):
        items = [l for l in rest.splitlines() if l.strip()]
        return len(items) >= 2 and bool(LIST_ITEM.match(items[0])) and sum(1 for l in items if LIST_ITEM.match(l)) >= 2
    return False


def find_offer(par):
    """The first offer phrase in par that is not part of a clarifying question, or None."""
    for m in OFFER.finditer(par):
        if not is_clarifying(par, m.start()):
            return m.group(0)
    return None


def read_events(path):
    events = []
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events


def last_assistant_text(events):
    text = ''
    for event in events:
        msg = event.get('message') or {}
        if event.get('type') != 'assistant' or msg.get('role') != 'assistant':
            continue
        parts = [c.get('text', '') for c in msg.get('content') or [] if isinstance(c, dict) and c.get('type') == 'text']
        if parts:
            text = '\n'.join(parts)
    return text


def tool_calls(events):
    for event in events:
        if event.get('isSidechain'):
            continue
        for c in (event.get('message') or {}).get('content') or []:
            if isinstance(c, dict) and c.get('type') == 'tool_use':
                yield c.get('name') or '', c.get('input') or {}


def open_tasks(events):
    """Names of task-list items that are not completed, from the latest state of whichever list the agent kept."""
    todos, tasks, plan, created = None, {}, None, 0
    for name, inp in tool_calls(events):
        if name == 'TodoWrite':
            todos = [(t.get('content') or '', t.get('status') or '') for t in inp.get('todos') or []]
        elif name == 'TaskCreate':
            created += 1
            tasks[str(created)] = [inp.get('subject') or inp.get('description') or 'task %d' % created, 'pending']
        elif name == 'TaskUpdate':
            t = tasks.get(str(inp.get('taskId') or inp.get('id') or ''))
            if t and inp.get('status'):
                t[1] = inp['status']
        elif name.endswith('update_plan'):
            if inp.get('items'):
                plan = [[i.get('content') or '', i.get('status') or 'pending'] for i in inp['items']]
            elif plan:
                if inp.get('advance'):
                    cur = next((i for i in plan if i[1] == 'in_progress'), None)
                    if cur:
                        cur[1] = 'completed'
                    nxt = next((i for i in plan if i[1] == 'pending'), None)
                    if nxt:
                        nxt[1] = 'in_progress'
                for n in inp.get('done') or []:
                    if 0 < int(n) <= len(plan):
                        plan[int(n) - 1][1] = 'completed'
                if inp.get('start') and 0 < int(inp['start']) <= len(plan):
                    plan[int(inp['start']) - 1][1] = 'in_progress'
        elif name.endswith('wrap_up') and plan:
            plan = [[c, 'completed'] for c, _ in plan]
    left = []
    for items in (todos or [], list(tasks.values()), plan or []):
        left += [c for c, s in items if s not in ('completed', 'deleted', 'cancelled')]
    return left


def decide(text, events):
    """(reason key, detail) when the stop should be blocked, else None."""
    if BLOCKER.search(visible(text)):
        return None
    body = visible(text)
    steps = sum(1 for unit in SENTENCE.split(body) if is_step(unit))
    if steps >= 3:
        return 'steps', steps
    offer = find_offer(tail_paragraph(text))
    if offer:
        return 'offer', offer
    left = open_tasks(events)
    if left:
        return 'tasks', (len(left), '; '.join(x[:60] for x in left[:5]))
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    try:
        if data.get('stop_hook_active') or not common.config().get('stop_hook', True):
            return
        events = []
        if data.get('transcript_path'):
            try:
                events = read_events(os.path.expanduser(data['transcript_path']))
            except OSError:
                events = []
        text = data.get('last_assistant_message') or last_assistant_text(events)
        hit = decide(text, events)
        if hit:
            print(json.dumps({'decision': 'block', 'reason': reason(*hit)}))
    except Exception as exc:
        common.log_error('stop_steps', exc)


if __name__ == '__main__':
    main()
