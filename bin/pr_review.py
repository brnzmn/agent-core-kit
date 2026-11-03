#!/usr/bin/env python3
"""Review a PR's head with Codex or Claude and post the verdict as a PR comment, for the merge gate in guard.py.

  pr_review.py <pr> codex [--adversarial]   Codex's review through the vendored plugin (read-only sandbox)
  pr_review.py <pr> claude                  Claude Opus, read-only (bin/claude-review.sh)
  pr_review.py <pr> auto                    both; one unavailable needs the other PASS and passing full tests

<pr> is anything `gh pr view` accepts: a number, a URL or a branch. Run it from a checkout of the PR head, with gh
signed in. The comment starts "## Codex review", "## Codex adversarial review" or "## Claude review", names the head
commit, and ends with VERDICT: PASS or VERDICT: BLOCK; a new commit needs a new review. Codex blocks on any P0 or P1
finding (the adversarial review blocks on a needs-attention verdict). Claude blocks when its own last line says so.
Anything in the review text that looks like a secret (tokens, keys, private key blocks, a value after token/secret/
password/key =) is replaced by [REDACTED] before it is posted or printed, and the count is printed.
Exit 0 pass, 1 block, 3 incomplete. Auto records the configured full test result; only passing tests can satisfy fallback.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import NamedTuple, Optional

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('AGENT_CORE_DIR', CORE)
sys.path.insert(0, os.path.join(CORE, 'claude', 'hooks'))
import common  # noqa: E402
sys.path.insert(0, os.path.join(CORE, 'bin'))
import review_worker  # noqa: E402

PLUGIN = os.path.join(common.CORE, 'vendor', 'codex-plugin-cc', 'plugins', 'codex', 'scripts')
GH = os.environ.get('AGENT_CORE_GH') or 'gh'
MAX_TEXT = 60000  # GitHub rejects comments over 65536 characters; the verdict line must always survive
REDACTED = '[REDACTED]'
# Match the complete literal after a credential name, including dots and quoted whitespace.
NAMED_SECRET = re.compile(
    r"""(?P<prefix>\b(?P<name>(?:[\w.-]{1,64}[_-]|[a-zA-Z]{1,32}(?=[A-Z])|(?i:api|access|refresh|client|private|db))?(?i:token|secret|password|passwd|key))\b["']?[ \t]*[:=][ \t]*)"""
    r"""(?:"(?P<double>(?:\\.|[^"\\])*)(?:"|$)|'(?P<single>(?:\\.|[^'\\])*)(?:'|$)|(?P<bare>(?:\\[\s\S]|[^\s`\\]|\\$)+))""")

EVIDENCE_TITLE = '## Agent-core review availability'
EVIDENCE_MARK = 'AGENT_CORE_REVIEW_EVIDENCE_V1: '
RUN_MARK = 'AGENT_CORE_REVIEW_RUN_V1: '
RUN_TITLE = '## Agent-core review run'
# Standalone credential shapes which need no assignment name.
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
]


