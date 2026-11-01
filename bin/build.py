#!/usr/bin/env python3
"""Build, install, check and undo agent-core-kit's targets. install.sh drives this; you rarely call it yourself.

  build.py                              write build/ (rendered CLAUDE.md, AGENTS.md, subagents, skills, settings, Codex files)
  build.py --check                      compare what is installed with build/; exit 1 on drift
  build.py --validate                   exit 1 if ~/.claude/settings.json, ~/.codex/hooks.json or local/config.json is not valid JSON
  build.py --snapshot                   copy the files the first install may change into the backup (symlinks noted)
  build.py --install-claude             CLAUDE.md, subagents, hooks, .core-dir and .local-dir into ~/.claude
  build.py --install-skills             skills into ~/.claude/skills and ~/.agents/skills (Codex scans it)
  build.py --install-codex              Codex rulebook, rules, agents and hooks into ~/.codex (all but the rulebook wait while Codex runs)
  build.py --install-plugin             OpenAI's Codex plugin for Claude Code from vendor/codex-plugin-cc
  build.py --merge-settings fresh|clean|keep
                                        merge build/claude/settings.json into ~/.claude/settings.json; all three keep your
                                        own rules, hooks and settings. clean (first install after --clean-slate) also drops
                                        your hook entries whose scripts --clean-slate moved into the backup
  build.py --clean-slate                move ~/.claude/{agents,skills,commands,rules,hooks} into the backup (nothing deleted)
  build.py --uninstall                  take the kit out of both tools and put back what the install changed (the kit's files
                                        are moved into a backup, nothing deleted)
  build.py --restore NAME               put local/backups/NAME back

Backups go to $AGENT_CORE_BACKUP, else local/backups/<timestamp>/, and are only created when a file is replaced.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:  # Python 3.11+; older Pythons skip TOML validation and use a plain-text check for config.toml
    import tomllib
except ImportError:
    tomllib = None

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('AGENT_CORE_DIR', CORE)
sys.path.insert(0, os.path.join(CORE, 'claude', 'hooks'))
import common  # noqa: E402

BUILD = os.path.join(CORE, 'build')
HOME = common.HOME
CLAUDE_DIR = os.path.join(HOME, '.claude')
CODEX_DIR = os.path.join(HOME, '.codex')
HOOKS_SRC = os.path.join(CORE, 'claude', 'hooks')
HOOKS_DST = os.path.join(CLAUDE_DIR, 'hooks', 'agent-core')
LEGACY_STATE = os.path.join(HOOKS_DST, '.install-state.json')  # older kits kept the install state here; --sync moves it
SKILLS = os.path.join(CORE, 'skills')
TEMPLATE = os.path.join(CORE, 'claude', 'settings.json')
PLUGIN_SRC = os.path.join(CORE, 'vendor', 'codex-plugin-cc')  # OpenAI's codex-plugin-cc, pinned in vendor/ (Apache-2.0)
PLUGIN_MARKET, PLUGIN = 'openai-codex', 'codex@openai-codex'
PLUGINS_DIR = os.path.join(CLAUDE_DIR, 'plugins')
SKILL_DIRS = (os.path.join(CLAUDE_DIR, 'skills'), os.path.join(HOME, '.agents', 'skills'))
MANIFEST = '.agent-core-manifest.json'
SKIP = ('.DS_Store', '__pycache__', '*.pyc')
ARCHIVE_DIRS = ('agents', 'skills', 'commands', 'rules', 'hooks')
# Claude subagent file -> Codex agent (name, reasoning effort). The model line is added only when config.json names
# one: codex.models maps a role (either name) to a model; without it Codex uses its own default.
CODEX_ROLES = {'Explore': ('explorer', 'medium'), 'worker': ('worker', 'high'),
               'verifier': ('verifier', 'medium'), 'reviewer': ('reviewer', 'high')}
READ_ONLY = 'Read-only: never create, edit, move or delete files, and never commit or push.\n\n'
CODEX_HOOK_SCRIPT = os.path.join(HOOKS_DST, 'codex_hook.py')
# The system Python's path never moves; a changed hooks.json command would need the user to trust it again in /hooks.
HOOK_PYTHON = '/usr/bin/python3' if os.path.exists('/usr/bin/python3') else 'python3'
CODEX_PROC = re.compile(r'(^|/)codex$|/Codex[^/]*\.app/|codex-code-mode-host', re.M)
OURS = 'hooks/agent-core/'  # marks a hook command as the kit's, in settings.json and hooks.json


# ---------------------------------------------------------------- small helpers

def digest(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_json(path):
    """Parsed JSON, or None when the file does not exist. Raises ValueError when it is not JSON."""
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def load_json_safe(path):
    try:
        return load_json(path)
    except (ValueError, OSError):
        return None


def is_exec(path):
    return bool(os.stat(path).st_mode & 0o111)


def dedupe(items):
    return list(dict.fromkeys(items))


def mine(entry):
    return OURS in json.dumps(entry)


def write_bytes(path, data, exe=False, mode=None):
    """Atomic write (a temp file next to the target, then rename). Resolves a symlinked target first."""
    if os.path.islink(path):
        path = os.path.realpath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.agent-core.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(data)
    os.chmod(tmp, mode if mode is not None else (0o755 if exe else 0o644))
    os.replace(tmp, path)


def write_json(path, obj):
    """Write a JSON file whose 'updated' stamp moves only when something else changed, so a repeat run changes nothing."""
    old = load_json_safe(path)
    if isinstance(old, dict) and dict((k, v) for k, v in old.items() if k != 'updated') == \
            dict((k, v) for k, v in obj.items() if k != 'updated'):
        return
    obj = dict(obj, updated=time.strftime('%Y-%m-%dT%H:%M:%S'))
    write_bytes(path, (json.dumps(obj, indent=1, sort_keys=True) + '\n').encode('utf-8'))


def fresh_dir(path):
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)


def unique(path):
    out, n = path, 1
    while os.path.exists(out):
        n += 1
        out = '%s-%d' % (path, n)
    return out


def render_bytes(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return data
    return common.render(text).encode('utf-8') if '{{' in text else data


def render_json(obj):
    if isinstance(obj, str):
        return common.render(obj)
    if isinstance(obj, list):
        return [render_json(x) for x in obj]
    if isinstance(obj, dict):
        return dict((k, render_json(v)) for k, v in obj.items())
    return obj


def sync_cmd():
    return common.kit_cmd('install.sh') + ' --sync'


# ---------------------------------------------------------------- backups and install state

LINKS = 'LINKS.json'  # in a backup folder: files that were symlinks when saved, with where the link pointed
REAL_DIRS = 'REALDIRS'  # in an uninstall backup: folders (~/.claude/skills ...) that were real folders when the kit's files left


def file_mode(path, default):
    """The permission bits of an existing file (through a symlink), else default."""
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return default


def read_links(folder):
    return load_json_safe(os.path.join(folder, LINKS)) or {}


def read_lines(folder, name):
    try:
        with open(os.path.join(folder, name), encoding='utf-8') as fh:
            return [l.strip() for l in fh if l.strip()]
    except OSError:
        return []


class Backup(object):
    """Copies or moves files into local/backups/<stamp>/{claude,codex,home,local}/<path under that folder>, created on
    first use. A file that was a symlink is saved by content, and LINKS.json notes the link and its target."""

    def __init__(self, path=None):
        self.path = path
        self.count = 0

    def dir(self):
        if not self.path:
            self.path = (os.environ.get('AGENT_CORE_BACKUP')
                         or os.path.join(common.LOCAL, 'backups', time.strftime('%Y%m%d-%H%M%S')))
        return self.path

    @staticmethod
    def rel(path):
        p = os.path.abspath(path)
        for prefix, tag in ((CLAUDE_DIR, 'claude'), (CODEX_DIR, 'codex'), (common.LOCAL, 'local'), (HOME, 'home')):
            if p == prefix or p.startswith(prefix + os.sep):
                return os.path.join(tag, os.path.relpath(p, prefix))
        return os.path.join('other', p.lstrip(os.sep))

    def note_link(self, rel, link):
        links = read_links(self.dir())
        links[rel] = {'link': link, 'target': os.readlink(link), 'real': os.path.realpath(link)}
        with open(os.path.join(self.dir(), LINKS), 'w', encoding='utf-8') as fh:
            json.dump(links, fh, indent=1, sort_keys=True)
            fh.write('\n')

    def save(self, path, label=None, quiet=False):
        """Copy a file in before it is replaced (first copy wins within one backup). A symlink is followed for the
        content and noted as a link. label is the path the file is known by when path is the real file behind a link."""
        shown = label or path
        real = os.path.realpath(path) if os.path.islink(path) else path
        if not os.path.isfile(real):
            return False
        rel = self.rel(shown)
        dst = os.path.join(self.dir(), rel)
        if os.path.exists(dst):
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(real, dst)
        linked = os.path.islink(shown)
        if linked:
            self.note_link(rel, shown)
        self.count += 1
        if not quiet:
            print('backup: %s%s' % (common.tilde(shown), ' (a symlink to %s; content saved, link noted)'
                                    % common.tilde(os.readlink(shown)) if linked else ''))
        return True

    def move(self, path):
        dst = unique(os.path.join(self.dir(), self.rel(path)))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(path, dst)
        self.count += 1
        return dst

    def archive(self, path):
        """Move a whole folder in and note it, so --restore puts the folder back as a whole."""
        dst = self.move(path)
        with open(os.path.join(self.dir(), 'ARCHIVED'), 'a', encoding='utf-8') as fh:
            fh.write(self.rel(path) + '\n')
        return dst

    def report(self):
        if self.count:
            print('backup: %s (restore with: %s --restore %s)'
                  % (common.tilde(self.path), common.kit_cmd('install.sh'), os.path.basename(self.path)))


BACKUP = Backup()
_STATE = None
_STATE_AT = None


def state_path():
    """The install state lives in local/ (git-ignored), not in ~/.claude/hooks, so --clean-slate cannot archive it."""
    return os.path.join(common.LOCAL, 'install-state.json')


def state():
    """What the kit recorded at install time:
    files      path -> digest of what the kit wrote there last
    settings   the allow and deny rules, and the autoMode.environment lines (autoModeEnvironment), the kit added to settings.json
    changes    settings.json keys the kit set: label -> {had, previous, set}
    created    settings.json containers the kit created (removed again when they end up empty)
    createdFiles  JSON files the kit created
    droppedAsk ask rules removed for permission_mode bypassPermissions
    droppedHooks  your hook entries --clean-slate dropped because it moved their scripts away
    originals  your files the kit replaced: path -> {backup, written}; backup is None when there was no file
    archived   folders --clean-slate moved away: ~/.claude-relative name -> backup folder (relative to local/)"""
    global _STATE, _STATE_AT
    if _STATE is None or _STATE_AT != state_path():
        _STATE = load_json_safe(state_path())
        if not isinstance(_STATE, dict):
            _STATE = load_json_safe(LEGACY_STATE)  # an install from before the state moved into local/
        if not isinstance(_STATE, dict):
            _STATE = {}
        _STATE_AT = state_path()
        for key, empty in (('files', {}), ('settings', {}), ('changes', {}), ('created', []), ('createdFiles', []),
                           ('droppedAsk', []), ('droppedHooks', []), ('originals', {}), ('archived', {})):
            if not isinstance(_STATE.get(key), type(empty)):
                _STATE[key] = empty
    return _STATE


def save_state():
    if _STATE is None:
        return
    write_json(state_path(), _STATE)
    if os.path.isfile(LEGACY_STATE):  # migrated: the copy in local/ is the one that counts now
        os.unlink(LEGACY_STATE)


def local_ref(path):
    """A backup path written relative to local/ when it sits inside it, so a moved local/ keeps working."""
    rel = os.path.relpath(path, common.LOCAL)
    return path if rel.startswith(os.pardir) else rel


def note_original(shown, written):
    """Remember where the copy of your own file lives before the kit's replaces it (the first copy wins)."""
    originals = state()['originals']
    if shown not in originals:
        copy = os.path.join(BACKUP.dir(), BACKUP.rel(shown))
        originals[shown] = {'backup': local_ref(copy) if os.path.isfile(copy) else None, 'written': written}


