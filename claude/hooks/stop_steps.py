#!/usr/bin/env python3
"""agent-core Stop hook: push back once when a turn ends by handing work to the user.

Blocks the stop when the last assistant message
- has 3 or more instructions to the user (run, open, click, log in, paste, "you need to", ...),
  numbered, bulleted or written as prose;
- ends on an offer instead of the work ("want me to", "should I", "say the word", "reply go"); a question that
  offers alternatives ("Should I use Postgres or SQLite?", or a question followed by a list of options) is a
  clarifying question and passes;
- ends on a question that marks the agent's own pick (an option labelled "(Recommended)" or "my pick", or "I'd go
  with X. Is that fine?"): the agent takes the pick instead, unless the choice is the user's to make (owner_choice);
- or the task list (TodoWrite, TaskCreate/TaskUpdate, the desktop app's plan) still has open
  items and the message names no blocker. This one stays quiet while Claude Code reports work in flight for the
  session (a background command, agent, monitor or workflow, or a scheduled wake-up): the turn is pausing, not
  ending, and a push would only draw a status line.
A line starting with "Needs you:" or "Blocked:" names what is left and lets the stop through.
Lets the stop through when stop_hook_active is set, so it nudges at most once per turn, and does nothing at all
when config stop_hook is false.
Its own errors never block; they go to learning/hook-errors.log.
"""
import datetime
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

# What the user alone decides: a question about one of these is a real question even when the agent has a pick.
OWNER_ITEMS = ('deleting data you did not create, spend over ${limit}, a message sent as them, a first public launch, '
               'creative direction, a step that takes a running service down, an honesty or safety question')