def sh(cmd, **kw):
    """subprocess.run that never raises for a missing program or a timeout: those come back as a failed result."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, '', 'command not found: %s' % cmd[0])
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, '', 'timed out: %s' % ' '.join(cmd[:3]))


class Review(NamedTuple):
    verdict: Optional[str]
    text: str
    unavailable: Optional[str] = None


def unavailable_reason(text):
    """Only call for failed provider execution. Local errors and timeouts do not establish an outage."""
    patterns = {
        'quota': r"you['’]ve hit your (?:weekly|usage|session) limit|usage limit (?:reached|exceeded)|"
                 r"rate limit (?:reached|exceeded)|credit balance is too low|insufficient (?:credits|quota)",
        'auth': r'not (?:logged|signed) in|please (?:log|sign) in|authentication failed|unauthorized.*(?:401|sign in)',
        'service': r'(?:service|server) (?:is )?unavailable|(?:selected|requested) model is at capacity\b|'
                   r'overloaded_error|HTTP (?:502|503|504)\b',
        'missing_cli': r'(?:claude|codex)(?: CLI)? (?:not found|is not installed)|(?:claude|codex): command not found',
    }
    return next((reason for reason, pattern in patterns.items() if re.search(pattern, text, re.I)), None)


def has_blocking_output(text):
    """An interrupted review may have already found a blocker; an outage cannot erase it."""
    return bool(re.search(r'\[P[01]\]|^VERDICT:\s*BLOCK\s*$', text, re.M))


def codex_error_reason(error):
    """Known app-server error codes survive even when native review stdout/stderr are empty."""
    if not isinstance(error, dict):
        return None
    codes = [error.get('code'), error.get('type'), error.get('codexErrorInfo')]
    known = {'quota': {'usage_limit_reached', 'usageLimitExceeded', 'rate_limit_exceeded', 'insufficient_quota'},
             'auth': {'unauthorized', 'authentication_error', 'invalid_api_key'},
             'service': {'service_unavailable', 'server_overloaded', 'overloaded_error'}}
    return next((reason for reason, values in known.items()
                 if any(isinstance(code, str) and code in values for code in codes)), None)


def codex_review(base, adversarial):
    """PASS needs successful completion; partial blocking findings still produce a durable BLOCK."""
    companion = os.path.join(PLUGIN, 'codex-companion.mjs')
    if not os.path.isfile(companion):
        return Review(None, 'the Codex plugin is missing at %s' % PLUGIN)
    for tool in ('node', 'codex'):
        if not shutil.which(tool):
            return Review(None, '%s CLI not found on PATH' % tool, 'missing_cli' if tool == 'codex' else None)
    cmd = ['node', companion, 'adversarial-review' if adversarial else 'review', '--wait', '--json', '--base', base]
    try:
        out = sh(cmd, timeout=1800, stdin=subprocess.DEVNULL)
    finally:
        sh(['node', os.path.join(PLUGIN, 'session-lifecycle-hook.mjs'), 'SessionEnd'], timeout=60,
           input=json.dumps({'cwd': os.getcwd(), 'hook_event_name': 'SessionEnd'}))
    try:
        data = json.loads(out.stdout)
    except ValueError:
        text = (out.stdout + '\n' + out.stderr).strip()
        if has_blocking_output(text):
            return Review('BLOCK', 'Interrupted Codex review contains blocking findings:\n' + text)
        return Review(None, text[-2000:], unavailable_reason(out.stderr) if out.returncode not in (0, 124) else None)
    if not isinstance(data, dict) or not isinstance(data.get('codex'), dict):
        return Review(None, json.dumps(data)[:2000])
    provider = data['codex']
    raw_text = provider.get('stdout')
    text = raw_text.strip() if isinstance(raw_text, str) else ''
    status = provider.get('status')
    if out.returncode != 0 or type(status) is not int or status != 0:
        structured = provider.get('error')
        error = str(provider.get('stderr') or out.stderr or '').strip()
        if isinstance(structured, dict):
            error = (error + '\n' + json.dumps(structured, ensure_ascii=False)).strip()
        reason = None
        if has_blocking_output(text + '\n' + error):
            return Review('BLOCK', 'Interrupted Codex review contains blocking findings:\n' + text + '\n' + error)
        if type(status) is int and status != 0 and out.returncode != 124:
            reason = codex_error_reason(structured) or unavailable_reason(provider.get('stderr') or out.stderr or '')
        return Review(None, (error or text)[-2000:], reason)
    found = data.get('result')
    if adversarial:
        if not isinstance(found, dict) or found.get('verdict') not in ('approve', 'needs-attention'):
            return Review(None, 'Codex returned no valid structured review verdict')
        return Review('PASS' if found['verdict'] == 'approve' else 'BLOCK', text or json.dumps(found, indent=1))
    if not text:
        return Review(None, 'Codex completed without review text')
    return Review('BLOCK' if has_blocking_output(text) else 'PASS', text)


def claude_review(base):
    out = sh(['bash', os.path.join(common.CORE, 'bin', 'claude-review.sh'), base], timeout=1800,
             stdin=subprocess.DEVNULL)
    text = re.sub(r'\n\(saved to [^\n]*\)\s*$', '', out.stdout.strip())
    verdict = {0: 'PASS', 1: 'BLOCK'}.get(out.returncode)
    if verdict is None and has_blocking_output(text + '\n' + out.stderr):
        return Review('BLOCK', 'Interrupted Claude review contains blocking findings:\n' + text + '\n' + out.stderr)
    # A successful review mentioning a quota or outage is still an actual review.
    reason = unavailable_reason(text or out.stderr) if out.returncode == 4 else None
    if verdict is not None:
        text = re.sub(r'\nVERDICT: (PASS|BLOCK)\s*$', '', text)
    return Review(verdict, text or out.stderr.strip()[-2000:], reason)


def redact(text):
    """(text with everything that looks like a secret replaced by [REDACTED], how many were replaced). A review is
    written by a model that has read the whole diff, so it can quote a token the diff contains."""
    total = 0
    def named(match):
        nonlocal total
        group = match.lastgroup
        value = match.group(group)
        name = match['name'].lower()
        if value == REDACTED:
            return match[0]
        if group == 'bare':
            if name in ('key', 'token') and re.fullmatch(r"[A-Za-z_][\w.]*\.get\(['\"][^'\"]+['\"]\)", value):
                return match[0]
            if name == 'key' and re.fullmatch(r"(?:[\w.-]+/)+[\w.-]+\.(?:py|tsx?|jsx?)", value.rstrip('.')):
                return match[0]
        total += 1
        start, end = match.span(group)
        return match[0][:start - match.start()] + REDACTED + match[0][end - match.start():]
    text = NAMED_SECRET.sub(named, text)
    for pattern, replacement in SECRET_PATTERNS:
        text, n = pattern.subn(replacement, text)
        total += n
    return text, total


def verdict_comment(title, head, text, verdict, run_id=None):
    """The PR comment body the merge gate reads: title line, the head commit, the review, the verdict as the last line."""
    text, _ = redact(text.strip())  # redact complete tokens before a length cut can hide their shape
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rstrip() + '\n\n[review truncated to fit the GitHub comment limit]'
    attempt = '\nrun: ' + run_id if run_id else ''
    return '## %s\nhead: %s%s\n\n%s\n\nVERDICT: %s\n' % (title, head, attempt, text, verdict)



def parse_args(argv):
    """(pr, who, adversarial), or None when the arguments are not valid."""
    flags = [a for a in argv if a.startswith('-')]
    rest = [a for a in argv if not a.startswith('-')]
    if len(rest) != 2 or rest[1] not in ('codex', 'claude', 'auto') or set(flags) - {'--adversarial'}:
        return None
    if '--adversarial' in flags and rest[1] != 'codex':
        return None
    return rest[0], rest[1], '--adversarial' in flags


def metadata(pr):
    out = sh([GH, 'pr', 'view', pr, '--json', 'number,baseRefName,baseRefOid,headRefOid,url'], timeout=60)
    if out.returncode:
        raise ValueError('gh pr view failed: ' + out.stderr.strip())
    try:
        data = json.loads(out.stdout)
        if (not isinstance(data, dict) or not isinstance(data.get('number'), int) or
                not re.fullmatch(r'[0-9a-f]{40}', data.get('baseRefOid', '')) or
                not re.fullmatch(r'[0-9a-f]{40}', data.get('headRefOid', '')) or
                not isinstance(data.get('baseRefName'), str) or not data['baseRefName'] or
                not str(data.get('url', '')).startswith('https://')):
            raise ValueError()
        return data
    except (ValueError, TypeError):
        raise ValueError('gh pr view returned invalid PR metadata') from None


def unchanged(pr, expected):
    """Check tracked source and both local and remote heads before and after long-running work."""
    head = sh(['git', 'rev-parse', 'HEAD'])
    dirty = sh(['git', 'status', '--porcelain', '--untracked-files=normal'])
    if head.returncode or dirty.returncode or head.stdout.strip() != expected['headRefOid'] or dirty.stdout.strip():
        raise ValueError('the checkout must be clean and at the unchanged PR head')
    current = metadata(pr)
    if any(current[k] != expected[k] for k in ('headRefOid', 'baseRefName', 'baseRefOid', 'number', 'url')):
        raise ValueError('the PR head, base or repository changed during the run')


def post_comment(pr, expected, body):
    unchanged(pr, expected)
    body, count = redact(body)
    print('pr_review: redacted %d secret-like string(s) before posting' % count)
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as fh:
        fh.write(body)
    try:
        posted = sh([GH, 'pr', 'comment', expected['url'], '--body-file', fh.name], timeout=60)
    finally:
        os.unlink(fh.name)
    if posted.returncode:
        raise ValueError('posting the comment failed: ' + posted.stderr.strip())
    print(body)


def test_count(text):
    """Recognize completed unittest, pytest and Jest/Vitest summaries; no count means no passing evidence."""
    summaries = re.findall(r'^Ran (\d+) tests? in [^\n]*\n(?:[ \t]*\n)?'
                           r'(OK|FAILED)(?: \(([^)\n]*)\))?[ \t]*$', text, re.M)
    if summaries:
        counts = []
        for total, outcome, details in summaries:
            if outcome != 'OK':
                return 0
            excluded = 0
            for item in details.split(','):
                key, _, value = item.strip().partition('=')
                if key in ('skipped', 'expected failures'):
                    if not value.isdigit():
                        return 0
                    excluded += int(value)
            counts.append(max(0, int(total) - excluded))
        return sum(counts)
    if re.search(r'^Ran \d+ tests? in ', text, re.M):
        return 0  # an interrupted unittest summary does not establish completed tests
    counts = []
    for pattern in (r'^[= \t]*(\d+) passed(?:, \d+ (?:skipped|deselected|xfailed|warnings?))* in [\d.]+s[= \t]*$',
                    r'^[ \t]*Tests:[ \t]+(?:\d+ skipped, )?(\d+) passed, \d+ total[ \t]*$',
                    r'^[ \t]*Tests[ \t]+(\d+) passed(?: \| \d+ skipped)? \(\d+\)[ \t]*$'):
        counts.extend(int(n) for n in re.findall(pattern, text, re.M))
    return max(counts, default=0)


def checkout_root():
    root = sh(['git', 'rev-parse', '--show-toplevel'], timeout=60)
    path = root.stdout.strip()
    if root.returncode or not os.path.isabs(path) or not os.path.isdir(path):
        raise ValueError('could not resolve the Git checkout root for the full tests')
    return path


def run_tests(command, slug):
    root = checkout_root()
    out = review_worker.execute(root, slug, command)
    text = out.stdout + out.stderr
    print(text)
    count = test_count(text)
    return {'status': 'PASS' if out.returncode == 0 and count > 0 else 'FAIL', 'count': count,
            'command': command, 'exit_code': out.returncode}


def fetch_base(meta):
    """Fetch the upstream PR base object. A fork's origin and its branch names are not authoritative."""
    url = re.fullmatch(r'(https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/[0-9]+', meta.get('url', ''))
    oid = meta.get('baseRefOid')
    if not url or not isinstance(oid, str) or not re.fullmatch(r'[a-f0-9]{40}', oid):
        raise ValueError('invalid PR repository or base commit')
    fetched = sh(['git', 'fetch', '-q', '--no-tags', '--no-recurse-submodules', url[1] + '.git', oid], timeout=120)
    if fetched.returncode:
        raise ValueError('could not fetch the PR base: ' + fetched.stderr.strip())
    resolved = sh(['git', 'rev-parse', '--verify', 'FETCH_HEAD^{commit}'])
    if resolved.returncode or resolved.stdout.strip() != oid:
        raise ValueError('fetched commit does not match the upstream PR base')
    return oid