def put_bytes(data, dst, exe=False, mine_file=True, mode=None, original=False):
    """Write data to dst when it differs. Returns 'new', 'updated' or 'same'. A file that is not the copy the kit wrote
    last time (yours, or one you edited) is backed up first. mine_file=False marks a file that always belongs to you
    (settings.json, hooks.json, config.toml): it is backed up whenever it changes, not recorded, and keeps its
    permission bits (mode is for a new file). original=True records that the kit took over a file of yours (or created
    it), so --uninstall can put your version back."""
    label = None
    if os.path.islink(dst):
        label, dst = dst, os.path.realpath(dst)
    shown = label or dst
    new = hashlib.sha256(data).hexdigest()
    status = 'new'
    if os.path.isfile(dst):
        cur = digest(dst)
        if cur == new:
            if exe and not os.access(dst, os.X_OK):
                os.chmod(dst, os.stat(dst).st_mode | 0o111)
            if mine_file:
                state()['files'][dst] = new
            return 'same'
        if not mine_file or state()['files'].get(dst) != cur:
            BACKUP.save(dst, label)
        if original and not generated_by_kit(dst):
            note_original(shown, new)
        status = 'updated'
        if not mine_file:
            mode = file_mode(dst, mode if mode is not None else 0o644)
    elif original and shown not in state()['originals']:
        state()['originals'][shown] = {'backup': None, 'written': new}
    write_bytes(dst, data, exe=exe, mode=mode)
    if mine_file:
        state()['files'][dst] = new
    if shown in state()['originals']:
        state()['originals'][shown]['written'] = new
    return status


def put(src, dst, exe=None, original=False):
    with open(src, 'rb') as fh:
        data = fh.read()
    return put_bytes(data, dst, exe=is_exec(src) if exe is None else exe, original=original)


# ---------------------------------------------------------------- build/

def frontmatter(text):
    m = re.match(r'^---\n(.*?)\n---\n(.*)$', text, re.S)
    meta = dict(re.findall(r'^([A-Za-z_-]+):\s*(.*?)\s*$', m.group(1), re.M)) if m else {}
    return meta, (m.group(2) if m else text)


def toml_str(value):
    return json.dumps(value, ensure_ascii=False)


def skill_names():
    if not os.path.isdir(SKILLS):
        return []
    return sorted(d for d in os.listdir(SKILLS) if os.path.isfile(os.path.join(SKILLS, d, 'SKILL.md')))


def skill_only(name):
    """'claude' or 'codex' when the skill's frontmatter says `agent-core-only: <tool>`, else None."""
    head = common.read(os.path.join(SKILLS, name, 'SKILL.md')).split('\n---', 1)[0]
    m = re.search(r'^agent-core-only:\s*(\w+)\s*$', head, re.M)
    return m.group(1) if m else None


def skills_for(tool):
    return [n for n in skill_names() if skill_only(n) in (None, tool)]


def need_rulebook():
    if not os.path.isfile(common.RULEBOOK):
        raise OSError('%s not found. Run bash %s to create your local/ folder.'
                      % (common.tilde(common.RULEBOOK), common.tilde(os.path.join(CORE, 'install.sh'))))


def build_instructions(quiet=True):
    need_rulebook()
    for adapter, name in (('claude', 'CLAUDE.md'), ('codex', 'AGENTS.md')):
        text = common.build_text(adapter)
        out = os.path.join(BUILD, adapter, name)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as fh:
            fh.write(text)
        if not quiet:
            print('built %s (%d lines)' % (common.tilde(out), common.count_lines(text)))


def build_claude_agents():
    out = os.path.join(BUILD, 'claude', 'agents')
    fresh_dir(out)
    for path in sorted(glob.glob(os.path.join(CORE, 'claude', 'agents', '*.md'))):
        with open(path, 'rb') as fh:
            write_bytes(os.path.join(out, os.path.basename(path)), render_bytes(fh.read()))


def build_skills():
    dest = os.path.join(BUILD, 'skills')
    fresh_dir(dest)
    for name in skill_names():
        src = os.path.join(SKILLS, name)
        for root, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d != '__pycache__')
            for f in sorted(files):
                if f == '.DS_Store' or f.endswith('.pyc'):
                    continue
                p = os.path.join(root, f)
                with open(p, 'rb') as fh:
                    write_bytes(os.path.join(dest, name, os.path.relpath(p, src)), render_bytes(fh.read()), exe=is_exec(p))


def build_core_dir():
    """The two paths the installed hooks need to find the kit and the personal folder without environment variables."""
    write_bytes(os.path.join(BUILD, 'claude', 'core-dir'), (common.CORE + '\n').encode('utf-8'))
    write_bytes(os.path.join(BUILD, 'claude', 'local-dir'), (common.LOCAL + '\n').encode('utf-8'))


def protected_rules(dirs):
    """Deny rules keeping agents from editing the user's read-only data folders (config protected_dirs)."""
    rules = []
    for d in ([dirs] if isinstance(dirs, str) else (dirs or [])):
        d = str(d).strip().rstrip('/')
        if not d or d in ('~', '/'):
            continue
        if d.startswith('~'):
            rules.append('Edit(%s/**)' % d)
        elif d.startswith('/'):
            rules.append('Edit(/%s/**)' % d)
        else:
            rules.append('Edit(**/%s/**)' % d.strip('/'))
    return dedupe(rules)


def settings_template():
    """claude/settings.json with the kit path filled in and the user's config applied."""
    ours = render_json(load_json(TEMPLATE))
    cfg = common.config()
    ours.pop('switchModelsOnFlag', None)
    ours.pop('autoMode', None)
    perms = ours.setdefault('permissions', {})
    perms.pop('defaultMode', None)
    perms.pop('ask', None)
    perms['deny'] = dedupe(list(perms.get('deny') or []) + protected_rules(cfg.get('protected_dirs')))
    perms['allow'] = dedupe(perms.get('allow') or [])
    mode = str(cfg.get('permission_mode') or '').strip()
    if mode:
        perms['defaultMode'] = mode
    if not cfg.get('stop_hook', True):
        (ours.get('hooks') or {}).pop('Stop', None)
    env_lines = cfg.get('auto_mode_environment') or []
    lines = [str(x) for x in ([env_lines] if isinstance(env_lines, str) else env_lines) if str(x).strip()]
    if lines:
        ours['autoMode'] = {'environment': ['$defaults'] + lines}
    return ours


def build_settings():
    ours = settings_template()
    write_bytes(os.path.join(BUILD, 'claude', 'settings.json'), (json.dumps(ours, indent=2) + '\n').encode('utf-8'))
    return ours


