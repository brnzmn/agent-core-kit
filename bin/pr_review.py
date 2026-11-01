#!/usr/bin/env python3
"""Review a PR's head with Codex or Claude and post the verdict as a PR comment, for the merge gate in guard.py.

  pr_review.py <pr> codex [--adversarial]   Codex's review through the vendored plugin (read-only sandbox)
  pr_review.py <pr> claude                  Claude Opus, read-only (bin/claude-review.sh)

<pr> is anything `gh pr view` accepts: a number, a URL or a branch. Run it from a checkout of the PR head, with gh
signed in. The comment starts "## Codex review", "## Codex adversarial review" or "## Claude review", names the head
commit, and ends with VERDICT: PASS or VERDICT: BLOCK; a new commit needs a new review. Codex blocks on any P0 or P1
finding (the adversarial review blocks on a needs-attention verdict). Claude blocks when its own last line says so.
Anything in the review text that looks like a secret (tokens, keys, private key blocks, a value after token/secret/
password/key =) is replaced by [REDACTED] before it is posted or printed, and the count is printed.
Exit 0 pass, 1 block, 3 no review (nothing is posted).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('AGENT_CORE_DIR', CORE)
sys.path.insert(0, os.path.join(CORE, 'claude', 'hooks'))
import common  # noqa: E402

PLUGIN = os.path.join(common.CORE, 'vendor', 'codex-plugin-cc', 'plugins', 'codex', 'scripts')
GH = os.environ.get('AGENT_CORE_GH') or 'gh'
MAX_TEXT = 60000  # GitHub rejects comments over 65536 characters; the verdict line must always survive
REDACTED = '[REDACTED]'
KEYWORD = r'(?:token|secret|password|passwd|key)'
# (pattern, replacement) pairs for text that looks like a secret. Specific token shapes first, then a catch-all for a
# long base64-ish value after token/secret/password/key and = or : (the name stays, the value goes). Bounded
# quantifiers keep a hostile blob from making the scan slow.
SECRET_PATTERNS = [
    (re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----'
                r'(?:.{0,10000}?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|(?:[ \t]*\n[A-Za-z0-9+/=]{20,}[ \t]*)*)', re.S),
     REDACTED),
    (re.compile(r'(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}'), REDACTED),
    (re.compile(r'(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}'), REDACTED),
    (re.compile(r'(?<![A-Za-z0-9])sk-(?:ant-)?[A-Za-z0-9_-]{20,}'), REDACTED),
    (re.compile(r'(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])'), REDACTED),
    (re.compile(r'(?<![A-Za-z0-9])xox[a-zA-Z]-[A-Za-z0-9-]{10,}'), REDACTED),
    (re.compile(r'\bbearer\s+[A-Za-z0-9._~+/=-]{20,}', re.I), 'Bearer ' + REDACTED),
    (re.compile(r'(?<=://)[^/\s:@]+:[^/\s@]+(?=@)'), REDACTED),
    (re.compile(r'(\b[\w.-]{0,40}' + KEYWORD + r'\b["\']?\s*[:=]\s*["\']?)(?=[A-Za-z0-9+/_=-]*\d)[A-Za-z0-9+/_=-]{16,}',
                re.I), r'\1' + REDACTED),
]


def sh(cmd, **kw):
    """subprocess.run that never raises for a missing program or a timeout: those come back as a failed result."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, '', 'command not found: %s' % cmd[0])
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, '', 'timed out: %s' % ' '.join(cmd[:3]))


def codex_review(base, adversarial):
    """(verdict, text). The companion leaves its broker running outside a Claude session, so the plugin's own
    SessionEnd teardown runs afterwards."""
    companion = os.path.join(PLUGIN, 'codex-companion.mjs')
    if not os.path.isfile(companion):
        return None, 'the Codex plugin is missing at %s (a kit checkout includes vendor/codex-plugin-cc)' % PLUGIN
    for tool, hint in (('node', 'the Codex plugin needs Node.js'), ('codex', 'install the Codex CLI and sign in')):
        if not shutil.which(tool):
            return None, '%s not found on PATH: %s' % (tool, hint)
    cmd = ['node', companion, 'adversarial-review' if adversarial else 'review', '--wait', '--json', '--base', base]
    try:
        out = sh(cmd, timeout=1800, stdin=subprocess.DEVNULL)
    finally:
        sh(['node', os.path.join(PLUGIN, 'session-lifecycle-hook.mjs'), 'SessionEnd'], timeout=60,
           input=json.dumps({'cwd': os.getcwd(), 'hook_event_name': 'SessionEnd'}))
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None, (out.stderr or out.stdout).strip()[-2000:]
    if not isinstance(data, dict):
        return None, json.dumps(data)[:2000]
    text = ((data.get('codex') or {}).get('stdout') or '').strip()
    found = data.get('result') if isinstance(data.get('result'), dict) else None
    if adversarial and found and found.get('verdict'):
        return ('PASS' if found['verdict'] == 'approve' else 'BLOCK'), text or json.dumps(found, indent=1)
    if not text:
        return None, json.dumps(data)[:2000]
    return ('BLOCK' if re.search(r'\[P[01]\]', text) else 'PASS'), text


