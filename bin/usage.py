#!/usr/bin/env python3
"""Remaining plan usage for Claude and Codex, and the routing call for new implementation lanes.

  usage.py           text
  usage.py --json    one JSON object

Sources, all non-interactive, 10 seconds in total, no keychain or login prompts:
  Claude  `claude -p /usage` (answered by the CLI itself: no model call, no cost)
  Codex   the rate_limits Codex writes into its newest session rollouts (as of its last turn)
  either  CodexBar's widget snapshot (macOS app, optional), when it is less than a day old
A number no source gives is reported as unknown with the reason, never guessed. Routing: past
config routing.claude_week_limit_pct (default 80) of Claude's weekly limit, new implementation lanes go to Codex.
"""
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('AGENT_CORE_DIR', CORE)
sys.path.insert(0, os.path.join(CORE, 'claude', 'hooks'))
import common  # noqa: E402

BUDGET = 9.0  # seconds for the slowest source; the whole run stays under 10
CODEX_HOME = os.environ.get('CODEX_HOME') or os.path.join(common.HOME, '.codex')
# Written by the optional CodexBar menu-bar app; absent on machines without it
CODEXBAR = os.path.join(common.HOME, 'Library', 'Group Containers', 'Y5PE65HELJ.com.steipete.codexbar',
                        'widget-snapshot.json')


def limit():
    """The routing threshold in percent, from config routing.claude_week_limit_pct (80 when unset or not a number)."""
    try:
        value = float((common.config().get('routing') or {}).get('claude_week_limit_pct', 80))
    except (TypeError, ValueError):
        return 80.0
    return value if 0 < value <= 100 else 80.0


def pct(value):
    """87 for 87.0, 82.5 for 82.5."""
    return '%d' % value if float(value).is_integer() else '%.1f' % value


def claude_usage():
    src = 'claude -p /usage'
    try:
        out = subprocess.run(['claude', '-p', '/usage', '--output-format', 'json', '--strict-mcp-config'],
                             capture_output=True, text=True, timeout=BUDGET, stdin=subprocess.DEVNULL,
                             cwd=common.HOME, env=dict(os.environ, CLAUDECODE=''))
    except FileNotFoundError:
        return {'source': src, 'error': 'the claude CLI is not on PATH'}
    except subprocess.TimeoutExpired:
        return {'source': src, 'error': 'timed out after %d s' % BUDGET}
    except OSError as exc:
        return {'source': src, 'error': 'could not run claude (%s)' % exc.__class__.__name__}
    try:
        text = json.loads(out.stdout).get('result') or ''
    except (ValueError, AttributeError):
        return {'source': src, 'error': 'claude did not return usage text (signed out, or a CLI without /usage?)'}
    got = {'source': src}
    for key, label in (('week', r'Current week \(all models\)'), ('session', r'Current session')):
        m = re.search(label + r':\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets ([^\n]+))?', text)
        if m:
            got[key] = float(m.group(1))
            got[key + '_resets'] = (m.group(2) or '').strip() or None
    if 'week' not in got:
        got['error'] = 'no weekly figure in the /usage text (plans without a weekly limit show none)'
    return got