def codex_agents():
    """{name: TOML text} for ~/.codex/agents, generated from the same role files as the Claude subagents."""
    models = dict((str(k).lower(), v.strip()) for k, v in ((common.config().get('codex') or {}).get('models') or {}).items()
                  if isinstance(v, str) and v.strip())
    out = {}
    for role, (name, effort) in sorted(CODEX_ROLES.items()):
        with open(os.path.join(CORE, 'claude', 'agents', role + '.md'), encoding='utf-8') as fh:
            meta, body = frontmatter(common.render(fh.read()))
        body = (READ_ONLY if meta.get('disallowedTools') else '') + body.strip() + '\n'
        if "'''" in body:
            raise ValueError('claude/agents/%s.md contains a TOML literal-string delimiter' % role)
        model = models.get(name.lower()) or models.get(role.lower())
        out[name] = ('# Generated by %s from claude/agents/%s.md. Edit that file, then run: %s\n'
                     % (common.tilde(os.path.join(CORE, 'bin', 'build.py')), role, sync_cmd()) +
                     'name = %s\ndescription = %s\n' % (toml_str(name), toml_str(meta.get('description', ''))) +
                     ('model = %s\n' % toml_str(model) if model else '') +
                     'model_reasoning_effort = %s\n' % toml_str(effort) +
                     "developer_instructions = '''\n%s'''\n" % body)
    return out


def codex_rules():
    import guard
    out = ['# Generated by %s from CODEX_RULES in claude/hooks/guard.py. A second layer under' %
           common.tilde(os.path.join(CORE, 'bin', 'build.py')),
           '# the agent-core PreToolUse hook; edit the table there, then run: ' + sync_cmd(), '']
    for pattern, key in guard.CODEX_RULES:
        example = ' '.join(p if isinstance(p, str) else p[0] for p in pattern)
        out.append('prefix_rule(pattern=%s, decision="forbidden", justification=%s, match=[%s])'
                   % (json.dumps(pattern), json.dumps('agent-core: ' + common.render(guard.MSG[key])), json.dumps(example)))
    return '\n'.join(out) + '\n'


def codex_hooks():
    def hook(mode, timeout=10):
        return {'type': 'command', 'command': '%s %s %s' % (HOOK_PYTHON, CODEX_HOOK_SCRIPT, mode), 'timeout': timeout}
    return {'PreToolUse': [{'matcher': '^Bash$', 'hooks': [hook('pre-bash')]},
                           {'matcher': '^apply_patch$', 'hooks': [hook('pre-patch')]}],
            'SessionStart': [{'matcher': '^startup$', 'hooks': [hook('session-start', 15)]}],
            'UserPromptSubmit': [{'hooks': [hook('prompt')]}],
            'Stop': [{'hooks': [hook('stop')]}]}


def validate_toml(texts, quiet=True):
    if tomllib is None:
        if not quiet:
            print('note: Python %d.%d has no tomllib, so the Codex agent files were not validated as TOML'
                  % sys.version_info[:2])
        return
    for name, text in texts.items():
        try:
            tomllib.loads(text)
        except ValueError as exc:
            raise ValueError('the generated Codex agent %s is not valid TOML: %s' % (name, exc))


def build_codex(quiet=True):
    base = os.path.join(BUILD, 'codex')
    write_bytes(os.path.join(base, 'rules', 'agent-core.rules'), codex_rules().encode('utf-8'))
    write_bytes(os.path.join(base, 'hooks.json'), (json.dumps({'hooks': codex_hooks()}, indent=2) + '\n').encode('utf-8'))
    agents = codex_agents()
    validate_toml(agents, quiet)
    fresh_dir(os.path.join(base, 'agents'))
    for name, text in agents.items():
        write_bytes(os.path.join(base, 'agents', name + '.toml'), text.encode('utf-8'))
    if not quiet:
        print('built %d Codex agents in %s' % (len(agents), common.tilde(os.path.join(base, 'agents'))))


def build(quiet=False):
    build_instructions(quiet)
    build_claude_agents()
    build_skills()
    build_core_dir()
    build_settings()
    build_codex(quiet)
    if not quiet:
        print('built %d skills in %s' % (len(skill_names()), common.tilde(os.path.join(BUILD, 'skills'))))


# ---------------------------------------------------------------- what belongs to the kit, source -> destination

def walk_files(root):
    for r, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for f in sorted(files):
            if f != '.DS_Store' and not f.endswith('.pyc'):
                yield os.path.join(r, f)


def claude_agents_dir():
    return os.path.join(CLAUDE_DIR, 'agents')


def codex_agents_dir():
    return os.path.join(CODEX_DIR, 'agents')


def agent_split(pairs, dest):
    """(pairs the kit owns, names left alone). Like skills, agents are tracked in a manifest in their folder: a file of
    that name that is not in the manifest, was never written by the kit and differs from the kit's is yours."""
    managed = read_manifest(dest)
    owned, alone = [], []
    for src, dst in pairs:
        name = os.path.basename(dst)
        ours = name in managed or dst in state()['files'] or (os.path.isfile(dst) and digest(src) == digest(dst))
        if os.path.exists(dst) and not ours:
            alone.append(name)
        else:
            owned.append((src, dst))
    return owned, alone


def claude_agent_pairs():
    agents = os.path.join(BUILD, 'claude', 'agents')
    return [(p, os.path.join(claude_agents_dir(), os.path.relpath(p, agents))) for p in walk_files(agents)]


def claude_pairs():
    pairs = [(os.path.join(BUILD, 'claude', 'CLAUDE.md'), os.path.join(CLAUDE_DIR, 'CLAUDE.md')),
             (os.path.join(BUILD, 'claude', 'core-dir'), os.path.join(HOOKS_DST, '.core-dir')),
             (os.path.join(BUILD, 'claude', 'local-dir'), os.path.join(HOOKS_DST, '.local-dir'))]
    pairs += agent_split(claude_agent_pairs(), claude_agents_dir())[0]
    pairs += [(p, os.path.join(HOOKS_DST, os.path.relpath(p, HOOKS_SRC))) for p in walk_files(HOOKS_SRC)]
    return pairs


def skill_pairs():
    """Skill files the kit owns. A folder that exists and is not in the manifest is someone else's, left alone."""
    pairs = []
    for dest, tool in zip(SKILL_DIRS, ('claude', 'codex')):
        managed = read_manifest(dest)
        for n in skills_for(tool):
            if os.path.exists(os.path.join(dest, n)) and n not in managed:
                continue
            src = os.path.join(BUILD, 'skills', n)
            pairs += [(p, os.path.join(dest, n, os.path.relpath(p, src))) for p in walk_files(src)]
    return pairs


def codex_agent_pairs():
    agents = os.path.join(BUILD, 'codex', 'agents')
    return [(os.path.join(agents, f), os.path.join(codex_agents_dir(), f))
            for f in (sorted(os.listdir(agents)) if os.path.isdir(agents) else []) if f.endswith('.toml')]


def codex_pairs():
    """Files the kit owns in ~/.codex besides AGENTS.md and its entries in hooks.json."""
    pairs = [(os.path.join(BUILD, 'codex', 'rules', 'agent-core.rules'), os.path.join(CODEX_DIR, 'rules', 'agent-core.rules'))]
    return pairs + agent_split(codex_agent_pairs(), codex_agents_dir())[0]


def managed_pairs():
    pairs = claude_pairs() + skill_pairs()
    if os.path.isdir(CODEX_DIR):
        pairs.append((os.path.join(BUILD, 'codex', 'AGENTS.md'), os.path.join(CODEX_DIR, 'AGENTS.md')))
        pairs += codex_pairs()
    return pairs


# ---------------------------------------------------------------- install

def write_agent_manifest(dest, all_pairs, owned_pairs):
    """List the agent files the kit manages in dest (the same manifest skills use), so a same-named file of yours is
    never taken for the kit's. A file the kit no longer ships stays listed while it exists."""
    if not os.path.isdir(dest):
        return
    kit = set(os.path.basename(d) for _, d in all_pairs)
    mine_now = [os.path.basename(d) for _, d in owned_pairs if os.path.exists(d)]
    gone = [g for g in read_manifest(dest) if g not in kit and os.path.exists(os.path.join(dest, g))]
    write_json(manifest_path(dest), {'managed': sorted(set(mine_now + gone)),
                                              'source': os.path.join(CORE, 'claude', 'agents')})
    for g in gone:
        print('agents: %s is no longer in agent-core-kit; archive it with: %s %s'
              % (g, common.kit_cmd('bin/trash.py'), common.tilde(os.path.join(dest, g))))


def report_alone(alone, dest):
    for name in alone:
        print("agents: %s exists in %s and is not agent-core's; left alone, so the kit's %s is not installed. "
              "Rename one of them." % (name, common.tilde(dest), name))


def install_claude():
    build_instructions()
    build_claude_agents()
    build_core_dir()
    counts = {'new': 0, 'updated': 0, 'same': 0}
    everything = claude_agent_pairs()
    owned, alone = agent_split(everything, claude_agents_dir())
    report_alone(alone, claude_agents_dir())
    for src, dst in claude_pairs():
        counts[put(src, dst, exe=True if dst.startswith(HOOKS_DST + os.sep) and dst.endswith('.py') else None,
                   original=dst == os.path.join(CLAUDE_DIR, 'CLAUDE.md'))] += 1
    write_agent_manifest(claude_agents_dir(), everything, owned)
    save_state()
    print('claude: %d file(s) installed or updated, %d unchanged (CLAUDE.md, subagents, hooks)'
          % (counts['new'] + counts['updated'], counts['same']))
    return 0


