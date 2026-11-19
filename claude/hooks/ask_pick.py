#!/usr/bin/env python3
"""agent-core PreToolUse hook for question boxes: a box that puts the agent's own pick to the user is not a question.

The rulebook says: with a pick in hand, take it, say what you assumed and keep going. Each box costs the user a tap on
a label the agent wrote, sometimes hours later. Two boxes do it:
- AskUserQuestion, where Claude Code tells the agent to put its recommendation first and label it "(Recommended)".
  Denied when a question marks a pick (stop_steps.PICK).
- The desktop app's next-step box (wrap_up with an ask), where the ask is usually the agent's own next step with a
  question mark ("Fix it and run round four?" / "Yes, fix it"). Denied when the ask proposes a step, asks leave for
  one ("Can I merge now?", "Want me to deploy?") or marks a pick. An ask for something only the user knows ("Is the
  token stored?", "When is the TV free?") passes.
Either box passes when the choice is the user's to make (stop_steps.owner_choice: deleting data the agent did not
create, spend over the limit, a message sent as them, a first public launch, creative direction, a step that takes a
running service down), when the question begins "Needs you:", which is how the agent says the answer is the user's
alone, and in plan mode, where the user is choosing on purpose.
Does nothing at all when config question_hook is false.
Its own errors never block; they go to learning/hook-errors.log.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import stop_steps  # noqa: E402

NEXT_STEP = 'mcp__ccd_turn__wrap_up'
# An ask whose sentence or clause opens with one of these wants an answer only the user has (what they see, when they
# are free, whether they did something). Anything else is a step the agent proposes.
ASKS_THE_USER = re.compile(r"(?:^|[.,;:]\s+)[*_\"'(]*(?:does|did|do|is|are|was|were|has|have|had|when|what|which|who|"
                           r"whom|whose|where|why|how|can|could|may|might|will|would|tell)\b", re.I)
# The agent's own step put as a request for leave: still its own step.
ASKS_LEAVE = re.compile(r"\b(?:can|could|may|might|shall|should) (?:i|we)\b|\b(?:is|does|would) (?:it|that|this) "
                        r"(?:be )?(?:ok|okay|fine|alright|all right|work)\b|\b(?:how|what) about\b|"
                        r"\bok(?:ay)? to\b|\bready to\b", re.I)
NAMED_NEED = stop_steps.NAMED_NEED
THEIRS = '(something only they can see, know or do, ' + stop_steps.OWNER_ITEMS + ')'
STEP_REASON = ('This next-step box asks {user} about a step you propose yourself ("%s"). That is not a question for '
               '{user}: take your pick, say in one line what you assumed, and keep going; show the box again without '
               'an ask when the work is done. Ask only what is theirs alone ' + THEIRS + ', and begin that ask with '
               '"Needs you:".')
REASON = ('This question box marks your own pick ("%s"). That is not a question for {user}: take the pick, say in '
          'one line what you assumed, and keep going. If the box held other questions with no pick of yours, ask only '
          'those. Only when the answer is theirs alone ' + THEIRS + ' ask it again: drop the pick marker and begin '
          'the question with "Needs you:".')


def question_text(q):
    """Everything the user would read for one question: the question, its header, each option's label and description."""
    parts = [q.get('question'), q.get('header')]
    for o in q.get('options') or []:
        if isinstance(o, dict):
            parts += [o.get('label'), o.get('description')]
    return '\n'.join(str(p) for p in parts if isinstance(p, str) and p.strip())


def marked_pick(tool_input):
    """The pick marker of the first question that has one and is the agent's to decide, or None."""
    questions = tool_input.get('questions') if isinstance(tool_input, dict) else None
    for q in questions if isinstance(questions, list) else []:
        if not isinstance(q, dict):
            continue
        text = question_text(q)
        if NAMED_NEED.match(str(q.get('question') or '')) or NAMED_NEED.match(str(q.get('header') or '')):
            continue
        m = stop_steps.PICK.search(text)
        if m and not stop_steps.owner_choice(text):
            label = next((str(o.get('label')) for o in q.get('options') or [] if isinstance(o, dict)
                          and stop_steps.PICK.search('%s\n%s' % (o.get('label') or '', o.get('description') or ''))), None)
            return label or m.group(0)
    return None


def proposed_step(tool_input):
    """The ask of a next-step box that puts the agent's own next step to the user, or None."""
    ask = tool_input.get('ask') if isinstance(tool_input, dict) else None
    if not isinstance(ask, str) or not ask.strip() or NAMED_NEED.match(ask):
        return None
    labels = [str(a.get('label')) for a in tool_input.get('actions') or [] if isinstance(a, dict) and a.get('label')]
    text = '\n'.join([ask] + labels)
    if stop_steps.owner_choice(text):
        return None
    own_step = stop_steps.PICK.search(text) or stop_steps.OFFER.search(ask) or ASKS_LEAVE.search(ask)
    if ASKS_THE_USER.search(ask.strip()) and not own_step:
        return None
    return ' '.join(ask.split())


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    try:
        tool = data.get('tool_name') if isinstance(data, dict) else None
        if tool not in ('AskUserQuestion', NEXT_STEP):
            return
        if not common.config().get('question_hook', True) or data.get('permission_mode') == 'plan':
            return
        pick = marked_pick(data.get('tool_input')) if tool == 'AskUserQuestion' else proposed_step(data.get('tool_input'))
        if pick:
            template = (REASON if tool == 'AskUserQuestion' else STEP_REASON).replace(
                '{user}', common.user().replace('%', '%%')).replace(
                '{limit}', str(common.config().get('spend_limit_usd', 5)).replace('%', '%%'))
            why = template % pick[:120]
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                                     'permissionDecisionReason': 'agent-core: ' + why}}))
    except Exception as exc:
        common.log_error('ask_pick', exc)


if __name__ == '__main__':
    main()