def codex_usage():
    """Newest rate_limits (by event time) in the 40 rollouts touched most recently in the last week; each file is
    read from its last 2 MB backwards."""
    src = 'Codex rollouts'
    sessions = os.path.join(CODEX_HOME, 'sessions')
    if not os.path.isdir(sessions):
        return {'source': src, 'error': 'no Codex sessions folder at %s (Codex has not run here)' % common.tilde(sessions)}
    files = glob.glob(os.path.join(sessions, '*', '*', '*', 'rollout-*.jsonl'))
    cutoff = time.time() - 7 * 86400
    files = sorted((f for f in files if os.path.getmtime(f) >= cutoff), key=os.path.getmtime, reverse=True)[:40]
    best = None
    for path in files:
        try:
            with open(path, 'rb') as fh:
                fh.seek(max(0, os.path.getsize(path) - 2_000_000))
                lines = fh.read().splitlines()
        except OSError:
            continue
        for raw in reversed(lines):
            if b'rate_limits' not in raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            p = ev.get('payload') or {}
            rl = p.get('rate_limits') or (p.get('info') or {}).get('rate_limits')
            if rl and rl.get('primary'):
                if not best or ev.get('timestamp', '') > best[0]:
                    best = (ev.get('timestamp', ''), rl)
                break
    if not best:
        return {'source': src, 'error': 'no rate_limits in the last week of Codex sessions'}
    ts, rl = best
    got = {'source': src, 'as_of': ts, 'plan': rl.get('plan_type')}
    for key in ('primary', 'secondary'):
        w = rl.get(key) or {}
        if w.get('used_percent') is None:
            continue
        name = {10080: 'week', 300: 'session'}.get(w.get('window_minutes'), '%smin' % w.get('window_minutes'))
        got[name] = float(w['used_percent'])
        if w.get('resets_at'):
            got[name + '_resets'] = time.strftime('%b %d %H:%M', time.localtime(w['resets_at']))
    if rl.get('rate_limit_reached_type'):
        got['limit_reached'] = rl['rate_limit_reached_type']
    return got


def codexbar(tool):
    """A fresh CodexBar widget entry for tool, or None. The app writes it only while it runs."""
    try:
        if time.time() - os.path.getmtime(CODEXBAR) > 86400:
            return None
        with open(CODEXBAR) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    for e in data.get('entries') or []:
        if str(e.get('provider') or '').lower() == tool:
            used = e.get('weeklyUsedPercent') or (e.get('secondary') or {}).get('usedPercent')
            if used is not None:
                return {'source': 'CodexBar', 'week': float(used)}
    return None


def route(claude, codex):
    cw, xw, cap = claude.get('week'), codex.get('week'), limit()
    if cw is None:
        return 'Claude weekly usage unknown: route as usual and say so.'
    if cw < cap:
        return 'Claude week at %s%%: below %s%%, new implementation lanes stay on Claude.' % (pct(cw), pct(cap))
    if xw is not None and xw >= cap:
        return ('Claude week at %s%% and Codex at %s%%: both past %s%%. Finish running lanes, open no new ones, '
                'tell %s.' % (pct(cw), pct(xw), pct(cap), common.user()))
    return 'Claude week at %s%%: past %s%%, new implementation lanes go to Codex%s.' % (
        pct(cw), pct(cap), '' if xw is not None else ' (its own usage is unknown)')


def main(argv):
    found = {}
    jobs = [threading.Thread(target=lambda: found.__setitem__('claude', claude_usage())),
            threading.Thread(target=lambda: found.__setitem__('codex', codex_usage()))]
    for j in jobs:
        j.start()
    for j in jobs:
        j.join(BUDGET + 0.5)
    claude = found.get('claude') or {'error': 'timed out'}
    codex = found.get('codex') or {'error': 'timed out'}
    for tool, got in (('claude', claude), ('codex', codex)):
        if got.get('week') is None:
            got.update(codexbar(tool) or {})
            if got.get('week') is not None:
                got.pop('error', None)
    result = {'claude': claude, 'codex': codex, 'limit_pct': limit(), 'route': route(claude, codex)}
    if '--json' in argv:
        print(json.dumps(result))
        return 0
    c, x = claude, codex
    if c.get('week') is None:
        line = 'unknown (%s)' % c.get('error')
    else:
        line = 'week %s%% used' % pct(c['week'])
        if c.get('week_resets'):
            line += ', resets %s' % c['week_resets']
        if c.get('session') is not None:
            line += '; session %s%%' % pct(c['session'])
    print('Claude: ' + line)
    if x.get('week') is None:
        line = 'unknown (%s)' % x.get('error')
    else:
        line = 'week %s%% used' % pct(x['week'])
        if x.get('week_resets'):
            line += ', resets %s' % x['week_resets']
        if x.get('plan'):
            line += '; plan %s' % x['plan']
        if x.get('as_of'):
            line += '; as of %s' % x['as_of'][:16]
    print('Codex:  ' + line)
    print('Route:  ' + result['route'])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