def manifest_path(dest):
    """Where the manifest for dest lives. Codex loads its agents folder, so that manifest stays in local/ instead."""
    if os.path.abspath(dest) == os.path.abspath(codex_agents_dir()):
        return os.path.join(common.LOCAL, 'codex-agents-manifest.json')
    return os.path.join(dest, MANIFEST)


def read_manifest(dest):
    try:
        with open(manifest_path(dest), encoding='utf-8') as fh:
            return list(json.load(fh).get('managed') or [])
    except Exception:
        return []


def same_tree(a, b):
    def files(root):
        return dict((os.path.relpath(p, root), digest(p)) for p in walk_files(root))
    return files(a) == files(b)


def install_skills():
    """Add or replace only the skill folders the kit manages, listed in each folder's manifest. Never removes anything."""
    build_skills()
    for dest, tool in zip(SKILL_DIRS, ('claude', 'codex')):
        names = skills_for(tool)
        os.makedirs(dest, exist_ok=True)
        managed, owned = read_manifest(dest), []
        for n in names:
            src, dst = os.path.join(BUILD, 'skills', n), os.path.join(dest, n)
            if os.path.exists(dst) and n not in managed and not same_tree(src, dst):
                print("skills: %s exists in %s and is not agent-core's; left alone. Rename one of them."
                      % (n, common.tilde(dest)))
                continue
            for p in walk_files(src):
                put(p, os.path.join(dst, os.path.relpath(p, src)))
            owned.append(n)
        gone = sorted(set(managed) - set(names))
        write_json(manifest_path(dest), {
            'managed': sorted(owned + [g for g in gone if os.path.exists(os.path.join(dest, g))]), 'source': SKILLS})
        print('skills: %d installed in %s' % (len(owned), common.tilde(dest)))
        for g in gone:
            if os.path.exists(os.path.join(dest, g)):
                print('skills: %s is no longer in agent-core-kit; archive it with: %s %s'
                      % (g, common.kit_cmd('bin/trash.py'), common.tilde(os.path.join(dest, g))))
    save_state()
    return 0


def strip_ours(entries):
    """A hook list without the kit's commands; an entry left with no commands is dropped, your other commands stay."""
    out = []
    for e in entries or []:
        inner = e.get('hooks') if isinstance(e, dict) else None
        if not isinstance(inner, list):
            if not mine(e):
                out.append(e)
            continue
        keep = [h for h in inner if not mine(h)]
        if len(keep) == len(inner):
            out.append(e)
        elif keep:
            out.append(dict(e, hooks=keep))
    return out


def merged_hooks(current):
    """The user's own Codex hooks stay; the kit's entries (they call hooks/agent-core/) are replaced."""
    out = {}
    for event, entries in ((current or {}).get('hooks') or {}).items():
        keep = strip_ours(entries)
        if keep:
            out[event] = keep
    for event, entries in codex_hooks().items():
        out.setdefault(event, []).extend(entries)
    return dict(current or {}, hooks=out)