def persisted_blockers(pr, meta):
    """A provider outage cannot supersede a blocking review already on this exact head."""
    out = sh([GH, 'pr', 'view', meta['url'], '--json', 'comments,headRefOid,baseRefName,baseRefOid'], timeout=60)
    if out.returncode:
        raise ValueError('could not verify the persisted review verdicts')
    try:
        data = json.loads(out.stdout)
        if (not isinstance(data, dict) or not isinstance(data.get('comments'), list)
                or any(data.get(key) != meta[key] for key in ('headRefOid', 'baseRefName', 'baseRefOid'))):
            raise ValueError()
    except (TypeError, AttributeError, ValueError):
        raise ValueError('persisted reviews do not describe the current PR') from None
    authors = {str(name).lstrip('@').lower() for name in (common.config().get('pr_gate') or {}).get('reviewers', [])}
    if not authors:
        login = sh([GH, 'api', 'user', '--jq', '.login'], timeout=60)
        if login.returncode or not login.stdout.strip():
            raise ValueError('could not verify the signed-in review author')
        authors = {login.stdout.strip().lower()}
    from guard import review_verdict
    return [name for name in ('Codex', 'Claude')
            if review_verdict(data, name, meta['headRefOid'], authors) == 'BLOCK']



def main(argv):
    args = parse_args(argv)
    if not args:
        print(__doc__)
        return 3
    pr, who, adversarial = args
    meta, attempt, results, tests, finished = None, None, {}, None, False

    def finish(status, code):
        nonlocal finished
        if attempt is not None:
            providers = {name: review.verdict or ('UNAVAILABLE' if review.unavailable else 'INCOMPLETE')
                         for name, review in results.items()}
            reasons = {name: review.unavailable for name, review in results.items() if review.unavailable}
            outcome = dict(attempt, status=status, providers=providers, reasons=reasons, tests=tests)
            post_comment(pr, meta, RUN_TITLE + '\n' + RUN_MARK + json.dumps(outcome, separators=(',', ':')) + '\n')
            finished = True
        return code

    try:
        meta = metadata(pr)
        unchanged(pr, meta)
        base_name, head = meta['baseRefName'], meta['headRefOid']
        if who == 'auto':
            attempt = {'run_id': uuid.uuid4().hex, 'head': head, 'base': base_name,
                       'base_oid': meta['baseRefOid'], 'status': 'RUNNING'}
            post_comment(pr, meta, RUN_TITLE + '\n' + RUN_MARK + json.dumps(attempt, separators=(',', ':')) + '\n')
        base = fetch_base(meta)
        if who == 'auto':
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {'Codex': pool.submit(codex_review, base, False),
                           'Claude': pool.submit(claude_review, base)}
                results = {name: future.result() for name, future in futures.items()}
        else:
            results = {who.capitalize(): codex_review(base, adversarial) if who == 'codex' else claude_review(base)}
        unchanged(pr, meta)
        # A failed GitHub request after a PASS must never hide a blocker already known to this process.
        for name, review in sorted(results.items(), key=lambda item: item[1].verdict != 'BLOCK'):
            if review.verdict:
                title = name + (' adversarial review' if adversarial else ' review')
                text, count = redact(review.text)
                print('pr_review: redacted %d secret-like string(s) from the review before posting' % count)
                post_comment(pr, meta, verdict_comment(title, head, text, review.verdict,
                                                     attempt['run_id'] if attempt else None))
            else:
                text, _ = redact(review.text)
                print('pr_review: %s %s; no verdict posted.\n%s' %
                      (name, 'unavailable (' + review.unavailable + ')' if review.unavailable else 'review incomplete', text))
        if any(r.verdict == 'BLOCK' for r in results.values()):
            return finish('BLOCK', 1)
        if who == 'auto' and persisted_blockers(pr, meta):
            print('pr_review: a blocking verdict on this head has not been superseded')
            return finish('BLOCK', 1)
        if all(r.verdict == 'PASS' for r in results.values()):
            return finish('PASS', 0)
        if who != 'auto':
            return finish('INCOMPLETE', 3)
        missing = [(name, r) for name, r in results.items() if r.unavailable]
        passed = [r for r in results.values() if r.verdict == 'PASS']
        gate = common.config().get('pr_gate') or {}
        if len(missing) != 1 or len(passed) != 1 or gate.get('allow_single_on_unavailable') is not True:
            print('pr_review: fallback needs one real PASS, one confirmed unavailable provider and enabled policy')
            return finish('INCOMPLETE', 3)
        slug = '/'.join(meta['url'].split('/')[3:5])
        command = common.review_test_command(slug)
        if command is None:
            raise ValueError('no full test command is configured for ' + slug)
        unchanged(pr, meta)
        tests = run_tests(command, slug)
        unchanged(pr, meta)
        name, review = missing[0]
        evidence = {'head': head, 'base': base_name, 'base_oid': meta['baseRefOid'],
                    'run_id': attempt['run_id'], 'provider': name, 'status': 'UNAVAILABLE',
                    'reason': review.unavailable, 'tests': tests}
        detail, _ = redact(review.text)
        body = EVIDENCE_TITLE + '\n' + EVIDENCE_MARK + json.dumps(evidence, separators=(',', ':')) + '\n\n' + detail[:4000] + '\n'
        post_comment(pr, meta, body)
        if tests['status'] != 'PASS':
            print('pr_review: fallback held: the full tests failed or reported no positive test count')
            return finish('FAIL', 3)
        if persisted_blockers(pr, meta):
            print('pr_review: a blocking verdict on this head has not been superseded')
            return finish('BLOCK', 1)
        code = finish('PASS', 0)
        print('pr_review: fallback satisfied by one PASS and %d passing tests' % tests['count'])
        return code
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('pr_review: ' + str(exc))
        if attempt is not None and not finished:
            try:
                finish('BLOCK' if any(review.verdict == 'BLOCK' for review in results.values()) else 'INCOMPLETE', 3)
            except (OSError, ValueError, subprocess.SubprocessError) as posting:
                print('pr_review: attempt remains held: ' + str(posting))
        return 3



if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