REASONS = {
    'steps': ('This reply gives {user} %d instructions. Do the steps you can yourself. Then send only what is left '
              'for {user}: one "Needs you:" block with the action, why it matters, and exactly how. Do not send the '
              'report again.'),
    'offer': ('This turn ends on an offer ("%s"). Do the work instead of offering it; {user}\'s standing '
              'authorization in the global instructions covers what it lists, and spend up to ${limit} per action. '
              'Only if it needs {user} (deleting data you did not create, spend over ${limit}, a message sent as '
              'them, a first public launch, an honesty or safety question) ask once in a "Needs you:" block.'),
    'pick': ('This turn ends on a question that marks your own pick ("%s"). That is not a question for {user}: take '
             'the pick, say in one line what you assumed, and keep going. Only when the answer is theirs alone '
             '(something only they can see, know or do, ' + OWNER_ITEMS + ') ask it, in a "Needs you:" block.'),
    'tasks': ('%d task(s) are still open: %s. Finish them now. If one waits on something that is still running, '
              'write no status line: set one watcher for it, where your tool has one, and end the turn. If {user} '
              'has to act, say it once in a "Needs you:" block.'),
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


# The agent's own pick. PICK_LABEL marks an option; OWN states it in the first person. A bare "recommended" counts only
# in a question box (ask_pick), since reports say "the recommended version" about other people's advice.
PICK_LABEL = re.compile(r"\(\s*recommended\s*\)|\[\s*recommended\s*\]|\brecommended\s*:|"
                        r"\bmy (?:pick|recommendation|suggestion|preference|vote)\b", re.I)
OWN = re.compile(r"\bi(?:'d| would)? (?:recommend|suggest)\b|\bi(?:'d| would) (?:pick|go with|choose|prefer)\b|"
                 r"\bmy (?:pick|recommendation|suggestion|preference|vote)\b", re.I)
PICK = re.compile(PICK_LABEL.pattern + '|' + OWN.pattern + r"|\brecommended\b", re.I)
# A closing question that only asks for a yes to what was just proposed ("Is that fine?", "Shall we do that?"): it
# opens like a yes/no question and points back at the proposal.
CONFIRMS = re.compile(r"^\W*(?:is|does|do|shall|should|can|could|would|ok|okay|sounds?|proceed|go|agree|right|fine|"
                      r"alright)\b", re.I)
REFERS = re.compile(r"\b(?:that|this|it|so|those|these)\b", re.I)
# A question that asks to choose among what was just listed, and an intro line that announces choices.
CHOOSES = re.compile(r"^\W*which\b|\bwhich (?:one|option|of)\b|\sor\s|\b(?:pick|choose|prefer|option)\w*\b", re.I)
OFFERS_CHOICES = re.compile(r"\?[\s*_\"')\]]*$|\b(?:options?|choices?|ways?|routes?|paths?|alternatives?|approaches|"
                            r"directions?|variants?|candidates?)\b[^.!?\n]*:[\s*_]*$", re.I)
QUESTION_END = re.compile(r'\?[\s*_"\')\]]*$')
OPTIONS_LEAD = re.compile(r'[?:][\s*_"\')\]]*$')
OWNER_WORDS = re.compile(
    r"\b(?:delet\w*|truncat\w*|eras\w*|purg\w*|destroy\w*|wip(?:e|es|ed|ing))\b"
    r"|\bdrop(?:s|ped|ping)? (?:the |a |that |this |all )?(?:[\w-]+ ){0,2}(?:tables?|databases?|db|schemas?|columns?|"
    r"index(?:es)?|collections?|buckets?|volumes?|rows|records|data)\b"
    r"|\b(?:send|sends|sending|sent|post\w*|publish\w*|reply|replies|replying|email\w*|messag\w*|tweet\w*|sign\w*)\b"
    r"[^.?!\n]{0,60}\b(?:as you|as yourself|in your name|on your behalf|from your (?:account|number|address|email|"
    r"inbox|profile))\b"
    r"|\bsend\w* (?:the|this|that|an?|your) (?:[\w-]+ ){0,2}(?:email|e-mail|message|dm|reply|post|tweet|invoice|"
    r"newsletter)\b"
    r"|\b(?:public launch|go(?:es|ing)? public|launch\w* (?:it |this )?publicly|make (?:it|this|the repo) public|"
    r"announc\w*)\b"
    r"|\b(?:creative|logo|colou?rs?|palette|fonts?|typeface|look and feel|branding|brand identity|art direction|"
    r"design direction|tone of voice|headline|tagline|mock-?ups?|naming|name for|"
    r"which (?:look|style|design|tone|name)|(?:page|site|ad|marketing|email) copy|copywriting)\b"
    r"|\b(?:subscription|paid plan|billing plan)\b"
    r"|\b(?:downtime|outage|maintenance window|restart\w*|reboot\w*|shut\w* ?down|(?:take|takes|taking|go|goes|going) "
    r"(?:[\w-]+ ){0,4}(?:down|offline)|stop\w* (?:the |a |that |this )?(?:[\w-]+ ){0,3}(?:bot|service|server|app|site|"
    r"database|workspace|container|instance|machine|worker|tunnel))\b", re.I)
MONEY = re.compile(r"[$€£]\s?(\d[\d,]*(?:\.\d+)?)|\b(\d[\d,]*(?:\.\d+)?)\s?(?:usd|eur|dollars?|euros?)\b", re.I)
NAMED_NEED = re.compile(r'[\s*_>-]*needs you\**\s*:', re.I)


def closing(text):
    """(question, options, before) for a message that ends on a question, else None.

    question: the asking line. options: the option lines that go with it, listed after it, or in the block right
    above when the question is the last line by itself. before: what precedes the question in its own paragraph."""
    lines = visible(text).rstrip().splitlines()
    if not lines:
        return None
    start = len(lines)
    while start > 0 and (LIST_ITEM.match(lines[start - 1]) or not lines[start - 1].strip()):
        start -= 1
    options = [l for l in lines[start:] if l.strip()]
    if len(options) >= 2 and start > 0 and OPTIONS_LEAD.search(lines[start - 1].rstrip()):
        lead = start - 1
    elif QUESTION_END.search(lines[-1].rstrip()) and not LIST_ITEM.match(lines[-1]):
        lead, options = len(lines) - 1, []
        above = lead - 1
        while above >= 0 and not lines[above].strip():
            above -= 1
        if above < lead - 1:  # a blank line above the question: options may be the list that ends the block before it
            first = above
            while first >= 0 and LIST_ITEM.match(lines[first]):
                first -= 1
            intro = first  # what introduces that list
            while intro > 0 and lines[intro - 1].strip():
                intro -= 1
            head = '\n'.join(lines[max(intro, 0):first + 1]) if first >= 0 else ''
            # a report's bullet list is not a set of options: the question has to choose, or the list has to offer
            if above - first >= 2 and (CHOOSES.search(lines[lead]) or OFFERS_CHOICES.search(head.rstrip())):
                return lines[lead], lines[first + 1:above + 1], head
    else:
        return None
    top = lead
    while top > 0 and lines[top - 1].strip():
        top -= 1
    return lines[lead], options, '\n'.join(lines[top:lead])


def owner_choice(text):
    """True when text names something only the user decides, so a question about it stands even with a pick marked.
    A wrong True only lets a question through as before; a wrong False costs one nudge that names the way out."""
    if OWNER_WORDS.search(text):
        return True
    limit = common.config().get('spend_limit_usd', 5)
    for m in MONEY.finditer(text):
        try:
            if float((m.group(1) or m.group(2)).replace(',', '')) > float(limit):
                return True
        except ValueError:
            pass
    return False


def find_pick(text):
    """The pick marker of a message that ends on a question the agent may decide itself, or None.

    With options: a marker on an option or in the question, or a first-person pick just before it. Without: the
    pick stated in the question, or in the sentence before a question that only asks for a yes to it. A
    recommendation somewhere in the report above an unrelated question for the user is not a pick."""
    end = closing(text)
    if not end:
        return None
    question, options, before = end
    if owner_choice('\n'.join([before, question] + options)):
        return None
    if options:
        block = '\n'.join([question] + options)
        m = PICK_LABEL.search(block) or OWN.search(block) or OWN.search(before)
        return m.group(0) if m else None
    sentences = [x for x in re.split(r'(?<=[.!?])\s+', (before + '\n' + question).strip()) if x.strip()]
    ask, prev = sentences[-1], (sentences[-2] if len(sentences) > 1 else '')
    m = PICK_LABEL.search(ask) or OWN.search(ask)
    if not m and CONFIRMS.match(ask) and REFERS.search(ask):
        m = OWN.search(prev) or PICK_LABEL.search(prev)
    return m.group(0) if m else None


ENDED = ('completed', 'failed', 'killed', 'stopped', 'cancelled', 'canceled', 'error', 'errored', 'timeout',
         'timed_out', 'done', 'exited')
LIVE = ('running', 'pending', 'starting', 'queued', 'in_progress', 'active', 'waiting', 'scheduled')
RUNNING_MAX_AGE = 6 * 3600  # a launch older than this with no end on record died with its session


def notices(text):
    """The bodies of the task notifications in text. Plain finds, so a long string with unclosed tags stays linear."""
    out, at = [], 0
    while True:
        start = text.find('<task-notification>', at)
        if start < 0:
            return out
        end = text.find('</task-notification>', start)
        if end < 0:
            return out
        out.append(text[start + 19:end])
        at = end + 20


def listed_in_flight(data):
    """What the Stop payload says about work in flight: True, False, or None when this Claude Code does not say.
    background_tasks holds commands, agents, monitors and workflows still running; session_crons holds wake-ups."""
    tasks, crons = data.get('background_tasks'), data.get('session_crons')
    if not isinstance(tasks, list) and not isinstance(crons, list):
        return None
    live = [t for t in tasks if isinstance(t, dict) and str(t.get('status') or '').lower() in LIVE] \
        if isinstance(tasks, list) else []
    return bool(live) or (isinstance(crons, list) and bool(crons))


def transcript_in_flight(path, now=None):
    """The same answer read from the transcript, for a Claude Code whose Stop payload has no background_tasks.
    A launch is a tool result that carries a task id; its end is the task notification that names the launching
    tool call (or the task) with a status, or a stop call for it."""
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    launched, ended = {}, set()
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if not ('TaskId' in line or 'taskId' in line or 'agentId' in line or 'task-notification' in line
                    or 'TaskStop' in line or 'KillShell' in line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get('isSidechain'):
                continue
            msg = e.get('message') if isinstance(e.get('message'), dict) else {}
            content = msg.get('content')
            blocks = [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []
            texts = [x for x in (e.get('content'), content, (e.get('attachment') or {}).get('prompt')
                                 if isinstance(e.get('attachment'), dict) else None) if isinstance(x, str)]
            for text in texts:
                for note in notices(text):
                    if re.search(r'<status>\s*\w', note):
                        ended.update(re.findall(r'<(?:tool-use-id|task-id)>\s*([^<\s]+)\s*</', note))
            for c in blocks:
                if c.get('type') == 'tool_use' and c.get('name') in ('TaskStop', 'KillShell', 'KillBash'):
                    inp = c.get('input') if isinstance(c.get('input'), dict) else {}
                    ended.update(str(v) for k, v in inp.items() if k in ('task_id', 'shell_id', 'bash_id', 'id') and v)
            result = e.get('toolUseResult')
            if isinstance(result, dict):
                task = (result.get('backgroundTaskId')
                        or (result.get('status') == 'async_launched' and (result.get('taskId') or result.get('agentId')))
                        or ('timeoutMs' in result and result.get('taskId')))
                call = next((c.get('tool_use_id') for c in blocks if c.get('type') == 'tool_result'), None)
                if task and call:
                    launched[call] = (str(task), stamp(e.get('timestamp')))
    return any(call not in ended and task not in ended and (at is None or now - at < RUNNING_MAX_AGE)
               for call, (task, at) in launched.items())


def stamp(value):
    """Unix time of a transcript timestamp ("2026-11-19T03:43:01.452Z"), or None."""
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def paused(data):
    """True when the session has work in flight or a wake-up scheduled: this stop is a pause, not an end."""
    listed = listed_in_flight(data)
    if listed is not None:
        return listed
    path = data.get('transcript_path')
    try:
        return bool(path) and transcript_in_flight(os.path.expanduser(path))
    except OSError:
        return False


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
    pick = find_pick(text)
    if pick:
        return 'pick', pick
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
        if hit and hit[0] == 'tasks' and paused(data):
            hit = None
        if hit:
            print(json.dumps({'decision': 'block', 'reason': reason(*hit)}))
    except Exception as exc:
        common.log_error('stop_steps', exc)


if __name__ == '__main__':
    main()
