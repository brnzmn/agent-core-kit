"""What a session opening in a repo should know first: where it deploys, which accounts it uses,
and the latest entries of its decision log. Read from files in the repo only; nothing is called
over the network. Returns no lines when the repo has none of these, so the hook stays quiet.
"""
import glob
import json
import os
import re
import subprocess

DECISION_LOGS = ('docs/decisions.md', 'docs/DECISIONS.md', 'DECISIONS.md', 'decisions.md',
                 'docs/decision-log.md', 'docs/decision_log.md')
ADR_DIRS = ('docs/adr', 'docs/decisions', 'adr')
SSH_TARGET = re.compile(r'\b(?:ssh|rsync|scp)\b[^\n]*?\b([A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})')
REMOTE_HOST = re.compile(r'\b[A-Z_]*HOST[A-Z_]*=["\']?([A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})')
USAGE = re.compile(r'^#[^\n]*?\bUsage:[ \t]*(\S[^\n]*)$', re.M)
SCRIPT_EXT = ('', '.sh', '.bash', '.py', '.js', '.mjs', '.ts')
ENTRY = re.compile(r'^(?:[-*]\s+|#{2,3}\s+)(.+)')
WIDTH = 180


def _read(path, limit=200000):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read(limit)
    except OSError:
        return ''


def _toml_value(text, key):
    m = re.search(r'^\s*%s\s*=\s*["\']([^"\']+)["\']' % key, text, re.M)
    return m.group(1) if m else None


def _short(text):
    text = re.sub(r'\*\*|__|`', '', text).strip()
    return text if len(text) <= WIDTH else text[:WIDTH - 1].rstrip() + '…'


SCHEME_URL = re.compile(r'^([A-Za-z][A-Za-z0-9+.-]*://)([^/]*)(.*)$', re.S)
SCP_URL = re.compile(r'^(?:[^/]*@)?([A-Za-z0-9.-]+:)(?!//)(.*)$', re.S)


def clean_url(url):
    """A remote URL that is safe to print: no userinfo (user, user:token, token@) and no query string or fragment.
    Handles scheme URLs and scp-style `user@host:path`; a plain local path comes back as it was."""
    url = (url or '').strip()
    m = SCHEME_URL.match(url)
    if m:
        scheme, authority, rest = m.groups()
        host = authority.rpartition('@')[2]  # the userinfo may itself hold an @
        return scheme + re.sub(r'[?#].*', '', host + rest, flags=re.S)
    m = SCP_URL.match(url)
    if m:
        return m.group(1) + re.sub(r'[?#].*', '', m.group(2), flags=re.S)
    return url


def github_remote(root):
    try:
        url = subprocess.run(['git', '-C', root, 'config', '--get', 'remote.origin.url'],
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return None
    url = clean_url(url)
    m = re.search(r'github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$', url)
    return 'GitHub ' + m.group(1) if m else ('git remote ' + url if url else None)


def deploy_scripts(root):
    """(deploy commands, ssh hosts) from scripts/deploy, a file or a folder of scripts."""
    path = os.path.join(root, 'scripts', 'deploy')
    files = []
    if os.path.isfile(path):
        files = [path]
    elif os.path.isdir(path):
        files = sorted(p for p in glob.glob(os.path.join(path, '*'))
                       if os.path.isfile(p) and os.path.splitext(p)[1] in SCRIPT_EXT)
    cmds, hosts = [], []
    for f in files:
        text = _read(f)
        rel = os.path.relpath(f, root)
        usage = USAGE.search(text)
        cmds.append(usage.group(1).strip() if usage and rel in usage.group(1) else rel)
        for m in list(SSH_TARGET.finditer(text)) + list(REMOTE_HOST.finditer(text)):
            if m.group(1) not in hosts:
                hosts.append(m.group(1))
    return cmds, hosts


def deploy_and_accounts(root):
    deploy, accounts = [], []
    cmds, hosts = deploy_scripts(root)
    if cmds:
        deploy.append(', '.join(cmds[:6]) + (' (+%d more)' % (len(cmds) - 6) if len(cmds) > 6 else ''))
    for skill in sorted(glob.glob(os.path.join(root, '.claude', 'skills', '*deploy*', 'SKILL.md'))):
        deploy.append('skill ' + os.path.relpath(os.path.dirname(skill), root))
    try:
        vercel = json.loads(_read(os.path.join(root, '.vercel', 'project.json')) or 'null')
    except ValueError:
        vercel = None
    if isinstance(vercel, dict) and (vercel.get('projectName') or vercel.get('projectId')):
        deploy.append('Vercel project %s' % (vercel.get('projectName') or vercel.get('projectId')))
        if vercel.get('orgId'):
            accounts.append('Vercel team %s' % vercel['orgId'])
    ref = _read(os.path.join(root, 'supabase', '.temp', 'project-ref')).strip()
    if ref:
        accounts.append('Supabase project %s' % ref)
    for name in ('wrangler.toml', 'wrangler.json', 'wrangler.jsonc'):
        text = _read(os.path.join(root, name))
        if not text:
            continue
        worker = _toml_value(text, 'name') or (re.search(r'"name"\s*:\s*"([^"]+)"', text) or [None, None])[1]
        account = _toml_value(text, 'account_id') or (re.search(r'"account_id"\s*:\s*"([^"]+)"', text) or [None, None])[1]
        if worker:
            deploy.append('Cloudflare worker %s' % worker)
        if account:
            accounts.append('Cloudflare account %s' % account)
        break
    app = _toml_value(_read(os.path.join(root, 'fly.toml')), 'app')
    if app:
        deploy.append('Fly app %s' % app)
    for host in hosts[:3]:
        accounts.append('ssh %s' % host)
    remote = github_remote(root)
    if remote:
        accounts.insert(0, remote)
    return deploy, accounts


def decisions(root, n=5):
    """(source, last n entries) from the repo's decision log, or (None, [])."""
    for rel in DECISION_LOGS:
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            entries = [m.group(1) for m in map(ENTRY.match, _read(path, 2000000).splitlines()) if m]
            if entries:
                return rel, [_short(e) for e in entries[-n:]]
    for rel in ADR_DIRS:
        files = sorted(glob.glob(os.path.join(root, rel, '*.md')))
        files = [f for f in files if os.path.basename(f).lower() not in ('readme.md', 'index.md', 'template.md')]
        if files:
            return rel + '/', [os.path.splitext(os.path.basename(f))[0] for f in files[-n:]]
    return None, []


def lines(root):
    """Context lines for a session in this repo; [] when it has none of these."""
    deploy, accounts = deploy_and_accounts(root)
    source, entries = decisions(root)
    if not (deploy or accounts or entries):
        return []
    out = ['Repo context for %s (read from its files; the files win if this is stale):' % os.path.basename(root)]
    if deploy:
        out.append('- Deploy targets: ' + '; '.join(deploy))
    if accounts:
        out.append('- Accounts: ' + '; '.join(accounts))
    if entries:
        out.append('- Last %d decisions in %s:' % (len(entries), source))
        out += ['  - ' + e for e in entries]
    return out