def codex_running():
    """True while any Codex executable runs (CLI, app-server, the desktop app). Matches executables, not arguments.
    AGENT_CORE_CODEX_RUNNING=0 or 1 overrides the check."""
    forced = os.environ.get('AGENT_CORE_CODEX_RUNNING')
    if forced in ('0', '1'):
        return forced == '1'
    try:
        out = subprocess.run(['ps', '-axo', 'comm='], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return True
    return bool(CODEX_PROC.search(out))


def memories_pinned(text):
    if tomllib is not None:
        try:
            return (tomllib.loads(text).get('features') or {}).get('memories') is False
        except ValueError:
            return False
    table = None
    for line in text.split('\n'):
        head = re.match(r'^\s*\[([^\[\]]+)\]\s*(#.*)?$', line)
        if head:
            table = head.group(1).strip()
        elif table == 'features' and re.match(r'^\s*memories\s*=\s*false\s*(#.*)?$', line):
            return True
        elif table is None and re.match(r'^\s*features\.memories\s*=\s*false\s*(#.*)?$', line):
            return True
    return False


def pin_memories(text):
    """config.toml with features.memories = false, every other byte kept; None if that can't be done safely.
    Without tomllib the file cannot be checked after the edit, so the edit is made only when the text is simple: no
    line mentions `features` except exactly one plain [features] header, or none does."""
    lines = text.split('\n')
    headers = [i for i, l in enumerate(lines) if re.match(r'^\s*\[features\]\s*(#.*)?$', l)]
    if tomllib is None:
        mentions = [i for i, l in enumerate(lines) if 'features' in l]
        if mentions and mentions != headers[:1]:
            return None
    head = headers[0] if headers else None
    if head is None:
        out = text.rstrip('\n') + ('\n\n' if text.strip() else '') + '[features]\nmemories = false\n'
    else:
        end = next((i for i in range(head + 1, len(lines)) if re.match(r'^\s*\[', lines[i])), len(lines))
        key = next((i for i in range(head + 1, end) if re.match(r'^\s*memories\s*=', lines[i])), None)
        if key is None:
            lines.insert(head + 1, 'memories = false')
        else:
            lines[key] = 'memories = false'
        out = '\n'.join(lines)
    if tomllib is not None:
        try:
            tomllib.loads(out)
        except ValueError:
            return None
    return out if memories_pinned(out) else None


def features_table(lines, value):
    """(header line, end of the table, line of its memories key) for the plain [features] table in lines; the key line
    must match value, a regex for what follows `memories =`. Each is None when it is not there."""
    head = next((i for i, l in enumerate(lines) if re.match(r'^\s*\[features\]\s*(#.*)?$', l)), None)
    if head is None:
        return None, None, None
    end = next((i for i in range(head + 1, len(lines)) if re.match(r'^\s*\[', lines[i])), len(lines))
    return head, end, next((i for i in range(head + 1, end) if re.match(r'^\s*memories\s*=' + value, lines[i])), None)


def unpin_memories(text, original):
    """config.toml without the kit's features.memories = false, everything else kept: the memories line your original
    had goes back; with none, the line goes, and a [features] table the kit added goes with it when nothing else is in
    it. None when the pin is not there to take out, or the result would not parse."""
    lines, was = text.split('\n'), original.split('\n')
    head, end, key = features_table(lines, r'\s*false\s*(#.*)?$')
    if key is None:
        return None
    was_head, _, was_key = features_table(was, '')
    if was_key is not None:
        lines[key] = was[was_key]
    else:
        del lines[key]
        if was_head is None and not any(l.strip() for l in lines[head + 1:end - 1]):
            del lines[head:end - 1]
            while lines and not lines[-1].strip():
                lines.pop()
            lines.append('')
    out = '\n'.join(lines)
    if tomllib is not None:
        try:
            tomllib.loads(out)
        except ValueError:
            return None
    return None if memories_pinned(out) else out


def note_created(path):
    if path not in state()['createdFiles']:
        state()['createdFiles'].append(path)


def install_codex():
    """The rulebook always (Codex reads it at session start); everything else in ~/.codex only while no Codex runs."""
    if not os.path.isdir(CODEX_DIR):
        print('codex: %s not found, so Codex files were skipped. Start Codex once, then run: %s'
              % (common.tilde(CODEX_DIR), sync_cmd()))
        return 0
    build_instructions()
    build_codex()
    everything = codex_agent_pairs()
    owned, alone = agent_split(everything, codex_agents_dir())
    report_alone(alone, codex_agents_dir())
    put(os.path.join(BUILD, 'codex', 'AGENTS.md'), os.path.join(CODEX_DIR, 'AGENTS.md'), exe=False, original=True)
    pending = [(src, dst) for src, dst in codex_pairs() if not (os.path.exists(dst) and digest(src) == digest(dst))]
    hooks_path = os.path.join(CODEX_DIR, 'hooks.json')
    hooks_now = load_json(hooks_path)
    hooks_new = merged_hooks(hooks_now)
    if hooks_new != hooks_now:
        pending.append((None, hooks_path))
    config_path = os.path.join(CODEX_DIR, 'config.toml')
    config_now = common.read(config_path) if os.path.isfile(config_path) else ''
    pin = bool((common.config().get('codex') or {}).get('pin_memories_off'))
    if pin and not memories_pinned(config_now):
        pending.append(('pin', config_path))
    if pending and codex_running():
        print('codex: %d file(s) wait until no Codex process runs (the desktop app counts); then run: %s\n       %s'
              % (len(pending), sync_cmd(), ', '.join(common.tilde(d) for _, d in pending)))
        save_state()
        return 0
    for src, dst in pending:
        if src == 'pin':
            pinned = pin_memories(config_now)
            if pinned is None:
                print('codex: did not pin features.memories = false in %s: the file is too unusual to edit safely '
                      '(a features entry other than one plain [features] table, or TOML that would not parse). '
                      'To pin it by hand, put the line  memories = false  under the [features] table.'
                      % common.tilde(dst))
                continue
            put_bytes(pinned.encode('utf-8'), dst, mine_file=False, mode=0o600, original=True)
            print('codex: pinned features.memories = false in %s' % common.tilde(dst))
        elif src is None:
            if hooks_now is None:
                note_created(dst)
            put_bytes((json.dumps(hooks_new, indent=2) + '\n').encode('utf-8'), dst, mine_file=False, mode=0o644)
            print('codex: installed %s. Trust the new hooks once: run codex, type /hooks, review, trust.' % common.tilde(dst))
        else:
            put(src, dst, exe=False)
            print('codex: installed %s' % common.tilde(dst))
    write_agent_manifest(codex_agents_dir(), everything, owned)
    save_state()
    return 0


def plugin_install_path():
    entries = ((load_json_safe(os.path.join(PLUGINS_DIR, 'installed_plugins.json')) or {}).get('plugins') or {}).get(PLUGIN)
    return (entries or [{}])[0].get('installPath')


def plugin_problems():
    """The installed copy matches vendor/, the marketplace never updates itself, the stop review gate is off."""
    out = []
    market = (load_json_safe(os.path.join(PLUGINS_DIR, 'known_marketplaces.json')) or {}).get(PLUGIN_MARKET)
    if not market:
        out.append('plugin: marketplace %s is not added' % PLUGIN_MARKET)
    elif market.get('autoUpdate') is not False:
        out.append('plugin: marketplace %s has auto-update on' % PLUGIN_MARKET)
    installed, src = plugin_install_path(), os.path.join(PLUGIN_SRC, 'plugins', 'codex')
    if not installed or not os.path.isdir(installed):
        out.append('plugin: %s is not installed' % PLUGIN)
    else:
        for root, _, files in os.walk(src):
            for f in files:
                a = os.path.join(root, f)
                b = os.path.join(installed, os.path.relpath(a, src))
                if not os.path.exists(b) or digest(a) != digest(b):
                    out.append('plugin: installed copy differs from vendor/ at ' + os.path.relpath(a, src))
                    break
    for st in glob.glob(os.path.join(PLUGINS_DIR, 'data', 'codex-*', 'state', '*', 'state.json')):
        if ((load_json_safe(st) or {}).get('config') or {}).get('stopReviewGate'):
            out.append('plugin: stop review gate is on in ' + st)
    return out


def install_plugin():
    """Marketplace from the vendored copy, plugin at user scope, auto-update off. A changed vendor copy is
    reinstalled, since the version number stays upstream's."""
    claude = shutil.which('claude')
    if not claude or not os.path.isdir(PLUGIN_SRC):
        print('plugin: skipped (%s)' % ('no claude CLI' if not claude else 'no vendor/codex-plugin-cc'))
        return 0
    known = os.path.join(PLUGINS_DIR, 'known_marketplaces.json')

    def cli(*args):
        out = subprocess.run([claude, 'plugin'] + list(args), capture_output=True, text=True, timeout=120)
        if out.returncode:
            print('plugin: claude plugin %s failed: %s' % (' '.join(args), (out.stderr or out.stdout).strip()[:300]))
        return out.returncode == 0

    if PLUGIN_MARKET not in (load_json_safe(known) or {}):
        cli('marketplace', 'add', PLUGIN_SRC, '--scope', 'user')
    elif any(p.startswith('plugin: installed copy differs') for p in plugin_problems()):
        cli('marketplace', 'update', PLUGIN_MARKET)
        cli('uninstall', PLUGIN, '--scope', 'user')
    if not plugin_install_path():
        cli('install', PLUGIN, '--scope', 'user')
    data = load_json_safe(known) or {}
    if PLUGIN_MARKET in data and data[PLUGIN_MARKET].get('autoUpdate') is not False:
        data[PLUGIN_MARKET]['autoUpdate'] = False
        write_bytes(known, (json.dumps(data, indent=2) + '\n').encode('utf-8'), mode=file_mode(known, 0o644))
    for p in plugin_problems():
        print(p)
    return 0


# ---------------------------------------------------------------- settings

def merge_rules(theirs, wanted, recorded):
    """(rules to write, rules the kit now owns). Rules the kit added earlier and no longer wants are removed; rules
    that were already yours are never recorded as the kit's."""
    stale = set(recorded) - set(wanted)
    base = dedupe([r for r in (theirs or []) if r not in stale])
    added = [r for r in wanted if r not in base]
    return base + added, [r for r in wanted if r in recorded or r in added]


HOOK_FILE = re.compile(r'(?:~|\$\{?HOME\}?)?[^\s\'"]*\.claude/hooks/[^\s\'"]+')


def hook_script_archived(entry):
    """True when every hook command in the entry runs a script from ~/.claude/hooks/ that --clean-slate moved into this
    run's backup. A path that cannot be resolved here ($CLAUDE_PROJECT_DIR, another variable, a relative path), a script
    outside ~/.claude/hooks/, or a script that is simply missing is not archived, so the entry stays."""
    cmds = [h.get('command', '') for h in (entry.get('hooks') or []) if isinstance(h, dict)] if isinstance(entry, dict) else []
    found = [m for c in cmds for m in HOOK_FILE.findall(c)]
    if not found or len(found) < len(cmds):
        return False
    root = os.path.join(CLAUDE_DIR, 'hooks')
    for raw in found:
        path = re.sub(r'^(~|\$\{HOME\}|\$HOME)(?=/)', HOME, raw)
        if not os.path.isabs(path) or '$' in path or '`' in path:
            return False
        path = os.path.normpath(path)
        if not path.startswith(root + os.sep) or os.path.lexists(path):
            return False
        if not os.path.lexists(os.path.join(BACKUP.dir(), 'claude', 'hooks', os.path.relpath(path, root))):
            return False
    return True


def need_object(data, key, where):
    if key in data and not isinstance(data[key], dict):
        raise ValueError('%s: "%s" must be a JSON object. Fix it, then rerun the installer.' % (where, key))


def adopt_legacy_auto_env(st, live):
    """An install from before the autoMode.environment lines were recorded one by one kept {had, previous, set} for the
    whole list. Its `set` lines minus `previous` (yours) become the kit's recorded lines, and the old record goes.
    Returns your `previous` lines, which that install replaced, while the live list (live) is still exactly what it
    wrote; a list you edited since is left as it is."""
    old = st['changes'].get('autoMode.environment')
    if old is None or 'autoModeEnvironment' in st['settings']:
        return []
    mine_before = old.get('previous') if old.get('had') and isinstance(old.get('previous'), list) else []
    st['settings']['autoModeEnvironment'] = [l for l in old.get('set') or [] if l not in mine_before]
    del st['changes']['autoMode.environment']
    if not old.get('had') and 'autoMode.environment' not in st['created']:
        st['created'].append('autoMode.environment')  # the kit made the list; it goes when it ends up empty
    return mine_before if live == old.get('set') else []


def merge_settings(mode):
    """fresh = first install, clean = first install after --clean-slate, keep = sync. All of them keep your own
    permission rules, hooks and other settings, and record what the kit sets so --uninstall can put it back."""
    ours = load_json(os.path.join(BUILD, 'claude', 'settings.json'))
    if ours is None:
        ours = build_settings()
    path = os.path.join(CLAUDE_DIR, 'settings.json')
    cur = load_json(path)
    existed = cur is not None
    cur = cur or {}
    if not isinstance(cur, dict):
        raise ValueError('%s is not a JSON object' % common.tilde(path))
    where = common.tilde(path)
    for key in ('env', 'permissions', 'autoMode'):
        need_object(cur, key, where)
    if 'hooks' in cur and not isinstance(cur['hooks'], dict):
        raise ValueError('%s: "hooks" must be a JSON object. Fix it, then rerun the installer.' % where)
    cfg = common.config()
    st = state()
    changes, created = st['changes'], st['created']
    if not existed:
        note_created(path)

    def set_key(label, had, previous, new):
        """Record that the kit set a key. A value that is the kit's own earlier write keeps the original on record."""
        rec = changes.get(label)
        if rec and had and previous == rec.get('set'):
            rec['set'] = new
        else:
            changes[label] = {'had': had, 'previous': previous if had else None, 'set': new}

    def made(label, present):
        if not present and label not in created:
            created.append(label)

    out = dict(cur)
    if '$schema' in ours and '$schema' not in out:
        out['$schema'] = ours['$schema']
        set_key('$schema', False, None, ours['$schema'])
    if not cur.get('model') and ours.get('model'):
        out['model'] = ours['model']
        set_key('model', 'model' in cur, cur.get('model'), ours['model'])
    made('env', 'env' in cur)
    env = dict(cur.get('env') or {})
    for key, value in (ours.get('env') or {}).items():
        if key not in env:
            env[key] = value
            set_key('env.' + key, False, None, value)
    out['env'] = env
    theirs, wanted = cur.get('permissions') or {}, ours.get('permissions') or {}
    made('permissions', 'permissions' in cur)
    replaced = adopt_legacy_auto_env(st, (cur.get('autoMode') or {}).get('environment'))
    recorded = st['settings']
    own_rules = dict((k, len([r for r in theirs.get(k) or [] if r not in (recorded.get(k) or [])])) for k in ('allow', 'deny'))
    perms, owned = dict(theirs), {}
    for key in ('allow', 'deny'):
        made('permissions.' + key, key in theirs)
        perms[key], owned[key] = merge_rules(theirs.get(key), wanted.get(key) or [], recorded.get(key) or [])
    dropped_ask = []
    if str(cfg.get('permission_mode') or '').strip() == 'bypassPermissions' and theirs.get('ask'):
        dropped_ask, perms['ask'] = list(theirs['ask']), []  # ask rules prompt even in bypass mode
        st['droppedAsk'] = dedupe(st['droppedAsk'] + dropped_ask)
    if wanted.get('defaultMode') and theirs.get('defaultMode') != wanted['defaultMode']:
        perms['defaultMode'] = wanted['defaultMode']
        set_key('permissions.defaultMode', 'defaultMode' in theirs, theirs.get('defaultMode'), wanted['defaultMode'])
    out['permissions'] = perms
    made('hooks', 'hooks' in cur)
    hooks, kept, dropped = {}, 0, []
    for event, entries in (cur.get('hooks') or {}).items():
        for entry in strip_ours(entries):
            if mode == 'clean' and hook_script_archived(entry):
                dropped.append(entry)
                if {'event': event, 'entry': entry} not in st['droppedHooks']:
                    st['droppedHooks'].append({'event': event, 'entry': entry})
                continue
            hooks.setdefault(event, []).append(entry)
            kept += 1
    for event, entries in (ours.get('hooks') or {}).items():
        hooks.setdefault(event, []).extend(json.loads(json.dumps(entries)))
    out['hooks'] = hooks
    if ours.get('autoMode'):
        made('autoMode', 'autoMode' in cur)
        auto = dict(cur.get('autoMode') or {})
        made('autoMode.environment', 'environment' in auto)
        auto['environment'], owned['autoModeEnvironment'] = merge_rules(
            dedupe(replaced + list(auto.get('environment') or [])), ours['autoMode']['environment'],
            recorded.get('autoModeEnvironment') or [])
        out['autoMode'] = auto
    elif recorded.get('autoModeEnvironment') and isinstance(cur.get('autoMode'), dict):
        auto = dict(cur['autoMode'])  # config dropped the lines: take the kit's out, as for allow and deny rules
        if isinstance(auto.get('environment'), list):
            auto['environment'] = [l for l in auto['environment'] if l not in set(recorded['autoModeEnvironment'])]
            if not auto['environment'] and 'autoMode.environment' in created:
                del auto['environment']
                created.remove('autoMode.environment')
        if auto or 'autoMode' not in created:
            out['autoMode'] = auto
        else:
            del out['autoMode']
            created.remove('autoMode')
    floor = ours.get('cleanupPeriodDays')
    have = cur.get('cleanupPeriodDays')
    if floor and not (isinstance(have, (int, float)) and not isinstance(have, bool) and have >= floor):
        out['cleanupPeriodDays'] = floor  # the learning loop reads old transcripts; a higher value of yours stays
        set_key('cleanupPeriodDays', 'cleanupPeriodDays' in cur, have, floor)
    for key, value in ours.items():
        if key not in out and key != 'switchModelsOnFlag':
            out[key] = value
            set_key(key, False, None, value)
    changed = put_bytes((json.dumps(out, indent=2, ensure_ascii=False) + '\n').encode('utf-8'), path, mine_file=False,
                        mode=0o600)
    st['settings'] = owned
    save_state()
    print('settings: %s. model %s, permission mode %s.'
          % ('updated' if changed != 'same' else 'already up to date', out.get('model'), perms.get('defaultMode') or 'unchanged'))
    print('          kept your %d allow, %d ask and %d deny rules and %d of your hooks.'
          % (own_rules['allow'], len(perms.get('ask') or []), own_rules['deny'], kept))
    if dropped:
        cmds = [h.get('command', '') for e in dropped for h in e.get('hooks') or [] if isinstance(h, dict)]
        print('          dropped %d of your hooks because --clean-slate moved their scripts into the backup '
              '(~/.claude/hooks): %s' % (len(dropped), '; '.join(cmds)))
    if dropped_ask:
        print('          dropped %d ask rules because permission_mode is bypassPermissions (they would prompt anyway; '
              'the backup has them, and --uninstall puts them back): %s' % (len(dropped_ask), ', '.join(dropped_ask)))
    other = sorted(k for k in cur if k not in ('$schema', 'model', 'env', 'permissions', 'hooks', 'autoMode', 'cleanupPeriodDays'))
    if other:
        print('          everything else unchanged: ' + ', '.join(other))


# ---------------------------------------------------------------- check

def check():
    problems = []
    try:
        build(quiet=True)
    except OSError as exc:
        print(exc)
        return 1
    for src, dst in managed_pairs():
        if not os.path.exists(dst):
            problems.append('missing  ' + dst)
        elif digest(src) != digest(dst):
            problems.append('differs  ' + dst)
    ours = load_json(os.path.join(BUILD, 'claude', 'settings.json')) or {}
    cur = load_json_safe(os.path.join(CLAUDE_DIR, 'settings.json')) or {}
    if cur.get('model') != ours.get('model'):
        print('note: your settings use model %r (kit default %r); that is your choice, not drift'
              % (cur.get('model'), ours.get('model')))
    live_hooks = cur.get('hooks') or {}
    for event, entries in (ours.get('hooks') or {}).items():
        have = [h.get('command') for e in live_hooks.get(event) or [] for h in (e.get('hooks') or []) if isinstance(h, dict)]
        for e in entries:
            if any(h.get('command') not in have for h in e.get('hooks') or []):
                problems.append('settings: agent-core %s hook is not registered' % event)
                break
    for event, entries in live_hooks.items():
        if event not in (ours.get('hooks') or {}) and any(mine(e) for e in entries):
            problems.append('settings: an agent-core %s hook is registered but config says it should not be '
                            '(stop_hook is false)' % event)
    for key in ('allow', 'deny'):
        missing = [r for r in (ours.get('permissions') or {}).get(key, []) if r not in ((cur.get('permissions') or {}).get(key) or [])]
        if missing:
            problems.append('settings: %d %s rule(s) missing, e.g. %s' % (len(missing), key, missing[0]))
    want_mode = (ours.get('permissions') or {}).get('defaultMode')
    if want_mode and (cur.get('permissions') or {}).get('defaultMode') != want_mode:
        problems.append('settings: defaultMode is not %s (config permission_mode)' % want_mode)
    if want_mode == 'bypassPermissions' and (cur.get('permissions') or {}).get('ask'):
        problems.append('settings: ask rules are set, which prompt even in bypassPermissions')
    if ours.get('autoMode') and any(l not in ((cur.get('autoMode') or {}).get('environment') or [])
                                    for l in ours['autoMode']['environment']):
        problems.append('settings: autoMode.environment differs from auto_mode_environment in config')
    if os.path.isdir(CODEX_DIR):
        live = (load_json_safe(os.path.join(CODEX_DIR, 'hooks.json')) or {}).get('hooks') or {}
        for event, entries in codex_hooks().items():
            if any(e not in (live.get(event) or []) for e in entries):
                problems.append('codex: agent-core %s hook missing from ~/.codex/hooks.json' % event)
        if (common.config().get('codex') or {}).get('pin_memories_off'):
            config = os.path.join(CODEX_DIR, 'config.toml')
            if not memories_pinned(common.read(config) if os.path.isfile(config) else ''):
                problems.append('codex: features.memories is not pinned to false in ~/.codex/config.toml')
    if os.path.isdir(PLUGIN_SRC) and shutil.which('claude'):
        problems += plugin_problems()
    for p in problems:
        print(p)
    print('drift: %d item(s). Fix with: %s' % (len(problems), sync_cmd()) if problems else 'in sync')
    return 1 if problems else 0


def validate():
    bad = 0
    for path in (os.path.join(CLAUDE_DIR, 'settings.json'), os.path.join(CODEX_DIR, 'hooks.json'), common.CONFIG_PATH):
        try:
            data = load_json(path)
        except ValueError as exc:
            print('%s is not valid JSON (%s). Fix it, then rerun the installer.' % (common.tilde(path), exc))
            bad = 1
            continue
        except OSError as exc:
            print('%s cannot be read (%s).' % (common.tilde(path), exc))
            bad = 1
            continue
        if data is not None and not isinstance(data, dict):
            print('%s must hold a JSON object. Fix it, then rerun the installer.' % common.tilde(path))
            bad = 1
    return bad


# ---------------------------------------------------------------- snapshot, clean slate, uninstall, restore

def snapshot():
    """First install: copy every file the kit may change into the backup before anything is touched. A symlink is saved
    by content and noted as a link."""
    got = 0
    codex = ['AGENTS.md', 'hooks.json']
    config = os.path.join(CODEX_DIR, 'config.toml')
    if (common.config().get('codex') or {}).get('pin_memories_off') and not memories_pinned(
            common.read(config) if os.path.isfile(config) else ''):
        codex.append('config.toml')  # only a file the kit will change: --restore must not revert one it never touched
    for base, names in ((CLAUDE_DIR, ('CLAUDE.md', 'settings.json')), (CODEX_DIR, codex)):
        for name in names:
            got += bool(BACKUP.save(os.path.join(base, name), quiet=True))
    print('       saved to %s' % common.tilde(BACKUP.dir()) if got else '       nothing to back up')
    return 0


def clean_slate():
    """The old behaviour, opt-in: move the whole of ~/.claude/{agents,skills,commands,rules,hooks} into the backup. The
    first move of each folder is recorded, so --uninstall can put your files back."""
    for d in ARCHIVE_DIRS:
        path = os.path.join(CLAUDE_DIR, d)
        if os.path.lexists(path):  # a symlink (dotfiles) is moved as the link, never followed
            BACKUP.archive(path)
            state()['archived'].setdefault(d, local_ref(BACKUP.dir()))
            save_state()  # after each folder, so one that fails later does not lose the record of those already moved
            print('archived ~/.claude/%s (moved into the backup, nothing deleted)' % d)
    BACKUP.report()
    return 0


def generated_by_kit(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return '<!-- Generated by' in fh.read(400)
    except OSError:
        return False


def get_at(data, label):
    """(the dict holding the key, the key) for a label such as 'model', 'env.MCP_TIMEOUT' or 'permissions.defaultMode'."""
    parent, dot, key = label.partition('.')
    if not dot:
        return data, label
    box = data.get(parent)
    return (box if isinstance(box, dict) else None), key


def uninstall_settings(bk, restored, moved):
    """settings.json: the kit's hooks and rules come out, and every key the kit set goes back to what it was (unless you
    changed it since); the ask rules dropped for bypassPermissions come back."""
    st = state()
    path = os.path.join(CLAUDE_DIR, 'settings.json')
    cur = load_json(path)
    if not isinstance(cur, dict):
        return
    out = json.loads(json.dumps(cur))
    hooks = {}
    for event, entries in (cur.get('hooks') or {}).items():
        keep = strip_ours(entries)
        if keep:
            hooks[event] = keep
    for rec in st['droppedHooks']:  # entries --clean-slate dropped; their scripts are put back with the archived folder
        if rec['entry'] not in hooks.get(rec['event'], []):
            hooks.setdefault(rec['event'], []).append(rec['entry'])
            restored.append('~/.claude/settings.json: your hook entry %s' % json.dumps(rec['entry']))
    if hooks or 'hooks' in cur:
        out['hooks'] = hooks
    perms = out.get('permissions') if isinstance(out.get('permissions'), dict) else {}
    for key in ('allow', 'deny'):
        if key in perms:
            perms[key] = [r for r in perms[key] if r not in set(st['settings'].get(key) or [])]
    auto = out.get('autoMode') if isinstance(out.get('autoMode'), dict) else {}
    replaced = adopt_legacy_auto_env(st, auto.get('environment'))
    if isinstance(auto.get('environment'), list):  # only the lines the kit added
        kit = set(st['settings'].get('autoModeEnvironment') or [])
        auto['environment'] = [l for l in replaced if l not in auto['environment']] + \
            [l for l in auto['environment'] if l not in kit]  # yours that the first release replaced come back
    reverted, changed_since = [], []
    for label, rec in sorted(st['changes'].items()):
        box, key = get_at(out, label)
        if box is None or key not in box:
            continue
        if box[key] != rec.get('set'):
            changed_since.append(label)
        elif rec.get('had'):
            box[key] = rec.get('previous')
            reverted.append('%s back to %s' % (label, json.dumps(rec.get('previous'))))
        else:
            del box[key]
            reverted.append('%s removed' % label)
    back = [r for r in st['droppedAsk'] if r not in (perms.get('ask') or [])]
    if back:
        perms['ask'] = (perms.get('ask') or []) + back
        out['permissions'] = perms
        restored.append('~/.claude/settings.json: %d ask rule(s) install dropped for bypassPermissions put back: %s'
                        % (len(back), ', '.join(back)))
    for label in sorted(st['created'], key=lambda l: -l.count('.')):  # containers the kit made, now empty
        box, key = get_at(out, label)
        if box is not None and key in box and box[key] in ({}, []):
            del box[key]
    if reverted:
        restored.append('~/.claude/settings.json: ' + '; '.join(reverted))
    if changed_since:
        print('kept %s in ~/.claude/settings.json: you changed %s since the install'
              % (', '.join(changed_since), 'it' if len(changed_since) == 1 else 'them'))
    if out == cur:
        return
    if path in st['createdFiles'] and not out:
        bk.move(path)
        moved.append('~/.claude/settings.json (the kit created it and nothing of yours was in it)')
        return
    put_bytes((json.dumps(out, indent=2, ensure_ascii=False) + '\n').encode('utf-8'), path, mine_file=False, mode=0o600)
    moved.append('~/.claude/settings.json: kit hooks and the rules it added taken out')


def uninstall_codex_hooks(bk, moved):
    cpath = os.path.join(CODEX_DIR, 'hooks.json')
    hooks_now = load_json_safe(cpath)
    if not (isinstance(hooks_now, dict) and mine(hooks_now)):
        return
    out = dict(hooks_now)
    out['hooks'] = dict((e, k) for e, k in ((e, strip_ours(v)) for e, v in (hooks_now.get('hooks') or {}).items()) if k)
    if cpath in state()['createdFiles'] and not out['hooks'] and set(out) == {'hooks'}:
        bk.move(cpath)
        moved.append('~/.codex/hooks.json (the kit created it and nothing of yours was in it)')
        return
    put_bytes((json.dumps(out, indent=2) + '\n').encode('utf-8'), cpath, mine_file=False, mode=0o644)
    moved.append('~/.codex/hooks.json: kit hooks taken out')


def first_backup_copy(shown):
    """Your original of an instruction file from the earliest backup that has one, for installs that recorded none."""
    root = os.path.join(common.LOCAL, 'backups')
    rel = Backup.rel(shown)
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if name.endswith(('-uninstall', '-before-restore')):
            continue
        p = os.path.join(root, name, rel)
        if os.path.isfile(p) and not generated_by_kit(p):
            return p
    return None


def original_copy(shown):
    """(path of your original, recorded) for a file the kit took over; the path is None when there was none to keep."""
    rec = state()['originals'].get(shown)
    if rec:
        p = os.path.join(common.LOCAL, rec['backup']) if rec.get('backup') else None
        return (p if p and os.path.isfile(p) else None), True
    return None, False


def put_original(shown, orig, bk):
    """Write your original back into shown (through a symlink, which stays), after the kit's version went into bk."""
    real = os.path.realpath(shown) if os.path.islink(shown) else shown
    bk.save(real, shown, quiet=True)
    with open(orig, 'rb') as fh:
        data = fh.read()
    write_bytes(real, data, mode=file_mode(orig, 0o644))


def uninstall_instruction_files(bk, restored, targets):
    for f in (os.path.join(CLAUDE_DIR, 'CLAUDE.md'), os.path.join(CODEX_DIR, 'AGENTS.md')):
        if not os.path.isfile(f):
            continue
        if not generated_by_kit(f):
            print('left %s alone: it is not the generated file (edited or replaced since)' % common.tilde(f))
            continue
        orig, recorded = original_copy(f)
        if not recorded:
            orig = first_backup_copy(f)
        elif orig is None and state()['originals'][f].get('backup'):
            print('note: your original %s was recorded in %s but that copy is gone; the generated file moves into the backup'
                  % (common.tilde(f), state()['originals'][f]['backup']))
        written = state()['files'].get(os.path.realpath(f) if os.path.islink(f) else f)
        if written and written != digest(f):  # a link with no original of yours has its target moved, so that is where it lands
            lands = Backup.rel(os.path.realpath(f)) if os.path.islink(f) and not orig else Backup.rel(f)
            print('note: you edited the generated %s; your edits are kept in %s'
                  % (common.tilde(f), common.tilde(os.path.join(bk.dir(), lands))))
        if orig:
            put_original(f, orig, bk)
            how = '; it is a symlink to %s and was written through the link' % common.tilde(os.path.realpath(f)) \
                if os.path.islink(f) else ''
            restored.append('%s: your own file, from %s%s' % (common.tilde(f), common.tilde(orig), how))
        elif os.path.islink(f):
            real = os.path.realpath(f)
            bk.move(real)  # the link stays; it points at nothing again, as before the install
            restored.append('%s: no file of yours was there before; the generated content moved into the backup and the '
                            'symlink to %s stays' % (common.tilde(f), common.tilde(real)))
        else:
            targets.append(f)
    for shown, rec in sorted(state()['originals'].items()):  # config.toml, when the kit pinned memories in it
        if shown in (os.path.join(CLAUDE_DIR, 'CLAUDE.md'), os.path.join(CODEX_DIR, 'AGENTS.md')):
            continue
        real = os.path.realpath(shown)
        if not os.path.isfile(real):
            continue
        orig, _ = original_copy(shown)
        if rec.get('backup') and not orig:
            continue
        have, before = common.read(real), common.read(orig) if orig else ''
        if have == pin_memories(before):  # exactly your original plus the pin: it goes back byte for byte
            if orig:
                put_original(shown, orig, bk)
                restored.append('%s: your own file, from %s' % (common.tilde(shown), common.tilde(orig)))
            else:
                bk.move(real)
                restored.append('%s: the kit created it; moved into the backup' % common.tilde(shown))
            continue
        text = unpin_memories(have, before)  # edited since: take out only the pin
        if text is None:
            print('left %s alone: it changed since the kit edited it and the memories pin is not in it' % common.tilde(shown))
        elif not orig and not text.strip():
            bk.move(real)
            restored.append('%s: the kit created it; moved into the backup' % common.tilde(shown))
        else:
            put_bytes(text.encode('utf-8'), shown, mine_file=False)
            restored.append('%s: features.memories pin taken out, the rest of the file kept' % common.tilde(shown))


def merge_back(src, dst, skip=()):
    """Copy what is in src and not in dst into dst, never over anything. Returns the number of items copied."""
    n = 0
    for name in sorted(os.listdir(src)):
        if name in skip:
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.lexists(d):
            if os.path.isdir(s) and not os.path.islink(s) and os.path.isdir(d) and not os.path.islink(d):
                n += merge_back(s, d)
            continue
        os.makedirs(dst, exist_ok=True)
        if os.path.islink(s):
            os.symlink(os.readlink(s), d)
        elif os.path.isdir(s):
            shutil.copytree(s, d, symlinks=True)
        else:
            shutil.copy2(s, d)
        n += 1
    return n


def put_link_back(saved, dst, restored):
    """Make the symlink that --clean-slate moved to saved again at dst, with the same text. The empty folder the kit
    made in its place is replaced; a folder holding anything else stays, and the link is left in the backup."""
    text = os.readlink(saved)
    if os.path.islink(dst) and os.readlink(dst) == text:
        return
    if os.path.isdir(dst) and not os.path.islink(dst):
        try:
            os.rmdir(dst)
        except OSError:
            print('left %s alone: it holds items of its own now. Your symlink to %s is still in %s'
                  % (common.tilde(dst), text, common.tilde(saved)))
            return
    if os.path.lexists(dst):
        print('left %s alone: something is there. Your symlink to %s is still in %s' % (common.tilde(dst), text, common.tilde(saved)))
        return
    os.symlink(text, dst)
    restored.append('%s: your symlink to %s, as it was' % (common.tilde(dst), text))


def restore_archived(restored):
    """--clean-slate moved your agents, skills, commands, rules and hooks aside; put back what the kit did not install."""
    for d, ref in sorted(state()['archived'].items()):
        folder = os.path.join(common.LOCAL, ref, 'claude', d)
        if os.path.islink(folder):  # a symlink was moved aside: make the same link again
            put_link_back(folder, os.path.join(CLAUDE_DIR, d), restored)
            continue
        if not os.path.isdir(folder):
            continue
        skip = set(read_manifest(folder)) | {MANIFEST} if d in ('agents', 'skills') else ({'agent-core'} if d == 'hooks' else set())
        n = merge_back(folder, os.path.join(CLAUDE_DIR, d), skip)
        if n:
            restored.append('~/.claude/%s: %d item(s) of yours that --clean-slate had moved aside, from %s'
                            % (d, n, common.tilde(folder)))


def agent_targets(dest, names):
    """The agent files in dest the kit manages: its manifest, else (an install from before manifests) the files the
    install state says it wrote."""
    listed = read_manifest(dest)
    if not listed:
        listed = [n for n in names if os.path.join(dest, n) in state()['files']]
    return [os.path.join(dest, n) for n in listed] + [manifest_path(dest)]


def uninstall():
    bk = Backup(unique(os.path.join(common.LOCAL, 'backups', time.strftime('%Y%m%d-%H%M%S') + '-uninstall')))
    global BACKUP
    BACKUP = bk
    moved, restored, targets = [], [], []
    uninstall_settings(bk, restored, moved)
    uninstall_codex_hooks(bk, moved)
    uninstall_instruction_files(bk, restored, targets)
    targets += agent_targets(claude_agents_dir(), [os.path.basename(p) for p in sorted(glob.glob(os.path.join(CORE, 'claude', 'agents', '*.md')))])
    targets += agent_targets(codex_agents_dir(), [name + '.toml' for name, _ in CODEX_ROLES.values()])
    targets.append(HOOKS_DST)
    for dest in SKILL_DIRS:
        targets += [os.path.join(dest, n) for n in read_manifest(dest)]
        targets.append(manifest_path(dest))
    targets.append(os.path.join(CODEX_DIR, 'rules', 'agent-core.rules'))
    groups, real_dirs = {}, []
    for t in targets:
        if os.path.lexists(t):
            parent = os.path.dirname(t)
            if parent not in (CLAUDE_DIR, CODEX_DIR, HOME, common.LOCAL) and os.path.isdir(parent) and not os.path.islink(parent):
                real_dirs.append(Backup.rel(parent))
            bk.move(t)
            groups.setdefault(os.path.dirname(t), []).append(os.path.basename(t))
    if real_dirs:  # so a later --restore never writes these files through a symlink that has taken the folder's place
        with open(os.path.join(bk.dir(), REAL_DIRS), 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(sorted(set(real_dirs))) + '\n')
    for parent in sorted(groups):  # folders the install made and the move emptied
        if parent not in (CLAUDE_DIR, CODEX_DIR, HOME):
            try:
                os.rmdir(parent)
            except OSError:
                pass
    for parent, names in sorted(groups.items()):
        moved.append('%s/%s' % (common.tilde(parent), names[0]) if len(names) == 1 else
                     '%s/ (%d items: %s)' % (common.tilde(parent), len(names), ', '.join(sorted(names)[:4]) +
                                            (', ...' if len(names) > 4 else '')))
    restore_archived(restored)
    for line in moved:
        print('moved: ' + line)
    for line in restored:
        print('restored: ' + line)
    if not moved and not restored:
        print('nothing of the kit was found to remove')
        return 0
    if os.path.lexists(state_path()):
        bk.move(state_path())  # the record of what the install changed has done its job; --restore brings it back
    name = os.path.basename(bk.path)
    earlier = sorted(d for d in os.listdir(os.path.join(common.LOCAL, 'backups')) if d != name) \
        if os.path.isdir(os.path.join(common.LOCAL, 'backups')) else []
    print('\nThe kit is out of both tools. Nothing was deleted: it all sits in %s' % common.tilde(bk.path))
    print('Undo this uninstall:  %s --restore %s' % (common.kit_cmd('install.sh'), name))
    if earlier:
        print('Earlier backups (what the first install replaced is already back where it was; these are only the copies): %s'
              % ', '.join(earlier))
    if shutil.which('claude') and plugin_install_path():
        print('The Codex plugin for Claude Code stays installed. Remove it with: claude plugin uninstall %s --scope user' % PLUGIN)
    return 0


def restore_target(rel):
    tag, _, rest = rel.partition(os.sep)
    base = {'claude': CLAUDE_DIR, 'codex': CODEX_DIR, 'home': HOME, 'local': common.LOCAL}.get(tag)
    return os.path.join(base, rest) if base and rest else None


def restore(name):
    root = os.path.join(common.LOCAL, 'backups')
    bk = os.path.join(root, name)
    if not os.path.isdir(bk):
        have = sorted(os.listdir(root)) if os.path.isdir(root) else []
        print('no backup named %s in %s. Available: %s' % (name, common.tilde(root), ' '.join(have) or '(none)'))
        return 1
    cur = Backup(unique(os.path.join(root, time.strftime('%Y%m%d-%H%M%S') + '-before-restore')))
    archived, real_dirs = read_lines(bk, 'ARCHIVED'), read_lines(bk, REAL_DIRS)
    links = read_links(bk)
    done, through = 0, []
    for rel in archived:  # folders archived as a whole come back as a whole
        src, dst = os.path.join(bk, rel), restore_target(rel)
        if dst and os.path.islink(src):  # a symlink that was moved aside comes back as the same link
            if not (os.path.islink(dst) and os.readlink(dst) == os.readlink(src)):
                if os.path.lexists(dst):
                    cur.move(dst)
                os.symlink(os.readlink(src), dst)
                done += 1
        elif dst and os.path.isdir(src):
            if os.path.lexists(dst):
                cur.move(dst)
            shutil.copytree(src, dst, symlinks=True)
            done += 1
    for r, dirs, files in os.walk(bk):
        for f in sorted(files):
            p = os.path.join(r, f)
            rel = os.path.relpath(p, bk)
            if rel in ('ARCHIVED', LINKS, REAL_DIRS) or rel in archived or any(rel.startswith(a + os.sep) for a in archived):
                continue
            if not os.path.exists(p):
                print('skipped %s (a link that points at nothing)' % rel)
                continue
            for d in real_dirs:  # the kit's folder stood here; a symlink there now is yours and is not written through
                at = restore_target(d)
                if at and rel.startswith(d + os.sep) and os.path.islink(at):
                    print('moved %s aside (a symlink to %s): the backup holds a real folder there' % (common.tilde(at), os.readlink(at)))
                    cur.archive(at)
            dst = restore_target(rel)
            if not dst:
                print('skipped %s (not under ~/.claude, ~/.codex, ~ or local/)' % rel)
                continue
            if rel in links and not os.path.lexists(dst) and os.path.isdir(os.path.dirname(dst)):
                os.symlink(links[rel]['target'], dst)  # it was a link when saved and is gone now: make it again
            real = os.path.realpath(dst) if os.path.islink(dst) else dst
            if os.path.isfile(real):
                if digest(real) == digest(p):
                    continue
                cur.save(real, dst, quiet=True)
            with open(p, 'rb') as fh:
                write_bytes(real, fh.read(), mode=file_mode(p, 0o644))
            if os.path.islink(dst):
                through.append(common.tilde(dst))
            done += 1
    print('Restored %d item(s) from backups/%s.' % (done, name))
    if through:
        print('Written through the symlink and left in place: %s' % ', '.join(through))
    if cur.count:
        print('What they replaced is in %s.' % common.tilde(cur.path))
    print('Files the kit added that the backup did not hold stay in place. To bring the kit back in line run: %s --sync; '
          'to remove it run: %s --uninstall' % (common.kit_cmd('install.sh'), common.kit_cmd('install.sh')))
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--merge-settings', choices=('fresh', 'clean', 'keep'))
    ap.add_argument('--snapshot', action='store_true')
    ap.add_argument('--validate', action='store_true')
    ap.add_argument('--install-claude', action='store_true')
    ap.add_argument('--install-skills', action='store_true')
    ap.add_argument('--install-codex', action='store_true')
    ap.add_argument('--install-plugin', action='store_true')
    ap.add_argument('--clean-slate', action='store_true')
    ap.add_argument('--uninstall', action='store_true')
    ap.add_argument('--restore', metavar='NAME')
    args = ap.parse_args()
    try:
        if args.validate:
            return validate()
        if args.check:
            return check()
        if args.snapshot:
            return snapshot()
        if args.restore:
            return restore(args.restore)
        if args.uninstall:
            return uninstall()
        if args.clean_slate:
            return clean_slate()
        if args.install_plugin:
            return install_plugin()
        code = 0
        if args.install_claude:
            code = install_claude()
        elif args.install_skills:
            code = install_skills()
        elif args.install_codex:
            code = install_codex()
        elif args.merge_settings:
            build_settings()
            merge_settings(args.merge_settings)
        else:
            build()
        BACKUP.report()
        return code
    except (OSError, ValueError) as exc:
        print('agent-core: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