def claude_review(base):
    """(verdict, text) from bin/claude-review.sh, whose exit code is 0 pass, 1 block, anything else no review."""
    out = sh(['bash', os.path.join(common.CORE, 'bin', 'claude-review.sh'), base], timeout=1800,
             stdin=subprocess.DEVNULL)
    text = re.sub(r'\n\(saved to [^\n]*\)\s*$', '', out.stdout.strip())
    text = re.sub(r'\nVERDICT: (PASS|BLOCK)\s*$', '', text)
    return {0: 'PASS', 1: 'BLOCK'}.get(out.returncode), text or out.stderr.strip()[-2000:]


def redact(text):
    """(text with everything that looks like a secret replaced by [REDACTED], how many were replaced). A review is
    written by a model that has read the whole diff, so it can quote a token the diff contains."""
    total = 0
    for pattern, replacement in SECRET_PATTERNS:
        text, n = pattern.subn(replacement, text)
        total += n
    return text, total


def verdict_comment(title, head, text, verdict):
    """The PR comment body the merge gate reads: title line, the head commit, the review, the verdict as the last line."""
    text = text.strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rstrip() + '\n\n[review truncated to fit the GitHub comment limit]'
    return '## %s\nhead: %s\n\n%s\n\nVERDICT: %s\n' % (title, head, text, verdict)


def parse_args(argv):
    """(pr, who, adversarial), or None when the arguments are not valid."""
    flags = [a for a in argv if a.startswith('-')]
    rest = [a for a in argv if not a.startswith('-')]
    if len(rest) != 2 or rest[1] not in ('codex', 'claude') or set(flags) - {'--adversarial'}:
        return None
    if '--adversarial' in flags and rest[1] != 'codex':
        return None
    return rest[0], rest[1], '--adversarial' in flags


def main(argv):
    args = parse_args(argv)
    if not args:
        print(__doc__)
        return 3
    pr, who, adversarial = args
    meta = sh([GH, 'pr', 'view', pr, '--json', 'number,baseRefName,headRefOid,url'], timeout=60)
    if meta.returncode:
        print('pr_review: gh pr view %s failed: %s' % (pr, meta.stderr.strip()))
        return 3
    try:
        meta = json.loads(meta.stdout)
        number, base_name, pr_head = meta['number'], meta['baseRefName'], meta['headRefOid']
    except (ValueError, KeyError, TypeError):
        print('pr_review: gh pr view %s returned something unexpected: %s' % (pr, str(meta.stdout)[:200]))
        return 3
    head = sh(['git', 'rev-parse', 'HEAD']).stdout.strip()
    if not head:
        print('pr_review: run this from a git checkout of the PR head.')
        return 3
    if head != pr_head:
        print('pr_review: this checkout is at %s, the PR head is %s. Check out the PR head first.'
              % (head[:12], pr_head[:12]))
        return 3
    sh(['git', 'fetch', '-q', 'origin', base_name], timeout=120)
    base = 'origin/' + base_name
    verdict, text = codex_review(base, adversarial) if who == 'codex' else claude_review(base)
    text, redactions = redact(text)  # before the length cut, so a secret can't survive as a half-cut prefix
    if verdict is None:
        print('pr_review: the %s review did not finish; nothing posted.\n%s' % (who, text))
        return 3
    print('pr_review: redacted %d secret-like string(s) from the review before posting' % redactions)
    title = 'Codex adversarial review' if who == 'codex' and adversarial else '%s review' % who.capitalize()
    body = verdict_comment(title, pr_head, text, verdict)
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as fh:
        fh.write(body)
    try:
        posted = sh([GH, 'pr', 'comment', str(number), '--body-file', fh.name], timeout=60)
    finally:
        os.unlink(fh.name)
    print(body)
    if posted.returncode:
        print('pr_review: posting the comment failed: %s' % posted.stderr.strip())
        return 3
    return 0 if verdict == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
