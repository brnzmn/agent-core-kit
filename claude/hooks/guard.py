#!/usr/bin/env python3
"""agent-core-kit PreToolUse guard for Bash and Agent calls.

Splits a Bash command into simple commands (quotes, operators, line breaks, comments, heredocs, `bash -c`, eval,
command substitutions, ( ) groups, env/sudo/xargs wrappers) and decides per command. cd and variables are tracked per
shell scope: a ( ) group, a command substitution, a pipeline stage, a background job and `bash -c` run in a child
shell, so a cd there does not move the commands after it; a cd to an unknown place makes the folder unknown.
Recursive deletes outside build folders and temp go to the trash instead: a lone `rm -r` of plain literal paths is
rewritten to the kit's bin/trash.py (dated _to_delete/, manifest, restore); anything else is denied with that
alternative. DROP TABLE/DATABASE/SCHEMA, DROP COLUMN and TRUNCATE (SQL clients, heredocs, -f files, SQL MCP tools)
are denied with the rename-to-_to_delete_ alternative, as are find -delete / -exec rm, and bulk or pattern-matched
moves out of a repo.
Deny, each with the alternative to use: staging everything (git add/stage -A/./-u/.., git commit -a), force-pushing or
deleting main, plain force pushes (use --force-with-lease), remote deletes, mirror pushes, hard resets, git clean,
discarding all changes, force-deleting branches and refs, dropping stashes, removing or pruning worktrees, history
rewrites, recursive deletes outside build folders and temp inside compound commands, moving worktrees with mv,
reading env files, writing into the protected folders listed in config protected_dirs, schema changes to the
Supabase projects listed in config protected_supabase outside the repo's scripts/deploy, applying learning-loop
proposals directly, a shell that runs a script it cannot be shown (a pipe from anything but echo/printf/cat), and
merging a PR into a gated branch (config pr_gate) before it has a Claude and a Codex review by an allowed author.
The one decision that is not a deny: spawning a subagent on a model listed in config gated_models (and the advisor
agent) returns "ask", so Claude Code prompts the user; that prompt also appears in bypass mode.
Prints a decision as JSON, or nothing to let the call through.
Its own errors never block a call; they go to learning/hook-errors.log.
"""
import fnmatch
import glob
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

START = time.monotonic()
MAX_CHARS = 200000
MAX_DEPTH = 8  # nested bash -c, eval, xargs and substitutions; deeper than this is refused rather than skipped
WRAPPERS = {'sudo', 'doas', 'env', 'time', 'nice', 'nohup', 'command', 'exec', 'builtin', 'caffeinate', 'timeout'}
WRAPPER_VALUE = {'sudo': {'-u', '-g', '-h', '-p', '-C', '-D', '-R', '-T', '-U', '-r', '-t'}, 'doas': {'-u', '-C'},
                 'env': {'-u', '-C', '-S'}, 'nice': {'-n'}, 'timeout': {'-s', '-k'}, 'caffeinate': {'-t', '-w'},
                 'exec': {'-a'}}
KEYWORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until'}  # a command can follow these on the same line
SHELLS = {'bash', 'sh', 'zsh', 'dash', 'ksh'}
READERS = {'cat', 'less', 'more', 'head', 'tail', 'bat', 'nl', 'open', 'strings', 'xxd', 'od', 'tac',
           'grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack', 'awk', 'gawk', 'mawk', 'sed', 'source', '.',
           'cut', 'sort', 'uniq', 'paste', 'column', 'fold', 'rev', 'base64', 'diff', 'cmp', 'comm', 'hexdump', 'jq', 'yq'}
PATTERN_FIRST = {'grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack', 'awk', 'gawk', 'mawk', 'sed'}  # first operand is the pattern
# short options that take a value, per search program (grep -T and ag -t take none: a wrong table drops nothing safe)
SEARCH_LETTERS = {'grep': 'efmABCdD', 'egrep': 'efmABCdD', 'fgrep': 'efmABCdD', 'rg': 'efmABCdgjrtTEM'}
SEARCH_VALUE = {'--include', '--exclude', '--exclude-dir', '--exclude-from', '--glob', '--iglob', '--type', '--type-not',
                '--max-count', '--max-depth', '--after-context', '--before-context', '--context', '--label', '--replace',
                '--directories', '--devices', '--binary-files', '--threads', '--max-columns', '--sort', '--sortr'}
PATTERN_OPTS = {'-e', '-f', '--regexp', '--file', '--expression'}
FILE_FLAGS = {'-g', '--glob', '--iglob', '--include', '-f', '--file'}  # options whose value is a file or a file glob
LAKE_WRITERS = {'tee', 'truncate', 'chmod', 'chown', 'touch', 'shred', 'unlink', 'rmdir'}
REGENERABLE = {'node_modules', 'dist', '.next', '.turbo', '.cache', 'coverage', '.pytest_cache',
               '.mypy_cache', '.ruff_cache', '__pycache__', '.parcel-cache', '.svelte-kit', '.vite', '.nuxt',
               '.output', '.venv', 'venv', '.vercel', '.wrangler', 'test-results', 'playwright-report', '.eggs'}
GENERIC = {'build', 'out', 'target', 'tmp'}  # build folders only inside a project; anywhere else they are somebody's folder
PROJECT_MARKERS = ('.git', 'package.json', 'pyproject.toml', 'setup.py', 'requirements.txt', 'Cargo.toml', 'go.mod',
                   'pom.xml', 'build.gradle', 'build.gradle.kts', 'Gemfile', 'composer.json', 'mix.exs', 'Makefile',
                   'CMakeLists.txt', 'Package.swift', 'deno.json', 'tsconfig.json')
PROJECT_HOMES = {'projects', 'project', 'code', 'dev', 'repos', 'src', 'work', 'workspace', 'github'}
# a script whose first command is rm and that has nothing running before or beside it: no heredoc, substitution, redirect,
# pipe, background job, group or eval (&& and || are fine)
PLAIN_RM = re.compile(r'^\s*rm\s(?!.*(?:[<>`({]|\$\(|(?<!&)&(?!&)|(?<!\|)\|(?!\|)|\b(?:eval|exec|source)\b))', re.S)
TEMP_ROOTS = ('/tmp', '/private/tmp', '/var/folders', '/private/var/folders')
MAIN_REFS = {'main', 'master', 'refs/heads/main', 'refs/heads/master'}
GIT_KNOWN = {'add', 'am', 'apply', 'archive', 'bisect', 'blame', 'branch', 'bundle', 'cat-file', 'check-ignore', 'checkout',
             'cherry', 'cherry-pick', 'clean', 'clone', 'commit', 'commit-tree', 'config', 'count-objects', 'credential',
             'describe', 'diff', 'diff-files', 'diff-index', 'diff-tree', 'difftool', 'fast-export', 'fast-import', 'fetch',
             'filter-branch', 'filter-repo', 'for-each-ref', 'format-patch', 'fsck', 'gc', 'grep', 'hash-object', 'help',
             'init', 'interpret-trailers', 'log', 'ls-files', 'ls-remote', 'ls-tree', 'maintenance', 'merge', 'merge-base',
             'mergetool', 'mktree', 'mv', 'name-rev', 'notes', 'pack-refs', 'prune', 'pull', 'push', 'range-diff', 'read-tree',
             'rebase', 'reflog', 'remote', 'repack', 'replace', 'request-pull', 'reset', 'restore', 'rev-list', 'rev-parse',
             'revert', 'rm', 'send-email', 'shortlog', 'show', 'show-branch', 'show-ref', 'sparse-checkout', 'stage', 'stash',
             'status', 'stripspace', 'submodule', 'subtree', 'switch', 'symbolic-ref', 'tag', 'update-index', 'update-ref',
             'var', 'verify-commit', 'verify-tag', 'version', 'whatchanged', 'worktree', 'write-tree'}
SUPABASE_SCHEMA = {('db', 'push'), ('db', 'reset'), ('migration', 'up')}
STATE = {'cwd': '', 'heredocs': [], 'expands': [], 'vars': {}, 'pipe': [], 'sep': ';', 'before': ';', 'plain': False,
         'moved': {}, 'undo': [], 'envcfg': []}  # cwd None = unknown; moved: worktree -> 'main' | 'unknown'
TRASH = common.kit_cmd('bin/trash.py')
PR_REVIEW = common.kit_cmd('bin/pr_review.py')
SQL_CLIENTS = {'psql', 'sqlite3', 'mysql', 'mariadb', 'duckdb', 'turso', 'pgcli', 'litecli', 'mycli'}
DESTRUCTIVE_SQL = re.compile(r'\b(drop\s+(table|database|schema)\b|truncate(\s+table)?\s+["`\w]|'
                             r'alter\s+table\b[^;]*?\bdrop\s+(column\b|(?!constraint|index|default|not\b)["`\w]))', re.I)
HEREDOC_MARK = 'AGENTCORE_HEREDOC_'
GLOB = re.compile(r'[*?\[]')
SQL_MCP = re.compile(r'^mcp__.*(execute_sql|apply_migration|d1_database_query|run_sql|sql_query)$')
BULK_MOVE = 5
ENV_FILE = re.compile(r'^\.env(\.[A-Za-z0-9_-]+)*$')
ENV_SAFE = re.compile(r'\.(example|sample|template|dist|defaults?)$')
ENV_SAMPLES = ('.env', '.env.local', '.env.production', '.env.development', '.env.test', '.env.staging', '.env.prod',
               '.env.dev', '.env.production.local', '.env.development.local', '.env.test.local')  # what a glob may hit
DB_WRITE = re.compile(r'\b(insert|update|delete|replace|drop|alter|create|vacuum|attach|truncate)\b|'
                      r'(^|\s)\.(restore|import|recover)\b|\bpragma\s+\w+\s*=', re.I)
DB_DUMP_TO = re.compile(r'(?:^|\s)\.(?:backup|save|clone)\s+(?:\w+\s+)?[\'"]?([^\s\'"]+)', re.I)
ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
VAR_REF = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?')
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
SEPARATORS = {';', '&&', '||', '|', '&', '|&', ';;', '(', ')', '{', '}', '!'}
SEP_PART = re.compile(r'\|&|\|\||&&|;;|[;&|()]')
PIPES = ('|', '|&')
REDIRECTS = {'>', '>>', '>|', '&>', '&>>'}
GIT_VALUE = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--config-env', '--super-prefix'}
XARGS_VALUE = {'-I', '-i', '-L', '-l', '-n', '-P', '-s', '-E', '-e', '-d', '-a', '-J', '-R'}
MKTEMP = re.compile(r'^(?:\$\(mktemp[^()`;&|<>$\n]*\)|`mktemp[^()`;&|<>$\n]*`)(?:/[^$`()\n]*)?$')
TMPDIR_REF = re.compile(r'^(?:\$TMPDIR|\$\{TMPDIR(?::-/?tmp)?\})/(.+)$')
API_MERGE = re.compile(r'(^|/)pulls/[^/\s]+/merge/?(\?.*)?$')
API_REPO = re.compile(r'^(https://api\.github\.com)?/?repos/[^/\s]+/[^/\s]+/?(\?.*)?$')
API_MAIN_REF = re.compile(r'^(https://api\.github\.com)?/?repos/[^/\s]+/[^/\s]+/git/refs/heads/(main|master)/?(\?.*)?$')
MAIN_DST = re.compile(r'(^|/)heads/(main|master)$')
GRAPHQL_MERGE = re.compile(r'\b(mergePullRequest|enablePullRequestAutoMerge)\b')
HTTP_CLIENTS = {'curl', 'wget', 'http', 'https', 'xh'}
WORKTREE = re.compile(r'(^|/)([^/]*-wt(/[^/]+)?|\.claude/worktrees(/[^/]+)?|[^/]*\.worktree)/?$')
PLAIN_WORD = (r"(?:[A-Za-z0-9._\-/~+@:,]|'[A-Za-z0-9._\-/~+@:, ]*'|\"[A-Za-z0-9._\-/~+@:, ]*\")+")
PLAIN_TAIL = re.compile(r'^(?:\s+' + PLAIN_WORD + r')+\s*$')  # what may follow `rm -rf` when it is rewritten
GH_TIMEOUT = 8  # seconds per gh call; the hook is killed at 10 and a killed hook lets the merge through
GH_BUDGET = 9.0  # seconds from start for all gh calls together


def _esc(text):
    """Escape % so a value can sit inside a %-template."""
    return str(text).replace('%', '%%')


def _listed(value):
    """A config value that should be a list of strings; a lone string counts as a one-item list."""
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, (list, tuple)) else []


def _supabase_refs():
    """protected_supabase from config as {project ref: note}."""
    raw = common.config().get('protected_supabase')
    if isinstance(raw, dict):
        return {str(k).strip(): str(v or '').strip() for k, v in raw.items() if str(k).strip()}
    return {ref: '' for ref in _listed(raw)}


def _fold(text):
    return text.lower() if sys.platform == 'darwin' else text  # macOS volumes ignore case


def _inside(path, folder):
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


class Protected:
    """protected_dirs from config. A bare name (or a relative path with a slash) protects every folder that has it as a
    path component. An absolute or ~ path protects that path only: a target is resolved against the tracked cwd and ~
    and compared by prefix."""

    def __init__(self, entries):
        names, paths = set(), set()
        for d in entries:
            d = os.path.expandvars(str(d).strip()).rstrip('/')
            if not d:
                continue
            full = os.path.expanduser(d)
            if os.path.isabs(full):
                q = os.path.normpath(full)
                paths.update({_fold(q), _fold(os.path.realpath(q))})
            else:
                names.add(os.path.normpath(d))
        self.paths = sorted(paths)
        self.tails = sorted({'/' + '/'.join(q.split('/')[-3:]) + '/' for q in self.paths})
        body = '|'.join(re.escape(n) for n in sorted(names))
        self.name_re = re.compile(r'(^|[/=:])(' + body + r')(/|$)') if names else None
        self.backup_re = re.compile(r'(^|[/=:])(' + body + r')/backups/') if names else None

    @staticmethod
    def _candidates(token):
        found = {token}
        if '=' in token:
            found.add(token.split('=', 1)[1])
        return [c for c in (re.sub(r'^file:(//)?', '', f).split('?', 1)[0] for f in found) if c]

    @staticmethod
    def _absolute(c):
        """c as an absolute path, or None when a variable, substitution or unknown folder stops us resolving it."""
        c = re.sub(r'^\$\{?HOME\}?(?=/|$)', common.HOME, c)
        c = os.path.expanduser(c)
        if '$' in c or '`' in c:
            return None
        if c.startswith('/'):
            return c
        base = STATE['cwd']
        if base is None:
            return None
        return os.path.join(base or os.getcwd(), c)

    def _tail_hit(self, c):
        """A path we cannot resolve: protected when it names the last folders of a protected path in a row."""
        parts = [x for x in c.split('/') if x and '$' not in x and '`' not in x]
        return any(t in _fold('/' + '/'.join(parts) + '/') for t in self.tails)

    def hit(self, token, ancestor=False):
        """True when token is, or sits in, a protected folder. ancestor: a folder that holds one counts too (rm -r, mv)."""
        if self.name_re and self.name_re.search(token):
            return True
        if not self.paths:
            return False
        for c in self._candidates(token):
            full = self._absolute(c)
            if full is None:
                if self._tail_hit(c):
                    return True
                continue
            if GLOB.search(full):
                pat = _fold(os.path.normpath(full)).split(os.sep)
                for q in self.paths:
                    qp = q.split(os.sep)
                    if (len(pat) >= len(qp) or ancestor) and all(
                            fnmatch.fnmatchcase(qp[i], pat[i]) for i in range(min(len(pat), len(qp)))):
                        return True
                continue
            norm = os.path.normpath(full)
            for p in {_fold(norm), _fold(os.path.realpath(norm))}:
                for q in self.paths:
                    if _inside(p, q) or (ancestor and _inside(q, p)):
                        return True
        return False

    def backup(self, token):
        """True when token is under <protected>/backups/: additive snapshots before a change are allowed."""
        if self.backup_re and self.backup_re.search(token):
            return True
        for c in self._candidates(token):
            full = self._absolute(c)
            if full and not GLOB.search(full):
                norm = _fold(os.path.normpath(full))
                if any(norm.startswith(q + os.sep + 'backups' + os.sep) for q in self.paths):
                    return True
        return False


PROTECTED = Protected(_listed(common.config().get('protected_dirs')))
PROTECTED_SUPABASE = _supabase_refs()
USER = common.user()
USER_S = USER + "'s"
PROPOSALS = common.tilde(os.path.join(common.LEARN, 'proposals')) + '/'

MSG = {
    'stage': 'Stage files by name. Other sessions may have work in this tree; git add -A, git add . and git commit -a sweep it up.',
    'force_main': 'Force-pushing or deleting main/master is blocked. Push a branch and open a PR instead.',
    'force': 'Plain force push is blocked. Use git push --force-with-lease on your own branch.',
    'delete_remote': 'Remote branch deletes are blocked. Leave the branch and list it in your final message.',
    'mirror': 'git push --mirror overwrites every ref on the remote. Push the branches you need by name.',
    'reset': 'git reset --hard is blocked. Run git stash push -u -m "backup <date>" first, then git reset --keep <ref>.',
    'clean': 'git clean is blocked. Move the untracked files into _to_delete/ and list what moved.',
    'discard': 'Discarding all changes is blocked. Run git stash push -u -m "backup <date>" instead.',
    'branch': 'Force-deleting a branch is blocked. Use git branch -d for merged branches; list unmerged ones.',
    'move_main': ('Moving main/master with git branch -f or git update-ref rewrites it silently. Commit on a branch '
                  'and open a PR instead.'),
    'branch_unknown': ('An earlier part of this command switches branch or folder, so the force push cannot be checked. '
                       'Push in its own call or name the refspec (git push origin <branch>).'),
    'stash': 'Dropping stashes is blocked. Leave them; list them in your final message.',
    'worktree': 'Removing or pruning worktrees is blocked. Use the worktree skill: git worktree move into _archive/.',
    'rewrite': 'History rewrites are blocked. Add a new commit instead.',
    'repo_delete': 'Deleting a GitHub repo is blocked; ' + USER + ' deletes repos. Name the repo in your final message.',
    'review_wait': ('PR #%s goes into %s, a gated branch of %s (pr_gate in config), so the merge waits for both '
                    'reviews of its head %s. Missing: %s. Run ' + _esc(PR_REVIEW) + ' %s codex (or claude) from a '
                    'checkout of the head, read the comment it posts, then merge.'),
    'review_block': ('PR #%s: the %s review of head %s says VERDICT: BLOCK. Fix the findings, push, and review the new '
                     'head before merging.'),
    'review_unread': 'gh pr merge is held: the PR\'s reviews could not be read (%s). Retry once gh works.',
    'api_merge': ('gh api cannot merge a PR around the review gate (pr_gate in config). Merge with gh pr merge <number>, '
                  'which waits for both reviews.'),
    'lane': ('This worktree\'s .agent-lane names the other tool, which owns its commits. Leave your change '
             'uncommitted and write a handoff note (handoff skill), or work in a worktree of your own.'),
    'rm': ('Recursive delete goes to the trash. Run it as its own command: ' + TRASH + ' <paths> '
           '(dated _to_delete/, manifest, restore). It stops for your review over 1 GB, on populated databases '
           'and on pattern batches. Before calling anything deletable, prove you created it (git log or session log).'),
    'find_delete': ('find -delete / -exec rm deletes a pattern-matched batch. List the matches first (same find '
                    'without -delete), show ' + USER + ' the full list, and after a yes move them with ' + TRASH +
                    ' --approved <paths>.'),
    'pipe_shell': ('A shell that runs a script from a pipe, here-string or process substitution hides what it does. '
                   'Run the commands directly instead.'),
    'deep': ('This command nests shells, eval or substitutions too deeply for the guard to read. Split it into separate, '
             'plain commands.'),
    'sql': ('DROP and TRUNCATE destroy data. Rename instead: ALTER TABLE <t> RENAME TO _to_delete_<yyyymmdd>_<t> '
            '(columns: RENAME COLUMN). If the table or database holds rows, stop and show ' + USER + ' the names and '
            'row counts first; only drop after a yes. Disposable databases under /tmp are exempt.'),
    'pattern_move': ('This moves a pattern-matched batch out of its repo. List the matches, show ' + USER + ' the '
                     'full list, and after a yes move them by name (or with ' + TRASH + ' --approved).'),
    'bulk_move': ('Bulk moves out of a repo go to the trash: ' + TRASH + ' <paths> keeps a manifest and restore. '
                  'Moving into an _archive/ folder by explicit names is fine.'),
    'rm_cwd': ('That deletes the whole working folder. Name the files or folders to delete; a recursive delete of plain '
               'paths then goes to the trash.'),
    'mv_wt': 'Moving a worktree with mv breaks git links. Use git worktree move.',
    'env': 'Reading an env file puts secrets into the conversation. Read .env.example or check variable names instead.',
    'lake': ('This writes to a protected folder (protected_dirs in config), which is read-only unless ' + USER +
             ' says otherwise. Prove it on a scratch copy first.'),
    'apply': ('Do not apply learning-loop proposals yourself. Leave them in ' + PROPOSALS + ' for ' + USER_S +
              ' review and tell ' + USER + ' how many are waiting.'),
    'gated': ('Claude Code asks you before spawning this subagent because %s is gated in local/config.json '
              '(gated_models). Say yes only if the task needs that model; take it out of gated_models to stop the prompt.'),
    'supabase': ('Supabase project %s is protected%s. Schema changes go only through the repo\'s scripts/deploy. '
                 'If the repo has no scripts/deploy yet, say so and stop.'),
}


# The git policy above as Codex prefix rules (~/.codex/rules/agent-core.rules, built by bin/build.py): a second layer
# under the hook. A rule matches leading tokens only, so `git -C dir ...` and reordered flags are left to the hook,
# and so are recursive deletes (a prefix can't tell node_modules from a project folder). A list is a set of
# alternatives for that position. tests/test_codex_guard.py checks that the hook denies every expansion.
MAINS = ['main', 'master']
CODEX_RULES = [
    (['git', 'add', ['-A', '--all', '.', './', ':/', '-u', '--update']], 'stage'),
    (['git', 'stage', ['-A', '--all', '.', './', ':/', '-u', '--update']], 'stage'),
    (['git', 'commit', ['-a', '-am', '--all']], 'stage'),
    (['git', 'push', ['--force', '-f', '--force-with-lease'], 'origin', MAINS], 'force_main'),
    (['git', 'push', 'origin', ['--force', '-f', '--force-with-lease'], MAINS], 'force_main'),
    (['git', 'push', 'origin', ['+main', '+master', ':main', ':master']], 'force_main'),
    (['git', 'push', ['--delete', '-d'], 'origin', MAINS], 'force_main'),
    (['git', 'push', 'origin', ['--delete', '-d'], MAINS], 'force_main'),
    (['git', 'reset', '--hard'], 'reset'),
    (['git', 'clean', ['-f', '-fd', '-df', '-fdx', '-dfx', '-xdf', '-xfd', '-fx', '-xf', '-ffd', '-ffdx', '--force']],
     'clean'),
    (['git', 'switch', ['--discard-changes', '-f', '--force']], 'discard'),
    (['git', 'update-ref', ['-d', '--delete']], 'branch'),
    (['git', 'update-ref', ['refs/heads/main', 'refs/heads/master']], 'move_main'),
    (['git', 'branch', ['-f', '--force'], MAINS], 'move_main'),
    (['git', 'rm', ['-rf', '-fr'], ['.', './', ':/', '*']], 'discard'),
    (['gh', 'repo', 'delete'], 'repo_delete'),
    (['git', 'worktree', 'remove'], 'worktree'),
]


def _ansi_c(body):
    """The text of a bash $'...' string."""
    plain = {'a': '\a', 'b': '\b', 'e': '\x1b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '\\': '\\',
             "'": "'", '"': '"', '?': '?'}

    def one(m):
        e = m.group(1)
        if e[0] in 'xu':
            return chr(int(e[1:], 16))
        if e[0].isdigit():
            return chr(int(e, 8) & 0xFF)
        return plain.get(e, '\\' + e)
    return re.sub(r'\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|[0-7]{1,3}|.)', one, body, flags=re.S)


def normalize(cmd):
    """Drop heredoc bodies, comments and line continuations; turn unquoted line breaks into ';'."""
    lines = cmd[:MAX_CHARS].split('\n')
    kept, i = [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        marks = [m for m in HEREDOC.finditer(line) if not m.group(2).startswith(HEREDOC_MARK)]
        for m in marks:
            body = []
            while i < len(lines) and lines[i].strip() != m.group(2):
                body.append(lines[i])
                i += 1
            i += 1
            STATE['heredocs'].append('\n'.join(body))
            STATE['expands'].append(m.group(1) == '')  # an unquoted delimiter lets $( ) and backticks in the body run
        for k, m in reversed(list(enumerate(marks))):
            n = len(STATE['heredocs']) - len(marks) + k
            line = line[:m.start()] + '<< %s%d' % (HEREDOC_MARK, n) + line[m.end():]
        kept.append(line)
    text = '\n'.join(kept)
    out, quote, esc, word_start, i = [], None, False, True, 0
    while i < len(text):
        ch = text[i]
        if esc:
            out.append(ch)
            esc, word_start = False, False
        elif quote:
            if ch == '\\' and quote == '"':
                esc = True
            elif ch == quote:
                quote = None
            out.append(ch)
        elif ch == '\\':
            if i + 1 < len(text) and text[i + 1] == '\n':
                i += 2
                continue
            out.append(ch)
            esc = True
        elif ch == "'" and i and text[i - 1] == '$' and (i < 2 or text[i - 2] != '\\'):
            j = i + 1  # $'...' honours backslash escapes, which shlex does not: decode it into a plain quoted word
            while j < len(text) and text[j] != "'":
                j += 2 if text[j] == '\\' else 1
            if j < len(text):
                out.pop()
                out.append(shlex.quote(_ansi_c(text[i + 1:j])))
                word_start = False
                i = j + 1
                continue
            quote, word_start = ch, False
            out.append(ch)
        elif ch in ('"', "'"):
            quote, word_start = ch, False
            out.append(ch)
        elif ch == '#' and word_start:
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        elif ch == '\n':
            out.append(' ; ')
            word_start = True
        else:
            out.append(ch)
            word_start = ch in ' \t;&|()'
        i += 1
    return ''.join(out)


def tokens_of(text):
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ''
        return list(lex)
    except ValueError:
        return []  # unbalanced quotes: bash refuses to run it, so there is nothing to judge


def simple_commands(tokens):
    cmd = []
    for t in tokens:
        if t in SEPARATORS or (t and all(c in ';&|()' for c in t)):
            if cmd:
                yield cmd
            cmd = []
        else:
            cmd.append(t)
    if cmd:
        yield cmd


def split_commands(tokens):
    """[(kind, tokens, separator before, separator after)]; kind is 'cmd', or 'open'/'close' for a ( ) group."""
    events, cmd, prev = [], [], ';'

    def flush(nxt):
        nonlocal cmd, prev
        if cmd:
            events.append(('cmd', cmd, prev, nxt))
        cmd, prev = [], nxt

    for t in tokens:
        run = bool(t) and all(c in ';&|()' for c in t)
        if t in SEPARATORS and not run:  # { } !
            flush(t)
        elif run:
            for s in SEP_PART.findall(t):
                if s == '(':
                    flush('(')
                    events.append(('open', [], '(', '('))
                elif s == ')':
                    flush(')')
                    events.append(('close', [], ')', ')'))
                else:
                    flush(s)
        else:
            cmd.append(t)
    flush(';')
    return events


def strip_prefix(toks):
    """Drop VAR=value assignments, shell keywords (then, do, ...) and wrappers such as env, sudo, time, nohup, timeout."""
    toks = list(toks)
    while toks:
        head = os.path.basename(toks[0])
        if ASSIGN.match(toks[0]) or toks[0] in KEYWORDS:
            toks.pop(0)
        elif head in WRAPPERS:
            toks.pop(0)
            values = WRAPPER_VALUE.get(head, ())
            while toks and toks[0].startswith('-'):
                flag = toks.pop(0)
                if flag == '--':
                    break
                if flag in values and toks:
                    toks.pop(0)
            if head == 'timeout' and toks and re.match(r'^[0-9.]+[smhd]?$', toks[0]):
                toks.pop(0)
        else:
            break
    return toks


def flag_parts(args, value_opts=(), value_letters=''):
    """Split args into (short letters, long flags, positionals), skipping option values."""
    short, long_, pos = set(), set(), []
    skip, end = False, False
    for a in args:
        if skip:
            skip = False
            continue
        if end or a == '-' or not a.startswith('-'):
            pos.append(a)
            continue
        if a == '--':
            end = True
            continue
        if a.startswith('--'):
            name = a.split('=', 1)[0]
            long_.add(name)
            skip = name in value_opts and '=' not in a
            continue
        if a in value_opts:
            skip = True
            continue
        for k, ch in enumerate(a[1:]):
            short.add(ch)
            if ch in value_letters:
                skip = k == len(a) - 2
                break
    return short, long_, pos


def split_redirects(args):
    """(operands, [(operator, target)]) with redirections such as < file, <<EOF markers and <<< string taken out."""
    plain, redirs, k = [], [], 0
    while k < len(args):
        a = args[k]
        if re.match(r'^[<>&]+$', a) and not a.startswith('&'):
            redirs.append((a, args[k + 1] if k + 1 < len(args) else ''))
            k += 2
        elif a.isdigit() and k + 1 < len(args) and re.match(r'^[<>]+&?$', args[k + 1]):
            k += 1  # the 2 of 2>/dev/null
        else:
            plain.append(a)
            k += 1
    return plain, redirs


LANE_SUBS = {'commit', 'merge', 'cherry-pick', 'revert', 'am', 'rebase'}


def lane_owner(start):
    """The tool named in .agent-lane at the root of the worktree holding start, or None when there is no lane file."""
    root = common.repo_root(start)
    try:
        with open(os.path.join(root, '.agent-lane'), encoding='utf-8') as fh:
            for line in fh:
                key, _, value = line.partition(':')
                if key.strip() == 'tool':
                    return value.strip().lower() or None
    except (OSError, TypeError):
        return None
    return None


CODEX_BOTS = {'chatgpt-codex-connector', 'chatgpt-codex-connector[bot]'}  # Codex's GitHub code review app
GH_MERGE_VALUE = {'-R', '--repo', '-b', '--body', '-F', '--body-file', '-t', '--subject', '-A', '--author-email',
                  '--match-head-commit'}


def gate_branches():
    """The base branches the PR gate covers, from config pr_gate; empty when the gate is off."""
    gate = common.config().get('pr_gate')
    if not isinstance(gate, dict) or not gate.get('enabled'):
        return []
    return _listed(gate.get('branches'))


def gate_covers(slug, branch):
    """True when config pr_gate covers a PR into branch on the GitHub repo owner/repo slug."""
    gate = common.config().get('pr_gate') or {}
    branches = gate_branches()
    if not branches or branch not in branches:
        return False
    repos = gate.get('repos', '*')
    if repos == '*' or repos is None:
        return True
    return slug.lower() in [r.lower() for r in _listed(repos)]


def gh_run(argv):
    """Run gh with a hard timeout (GH_TIMEOUT, and GH_BUDGET for all calls of this hook run) and kill its whole process
    group on expiry. The hook itself is killed at its own timeout, and a killed hook lets the merge through."""
    left = GH_BUDGET - (time.monotonic() - START)
    if left < 0.5:
        raise subprocess.TimeoutExpired(argv, 0)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=STATE['cwd'] or None, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=min(GH_TIMEOUT, left))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.SubprocessError:
            pass
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def review_authors(gh, gate):
    """Logins whose verdict comments count: pr_gate.reviewers when set, else the signed-in gh user."""
    listed = [r.lstrip('@').lower() for r in _listed(gate.get('reviewers'))]
    if listed:
        return set(listed)
    out = gh_run([gh, 'api', 'user', '--jq', '.login'])
    login = out.stdout.strip()
    if out.returncode or not login:
        raise ValueError(out.stderr.strip()[:200] or 'could not read the signed-in gh user')
    return {login.lower()}


def review_verdict(pr, who, head, authors, trust_bot=False):
    """'PASS', 'BLOCK' or None for the latest '## <Who> ...review' comment naming head, written by one of authors.
    Codex's GitHub review app on the head commit counts as a Codex review only when pr_gate.trust_codex_bot is true."""
    found = None
    for c in pr.get('comments') or []:
        if str((c.get('author') or {}).get('login') or '').lower() not in authors:
            continue
        body = str(c.get('body') or '').lstrip()
        if body.startswith('## ' + who) and ('head: ' + head) in body:
            m = re.findall(r'^VERDICT: (PASS|BLOCK)\s*$', body, re.M)
            found = m[-1] if m else found
    if who == 'Codex' and not found and trust_bot:
        for r in pr.get('reviews') or []:
            if (r.get('author') or {}).get('login') in CODEX_BOTS and (r.get('commit') or {}).get('oid') == head:
                found = 'PASS'
    return found


def merge_decision(args, repo=()):
    """The PR gate (config pr_gate): a PR into a gated branch merges only after a Claude and a Codex verdict on its
    head commit, by an allowed author, neither BLOCK. The gate off, other branches and repos not listed pass."""
    if not gate_branches():
        return None
    gate = common.config().get('pr_gate') or {}
    sel, i = [], 0
    repo = list(repo)
    while i < len(args):
        a = args[i]
        if a in GH_MERGE_VALUE:
            if a in ('-R', '--repo') and i + 1 < len(args):
                repo = ['-R', args[i + 1]]
            i += 2
            continue
        if a.startswith('--repo='):
            repo = ['-R', a.split('=', 1)[1]]
        elif not a.startswith('-'):
            sel.append(a)
        i += 1
    gh = os.environ.get('AGENT_CORE_GH') or 'gh'
    try:
        out = gh_run([gh, 'pr', 'view'] + sel[:1] + repo + ['--json', 'number,url,baseRefName,headRefOid,'
                                                                      'comments,reviews'])
        if out.returncode:
            return 'deny', MSG['review_unread'] % (out.stderr.strip()[:200] or 'gh failed')
        pr = json.loads(out.stdout)
        slug = '/'.join(str(pr.get('url') or '').split('/')[3:5])
        branch = pr.get('baseRefName') or ''
        if not gate_covers(slug, branch):
            return None
        authors = review_authors(gh, gate)
    except subprocess.TimeoutExpired:
        return 'deny', MSG['review_unread'] % ('gh did not answer within %d seconds' % GH_TIMEOUT)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return 'deny', MSG['review_unread'] % exc
    head = pr.get('headRefOid') or ''
    trust = gate.get('trust_codex_bot') is True
    verdicts = {who: review_verdict(pr, who, head, authors, trust) for who in ('Claude', 'Codex')}
    for who, v in verdicts.items():
        if v == 'BLOCK':
            return 'deny', MSG['review_block'] % (pr.get('number'), who, head[:12])
    missing = [who for who, v in verdicts.items() if not v]
    if missing:
        return 'deny', MSG['review_wait'] % (pr.get('number'), branch, slug, head[:12], ' and '.join(missing),
                                             pr.get('number'))
    return None


def gh_split(args):
    """(command words, ['-R', repo] or [], other args) for a gh call, with -R/--repo taken out wherever it stands."""
    words, rest, repo, k = [], [], [], 0
    while k < len(args):
        a = args[k]
        if a in ('-R', '--repo') and k + 1 < len(args):
            repo = ['-R', args[k + 1]]
            k += 1
        elif a.startswith('--repo='):
            repo = ['-R', a.split('=', 1)[1]]
        elif a.startswith('-R') and not a.startswith('--') and len(a) > 2:
            repo = ['-R', a[2:]]
        elif len(words) < 2 and not a.startswith('-'):
            words.append(a)
        else:
            rest.append(a)
        k += 1
    return words, repo, rest


def api_method(args):
    """The method of a gh api call: -X DELETE, -X=DELETE, -XDELETE, -iX DELETE, --method DELETE or --method=DELETE; the last wins."""
    method = ''
    for k, a in enumerate(args):
        if a in ('--method',) and k + 1 < len(args):
            method = args[k + 1]
        elif a.startswith('--method='):
            method = a.split('=', 1)[1]
        elif a.startswith('-') and not a.startswith('--') and 'X' in a and not set(a[1:a.index('X')]) & set('fFHqtp'):
            tail = a[a.index('X') + 1:]
            method = tail.lstrip('=') if tail else (args[k + 1] if k + 1 < len(args) else '')
    return method.upper()


def api_destroys(args):
    """The message key for a gh api call that deletes a repos/<owner>/<repo>, or deletes / force-updates the heads/main or
    heads/master ref; None otherwise."""
    method = api_method(args)
    if method == 'DELETE' and any(API_REPO.match(a) for a in args):
        return 'repo_delete'
    if method == 'DELETE' and any(API_MAIN_REF.match(a) for a in args):
        return 'force_main'
    if method == 'PATCH' and any(API_MAIN_REF.match(a) for a in args) and any(
            re.search(r'force\s*=\s*(true|1)|^--input', a) for a in args):
        return 'force_main'
    return None


def gh_decision(args):
    words, repo, rest = gh_split(args)
    if words[:2] == ['pr', 'merge']:
        return merge_decision(rest, repo)
    if words[:1] == ['api'] and gate_branches() and any(API_MERGE.search(a) or GRAPHQL_MERGE.search(a) for a in args):
        return 'deny', MSG['api_merge']
    if words[:2] == ['repo', 'delete']:
        return 'deny', MSG['repo_delete']
    key = api_destroys(args) if words[:1] == ['api'] else None
    if key:
        return 'deny', MSG[key]
    if words[:2] == ['pr', 'checkout']:
        mark_moved(STATE['cwd'] or os.getcwd(), 'unknown')  # the PR's branch is not known here
    return None


def current_branch(where):
    """The branch checked out in where, or None when it cannot be read (detached HEAD, no repo, git missing)."""
    try:
        out = subprocess.run(['git', '-C', where, 'symbolic-ref', '--short', 'HEAD'], capture_output=True, text=True,
                             timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def branch_exists(where, name):
    """True when refs/heads/<name> exists in where, or cannot be told (git missing, no repo)."""
    try:
        out = subprocess.run(['git', '-C', where, 'show-ref', '--verify', '--quiet', 'refs/heads/' + name],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return True
    return out.returncode != 1


def stage_sweeps(specs, where):
    """True when a git add pathspec list stages a whole tree: ., .., :/, :(top), a folder that is the repo root, only
    exclusions, or a command substitution whose result cannot be known."""
    root = common.repo_root(where)
    root = os.path.realpath(root) if root else None
    excluded = 0
    for spec in specs:
        if '$(' in spec or '`' in spec or spec.endswith('$') or spec == ':':
            return True
        rest, top, exclude = spec, False, False
        if spec.startswith(':'):
            if spec.startswith(':('):
                end = spec.find(')')
                if end < 0:
                    return True
                magic = [m.strip().split(':')[0] for m in spec[2:end].split(',')]
                top, exclude, rest = 'top' in magic, 'exclude' in magic, spec[end + 1:]
            else:
                i = 1
                while i < len(spec) and spec[i] in '/!^':
                    i += 1
                top, exclude, rest = '/' in spec[1:i], bool(set(spec[1:i]) & set('!^')), spec[i:].lstrip(':')
        if top:
            return True
        if exclude:
            excluded += 1
            continue
        if not rest.strip('./*?'):
            return True
        if root:
            path = re.sub(r'^\$\{?PWD\}?(?=/|$)', where, rest)
            path = re.sub(r'^\$\{?HOME\}?(?=/|$)', common.HOME, path)
            if '$' not in path and not GLOB.search(path) and os.path.realpath(
                    os.path.normpath(os.path.join(where, os.path.expanduser(path)))) == root:
                return True
    return bool(specs) and excluded == len(specs)


def git_head(args):
    """(folder, subcommand, its arguments, -c settings) of a git call, or None when there is no subcommand. The settings
    are -c and --config-env on the command line and GIT_CONFIG_* variables set before it."""
    i, where, config = 0, STATE['cwd'] or os.getcwd(), list(STATE['envcfg'])
    while i < len(args):
        if args[i] == '-C' and i + 1 < len(args):
            where = os.path.join(where, os.path.expanduser(args[i + 1]))
        if args[i] in ('-c', '--config-env') and i + 1 < len(args):
            config.append(args[i + 1])
        elif args[i].startswith('--config-env='):
            config.append(args[i].split('=', 1)[1])
        if args[i] in GIT_VALUE:
            i += 2
        elif args[i].startswith('-'):
            i += 1
        else:
            break
    return (where, args[i], args[i + 1:], config) if i < len(args) else None


def env_config(toks):
    """The settings GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n / GIT_CONFIG_PARAMETERS give a git command,
    from assignments in front of it and from variables set earlier, as key=value strings."""
    env = dict(STATE['vars'])
    for t in toks:
        if os.path.basename(t) == 'git':
            break
        if ASSIGN.match(t):
            k, v = t.split('=', 1)
            env[k] = v
    try:
        count = min(int(env.get('GIT_CONFIG_COUNT') or 0), 64)
    except ValueError:
        count = 0
    out = ['%s=%s' % (env['GIT_CONFIG_KEY_%d' % n], env.get('GIT_CONFIG_VALUE_%d' % n, '')) for n in range(count)
           if env.get('GIT_CONFIG_KEY_%d' % n)]
    return out + re.findall(r"'([^']*)'", env.get('GIT_CONFIG_PARAMETERS', ''))


def wt_key(where):
    """The worktree a git command works in, so a branch change in one is not held against a push in another."""
    return os.path.realpath(common.repo_root(where) or where)


def mark_moved(where, kind):
    key = wt_key(where)
    if kind == 'main' or (kind and STATE['moved'].get(key) != 'main'):
        STATE['moved'][key] = kind


def moved_in(where):
    return STATE['moved'].get(wt_key(where))


def restore_moves():
    """The end of an && chain after a switch to a named branch: the switch may have failed, so what was known before is back."""
    for key, kind in STATE['undo']:
        STATE['moved'].setdefault(key, kind)
    STATE['undo'] = []


def branch_class(name, track=False):
    """'main' for main/master (refs/heads/main too; origin/main only with --track, which makes a local main), 'unknown' for
    a name that cannot be told (-, @{-1}, a variable), None for any other branch (feature/main is one) or a path."""
    if not name or name == '-' or '@{' in name or '$' in name or '`' in name:
        return 'unknown'
    if name in MAIN_REFS or MAIN_DST.search(name) or (track and name.split('/')[-1] in MAINS):
        return 'main'
    return None


def note_move(sub, rest, config, where, clear=False):
    """Remember in STATE['moved'] (per worktree: 'main' or 'unknown') that this git command may leave main, or a branch
    that cannot be told, checked out for the commands after it. The branch read at hook time is stale from then on.
    Moving to another named branch changes nothing for a later push, and with clear (&& on both sides: the rest of the
    chain runs only if the switch worked) it even undoes an earlier 'main' for that && chain."""
    kind, switched = None, False
    if any(c.lower().startswith('alias.') for c in config) or sub not in GIT_KNOWN:
        kind = 'unknown'  # an alias or an external git-<name> can do anything
    elif sub in ('checkout', 'switch'):
        made = {'-b', '-B', '-c', '-C', '--create', '--force-create'}
        make = next((rest[k + 1] for k, a in enumerate(rest[:-1]) if a in made), None)
        if make is None:  # -cNAME, -bNAME, --create=NAME, --force-create=NAME
            make = next((a.split('=', 1)[1] if a.startswith('--') else a[2:] for a in rest if (
                a[:2] in ('-b', '-B', '-c', '-C') and not a.startswith('--') and len(a) > 2) or a.startswith(
                ('--create=', '--force-create='))), None)
        flags = flag_parts(rest, value_opts=made | {'--orphan', '--source', '-s', '--conflict', '--pathspec-from-file'},
                           value_letters='bBcCs')
        track = 't' in flags[0] or '--track' in flags[1]
        p = flags[2]
        target = make or (p[0] if p else None)
        if target and not (sub == 'checkout' and '--' in rest and not make):  # checkout [<tree>] -- <paths> restores files
            kind = branch_class(target, track and not make)
            switched = bool(make) or sub == 'switch' or not os.path.lexists(os.path.join(where, target))
            switched = switched and '--detach' not in flags[1] and not flags[0] & {'d'}
    elif sub == 'rebase':
        s_, l_, p = flag_parts(rest, value_opts={'--onto', '-s', '--strategy', '-X', '--strategy-option', '-x', '--exec', '-C',
                                                 '--whitespace', '--empty'}, value_letters='sXxC')
        target = p[0] if '--root' in l_ and p else (p[1] if len(p) > 1 else None)  # rebase <upstream> <branch> switches
        kind = branch_class(target) if target else None
    elif sub == 'symbolic-ref':
        p = flag_parts(rest, value_opts={'-m'}, value_letters='m')[2]
        kind = branch_class(p[1]) if len(p) > 1 and p[0] == 'HEAD' else None
    elif sub == 'bisect':
        kind = 'unknown' if rest[:1] == ['reset'] else None
    elif sub == 'branch':
        s_, l_, p = flag_parts(rest)
        kind = branch_class(p[-1]) if p and (s_ & {'m', 'M'} or '--move' in l_) else None
    if kind:
        mark_moved(where, kind)
    elif switched and clear:
        key = wt_key(where)
        if key in STATE['moved']:
            STATE['undo'].append((key, STATE['moved'].pop(key)))


def forced_rm(rest, where):
    """git rm that is forced, keeps the files in the tree out of the index only with --cached, and names the whole tree,
    a pathspec file, or nothing at all (xargs and pipes feed the paths)."""
    s, l, p = flag_parts(rest, value_opts={'--pathspec-from-file'})
    if not ('f' in s or '--force' in l) or '--cached' in l or 'n' in s or '--dry-run' in l:
        return False
    return not p or '--pathspec-from-file' in l or stage_sweeps(p, where)


def git_decision(args):
    head = git_head(args)
    if not head:
        return None
    where, sub, rest, config = head
    note_move(sub, rest, config, where, clear=STATE['sep'] == STATE['before'] == '&&')
    if sub in LANE_SUBS:
        owner = lane_owner(where)
        if owner and owner != (os.environ.get('AGENT_CORE_TOOL') or 'claude'):
            return 'deny', MSG['lane']
    if sub in ('add', 'stage'):
        s, l, p = flag_parts(rest, value_opts={'--chmod', '--pathspec-from-file'})
        if s & {'A', 'u'} or l & {'--all', '--update', '--no-ignore-removal'}:
            # -A and -u stage only the named paths; a bare one, a pathspec file, a pathspec the shell may expand to nothing
            # ($VAR, $(...), a glob) or a tree-wide pathspec is the sweep
            if not p or '--pathspec-from-file' in l or any(re.search(r'[$`*?\[{]', x) for x in p) or stage_sweeps(p, where):
                return 'deny', MSG['stage']
        elif stage_sweeps(p, where):
            return 'deny', MSG['stage']
    elif sub == 'commit':
        s, l, p = flag_parts(rest, value_opts={'--message', '--file', '--author', '--date', '--template', '--reuse-message',
                                               '--reedit-message', '--fixup', '--squash', '--trailer', '--cleanup',
                                               '-m', '-F', '-C', '-c', '-t'}, value_letters='mFCct')
        if 'a' in s or '--all' in l:
            return 'deny', MSG['stage']
    elif sub == 'push':
        s, l, p = flag_parts(rest, value_opts={'--repo', '--receive-pack', '--exec', '--push-option', '-o'}, value_letters='o')
        force = 'f' in s or bool(l & {'--force', '--force-with-lease', '--force-if-includes'})
        deleting = 'd' in s or '--delete' in l
        plus = colon_delete = main_hit = wild = unresolved = False
        for ref in p[1:]:
            plus = plus or ref.startswith('+')
            src, sep, dst = ref.lstrip('+').partition(':')
            colon_delete = colon_delete or (bool(sep) and not src)
            main_hit = main_hit or (dst if sep else src) in MAIN_REFS or bool(MAIN_DST.search(dst if sep else src))
            wild = wild or '*' in ref
            unresolved = unresolved or bool(re.search(r'\$[A-Za-z_{]', ref))  # a variable: it may well hold main
        # a refspec or mirror in the command line config (remote.<name>.push / .mirror, push.default) is a push nobody can read
        configured = any(re.match(r'remote\..*\.(push|mirror)(=|$)', c, re.I) or (re.match(r'push\.default=', c, re.I) and (force or plus))
                         for c in config)
        if configured or ((force or plus or deleting or colon_delete) and main_hit):
            return 'deny', MSG['force_main']
        if (force or plus) and (l & {'--all', '--branches'} or wild or unresolved):
            return 'deny', MSG['force_main']
        if (force or plus) and (len(p) < 2 or any(r.lstrip('+') in ('HEAD', '@', '$') or '$(' in r or '`' in r for r in p[1:])):
            # no refspec (or one a substitution fills in), or HEAD: the current branch is what gets force-pushed, and an unreadable branch counts as main;
            # so is main as an earlier part of this command left it
            if moved_in(where) == 'main' or current_branch(where) in (None, 'main', 'master'):
                return 'deny', MSG['force_main']
            if moved_in(where) or STATE['cwd'] is None:  # switched to a branch we cannot name, or a folder we cannot follow
                return 'deny', MSG['branch_unknown']
        if plus or (force and not (l & {'--force-with-lease', '--force-if-includes'})):
            return 'deny', MSG['force']
        if deleting or colon_delete:
            return 'deny', MSG['delete_remote']
        if '--mirror' in l:
            return 'deny', MSG['mirror']
    elif sub == 'reset':
        if '--hard' in flag_parts(rest)[1]:
            return 'deny', MSG['reset']
    elif sub == 'clean':
        s, l, p = flag_parts(rest, value_opts={'-e', '--exclude'}, value_letters='e')
        if not ('n' in s or '--dry-run' in l):  # whatever -c clean.requireForce says
            return 'deny', MSG['clean']
    elif sub in ('checkout', 'restore'):
        s, l, p = flag_parts(rest, value_opts={'-b', '-B', '--orphan', '--source', '-s', '--conflict', '--pathspec-from-file'},
                             value_letters='bBs')
        if sub == 'restore' and ('S' in s or '--staged' in l) and not ('W' in s or '--worktree' in l):
            return None
        if set(p) & {'.', './', ':/', '*'} or (sub == 'checkout' and ('f' in s or '--force' in l)):
            return 'deny', MSG['discard']
    elif sub == 'switch':
        s, l, p = flag_parts(rest, value_opts={'-c', '-C', '--create', '--force-create', '--orphan', '--conflict'},
                             value_letters='cC')
        if 'f' in s or l & {'--force', '--discard-changes'}:
            return 'deny', MSG['discard']
    elif sub == 'update-ref':
        s, l, p = flag_parts(rest, value_opts={'-m'}, value_letters='m')
        if 'd' in s or '--delete' in l:
            return 'deny', MSG['branch']
        if '--stdin' in l or (p and p[0] in MAIN_REFS):
            return 'deny', MSG['move_main']
        if p and p[0] == 'HEAD' and (moved_in(where) == 'main' or current_branch(where) in (None, 'main', 'master')):
            return 'deny', MSG['move_main']
    elif sub == 'branch':
        s, l, p = flag_parts(rest)
        force = 'f' in s or '--force' in l
        if 'D' in s or (('d' in s or '--delete' in l) and force):
            return 'deny', MSG['branch']
        if force and not s & {'d', 'm', 'M', 'c', 'C'} and p and p[0] in MAINS:
            return 'deny', MSG['move_main']
        if p and p[-1] in MAINS and (s & {'M', 'C'} or (s & {'m', 'c'} or l & {'--move', '--copy'}) and force):
            # a forced rename or copy onto main; the first `git branch -M main` of a new repo has no main to overwrite
            if not (len(p) == 1 and current_branch(where) == p[0]) and branch_exists(where, p[-1]):
                return 'deny', MSG['move_main']
    elif sub == 'config':
        s, l, p = flag_parts(rest, value_opts={'-f', '--file', '--blob', '--type', '-t', '--default'}, value_letters='ft')
        p = p[1:] if p[:1] == ['set'] else p
        reading = s & {'l', 'e'} or l & {'--get', '--get-all', '--get-regexp', '--list', '--unset', '--unset-all', '--edit'}
        if len(p) >= 2 and not reading and re.match(r'(remote\..*\.(push|mirror)|push\.default)$', p[0], re.I):
            return 'deny', MSG['force_main']  # a saved refspec or mirror makes every later `git push` a push nobody can read
    elif sub == 'rm':
        if forced_rm(rest, where):
            return 'deny', MSG['discard']  # forced, so it throws away uncommitted edits of every tracked file
    elif sub == 'stash':
        if rest[:1] and rest[0] in ('drop', 'clear'):
            return 'deny', MSG['stash']
    elif sub == 'worktree':
        if rest[:1] == ['remove']:
            return 'deny', MSG['worktree']
        if rest[:1] == ['prune']:
            s, l, p = flag_parts(rest[1:])
            if not ('n' in s or '--dry-run' in l):
                return 'deny', MSG['worktree']
    elif sub in ('filter-branch', 'filter-repo'):
        return 'deny', MSG['rewrite']
    return None


def _temp_path(path):
    """True when path, with symlinks resolved, sits under a temp root."""
    roots = {r for r in TEMP_ROOTS} | {os.path.realpath(r) for r in TEMP_ROOTS}
    probe = os.path.dirname(path) if GLOB.search(os.path.basename(path)) else path
    real = os.path.realpath(probe)
    return any(path.startswith(r + '/') and real.startswith(q + '/') for r in TEMP_ROOTS for q in roots)


def _not_a_project(folder):
    """True for a folder that holds projects instead of being one: home, a volume root, projects_root from the config, and
    a Projects/Code/... folder directly under home or a volume."""
    folder = os.path.normpath(folder)
    parent = os.path.dirname(folder)
    root = common.config().get('projects_root')
    tops = {common.HOME, os.path.realpath(common.HOME)}
    if root:
        tops |= {os.path.normpath(os.path.expanduser(str(root))), os.path.realpath(os.path.expanduser(str(root)))}
    return (folder in ('/', '/Volumes', '/Users') or parent == '/Volumes' or folder in tops or os.path.realpath(folder) in tops
            or (os.path.basename(folder).lower() in PROJECT_HOMES and (parent in tops or os.path.dirname(parent) == '/Volumes')))


def _in_project(folder):
    """True when folder or a folder above it (below home, a volume or the projects folder) has a project marker."""
    while folder and not _not_a_project(folder):
        if any(os.path.exists(os.path.join(folder, m)) for m in PROJECT_MARKERS):
            return True
        up = os.path.dirname(folder)
        if up == folder:
            break
        folder = up
    return False


def disposable(target):
    """True for a target `rm -r` may delete without the trash: a fresh mktemp, a path under temp, a build folder.
    A target with .. segments, or with a variable or substitution other than a bare $TMPDIR/..., never is."""
    segments = target.split('/')
    if '..' in segments:
        return False
    if MKTEMP.match(target):
        return True
    m = TMPDIR_REF.match(target)
    if m:
        return '$' not in m.group(1) and '`' not in m.group(1)
    if '$' in target or '`' in target:
        return False
    if target.startswith(('/', '~')):
        norm = os.path.normpath(os.path.expanduser(target))
    else:
        if STATE['cwd'] is None and (PROTECTED.name_re or PROTECTED.paths):
            return False  # a relative target with the folder unknown could sit in a protected folder
        norm = os.path.normpath(os.path.join(STATE['cwd'], target) if STATE['cwd'] else target)
    if _temp_path(norm):
        return True
    folder = norm.rstrip('/')
    if GLOB.search(os.path.basename(folder)):
        folder = os.path.dirname(folder)
    base, parent = os.path.basename(folder), os.path.dirname(folder)
    known = os.path.isabs(folder)  # with the working folder unknown only an unambiguous build name is enough
    real = os.path.realpath(folder) if known else folder
    for where in {folder, real}:  # a project's tmp that is a symlink to ~/tmp is ~/tmp
        if known and _not_a_project(os.path.dirname(where)):
            return False  # ~/tmp, /Volumes/disk/target, ~/Projects/build: somebody's folder, whatever it is called
    if base in GENERIC:
        return known and _in_project(parent) and _in_project(os.path.dirname(real))
    return base in REGENERABLE or base.endswith('.egg-info') or base.startswith(('dist-', 'build-'))


def plain_files(target):
    """True when target is a literal path to an existing regular file and this rm is the very first command of a script
    that has nothing running before or beside it (PLAIN_RM): the file is still a file when rm reaches it, so rm -r is
    rm -f. A glob or variable never counts (the shell may expand it to more than the guard saw)."""
    if STATE['cwd'] is None or not STATE['plain'] or re.search(r'[$`*?\[{~\\]', target):
        return False
    path = os.path.join(STATE['cwd'], target)
    return os.path.isfile(path) and not PROTECTED.hit(os.path.realpath(path), ancestor=True)


def rm_parts(args):
    recursive, force, targets, end = False, False, [], False
    for a in args:
        if not end and a == '--':
            end = True
        elif not end and a.startswith('--'):
            recursive = recursive or a == '--recursive'
            force = force or a == '--force'
        elif not end and a.startswith('-') and len(a) > 1:
            recursive = recursive or 'r' in a[1:].lower()
            force = force or 'f' in a[1:]
        else:
            targets.append(a)
    return recursive, force, targets


def whole_cwd(target):
    """True for ., ./, *, .., or the working folder by name (also as ../<name> or ~+): a delete that would take the whole
    folder with it."""
    if '$' in target or '`' in target:
        return False
    cwd = STATE['cwd']
    if target.startswith('~+') and cwd:
        target = cwd + target[2:]
    if not target.startswith(('/', '~')):
        norm = os.path.normpath(target)
        if norm in ('.', '*') or set(norm.split('/')) == {'..'}:
            return True
    return bool(cwd) and not GLOB.search(target) and (
        os.path.normpath(os.path.join(cwd, os.path.expanduser(target))) == os.path.normpath(cwd))


def rm_decision(args):
    recursive, _, targets = rm_parts(args)
    if any(PROTECTED.hit(t, ancestor=recursive) for t in targets):
        return 'deny', MSG['lake']
    here = os.path.normpath(STATE['cwd'] or '/')
    if recursive and any(whole_cwd(t) and not _temp_path(os.path.normpath(os.path.join(here, os.path.expanduser(t))))
                         for t in targets):
        return 'deny', MSG['rm_cwd']  # whatever the folder is called: a build-folder name does not make its parent disposable
    if recursive and (not targets or not all(disposable(t) or plain_files(t) for t in targets)):
        return 'deny', MSG['rm']
    return None


def destination(args, value_letters):
    s, l, p = flag_parts(args, value_opts={'-t', '--target-directory', '-e', '--rsh', '--exclude', '--include',
                                           '--filter', '-S', '--suffix'}, value_letters=value_letters)
    for k, a in enumerate(args):
        if a == '-t' and k + 1 < len(args):
            return args[k + 1], p
        if a.startswith('--target-directory='):
            return a.split('=', 1)[1], p
    return (p[-1] if len(p) >= 2 else None), p


def inplace_files(prog, args):
    """The files sed -i / perl -i would rewrite: positionals after the script, not the script itself."""
    files, script_seen, k = [], False, 0
    while k < len(args):
        a = args[k]
        if a in ('-e', '--expression', '-f', '--file') or (prog == 'perl' and a == '-E'):
            script_seen = True
            k += 2
            continue
        if a.startswith('-'):
            k += 1
            continue
        if script_seen:
            files.append(a)
        else:
            script_seen = True
        k += 1
    return files


def resolve(path):
    return os.path.normpath(os.path.join(STATE['cwd'] or os.getcwd(), os.path.expanduser(path)))


def is_worktree(src):
    """A mv source that is a linked worktree: <repo>-wt/<name>, .claude/worktrees/<name>, or a folder whose .git is a FILE."""
    if WORKTREE.search(src) or WORKTREE.search(resolve(src)):
        return True
    return os.path.isfile(os.path.join(resolve(src), '.git'))


def move_decision(dest, sources, depth):
    """Bulk or pattern-matched moves that leave their repo; moves inside one repo lose nothing."""
    if not dest or not sources:
        return None
    globbed = any(GLOB.search(s) for s in sources)
    if not globbed and len(sources) < BULK_MOVE:
        return None
    dest_abs = resolve(dest)
    dest_root = common.repo_root(dest_abs if os.path.isdir(dest_abs) else os.path.dirname(dest_abs))
    roots = {common.repo_root(os.path.dirname(resolve(s.split('*')[0] or '.') + os.sep)) for s in sources}
    if dest_root and roots == {dest_root} or re.search(r'(^|/)(_archive|_to_delete)(/|$)', dest):
        return None
    return 'deny', MSG['pattern_move' if globbed else 'bulk_move']


def sql_texts(args):
    texts, k = [], 0
    while k < len(args):
        a = args[k]
        if a.startswith(HEREDOC_MARK):
            try:
                texts.append(STATE['heredocs'][int(a[len(HEREDOC_MARK):])])
            except (ValueError, IndexError):
                pass
        elif a in ('-f', '--file', '<', '-init') and k + 1 < len(args) or a.startswith('--file='):
            name = a.split('=', 1)[1] if a.startswith('--file=') else args[k + 1]
            try:
                with open(resolve(name), encoding='utf-8', errors='replace') as fh:
                    texts.append(fh.read(1 << 20))
            except OSError:
                pass
        else:
            texts.append(a)
        k += 1
    return texts


def sql_decision(prog, args):
    if not any(DESTRUCTIVE_SQL.search(t) for t in sql_texts(args)):
        return None
    if prog in ('sqlite3', 'duckdb', 'litecli'):
        dbs = [a for a in args if not a.startswith('-') and not a.startswith(HEREDOC_MARK) and a != '<'][:1]
        if not dbs or dbs[0] == ':memory:' or disposable(dbs[0]) or not os.path.exists(resolve(dbs[0])):
            return None
    return 'deny', MSG['sql']


def linked_ref(start):
    d = os.path.abspath(os.path.expanduser(start or '.'))
    while True:
        try:
            with open(os.path.join(d, 'supabase', '.temp', 'project-ref')) as fh:
                return fh.read().strip()
        except OSError:
            pass
        parent = os.path.dirname(d)
        if parent == d:
            return ''
        d = parent


def supabase_decision(args):
    words = [a for a in args if not a.startswith('-')]
    if tuple(words[:2]) not in SUPABASE_SCHEMA:
        return None
    workdir = next((args[k + 1] for k, a in enumerate(args[:-1]) if a == '--workdir'), None)
    refs = {linked_ref(os.path.join(STATE['cwd'] or os.getcwd(), workdir) if workdir else STATE['cwd'] or os.getcwd())}
    refs |= {r for r in PROTECTED_SUPABASE if any(r in a for a in args)}
    hit = sorted(refs & set(PROTECTED_SUPABASE))
    if not hit:
        return None
    note = PROTECTED_SUPABASE[hit[0]]
    return 'deny', MSG['supabase'] % (hit[0], (': ' + note) if note else '')


def expand(tok):
    return VAR_REF.sub(lambda m: STATE['vars'].get(m.group(1), m.group(0)), tok)


def env_hit(token):
    """True when token names an env file (.env, .env.local ...) or a glob that matches one; .env.example and the other
    safe suffixes are fine. A host:path (scp) is judged by its path."""
    base = os.path.basename((token.rsplit(':', 1)[-1] if ':' in token else token).rstrip('/'))
    if GLOB.search(base):
        return base.startswith('.') and any(fnmatch.fnmatchcase(n, base) for n in ENV_SAMPLES)
    return bool(ENV_FILE.match(base)) and not ENV_SAFE.search(base)


def env_hint(token):
    """True when a token of an upstream pipeline command points at env files: .env, .env.*, a glob for them, or env."""
    return env_hit(token) or token == 'env' or bool(re.search(r'\.env', token) and not ENV_SAFE.search(token))


def listed_names(cmd):
    """(tokens, patterns) of an upstream pipeline command. A content search that only lists matching files (grep -l,
    rg -l, git grep -l) prints names it found, never its pattern, so the pattern moves to the second list, where only a
    pattern that looks like an env file name still counts. Anything unclear drops nothing."""
    prog, args = os.path.basename(cmd[0]), cmd[1:]
    if prog == 'git':
        k = 0
        while k < len(args) and args[k].startswith('-'):
            k += 2 if args[k] in GIT_VALUE else 1
        if k >= len(args) or args[k] != 'grep':
            return cmd[1:], []
        prog, args = 'grep', args[k + 1:]
    if prog not in SEARCH_LETTERS:
        return cmd[1:], []
    s, l, p = flag_parts(args, value_opts=SEARCH_VALUE | PATTERN_OPTS, value_letters=SEARCH_LETTERS[prog])
    if not (s & {'l'} or l & {'--files-with-matches', '--name-only'}) or '--files' in l:
        return cmd[1:], []  # prints matching lines, filters its input by the pattern, or lists files without searching
    patterns = [a.split('=', 1)[1] for a in args if a.startswith('--regexp=')]
    patterns += [args[k + 1] for k, a in enumerate(args[:-1]) if a in ('-e', '--regexp')]
    given = s & {'e', 'f'} or any(a in PATTERN_OPTS or a.startswith(('--file=', '--expression=')) for a in args)
    if not given and not patterns and p:
        patterns = p[:1]  # no -e/-f: the first operand is the pattern
    names = list(args)
    for pat in patterns:
        for tok in ('--regexp=' + pat, pat):
            if tok in names:
                names.remove(tok)
                break
    return names, patterns


def pattern_env(pattern):
    """True when a search pattern reads as an env file name: .env, ^\\.env, env (process.env and import.meta.env do not)."""
    plain = re.sub(r'[\\^$]', '', pattern)
    return env_hit(pattern) or env_hit(plain) or plain == 'env' or bool(re.search(r'(?<![A-Za-z0-9_])\.env', plain))


def feeds_env(cmd):
    """True when an upstream pipeline command names env files for the reader after it."""
    names, patterns = listed_names(cmd)
    return any(env_hint(t) for t in names) or any(pattern_env(q) for q in patterns)


def reads_env(prog, args):
    """True when a reader program is given an env file (or a glob matching one) to read."""
    if prog == 'sed' and any(a == '--in-place' or a.startswith('--in-place=') or
                             (a.startswith('-') and not a.startswith('--') and 'i' in a[1:]) for a in args):
        return False  # sed -i edits in place and prints nothing; the protected-folder check covers it
    skip_first = prog in PATTERN_FIRST and not any(a in PATTERN_OPTS or a.startswith(('--regexp=', '--file=',
                                                                                      '--expression=')) for a in args)
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a in FILE_FLAGS and k < len(args):  # -g .env*, --include .env, -f patterns: the value names files
            if env_hit(args[k]):
                return True
            k += 1
        elif a.startswith('-') and a != '-':
            if a.startswith('--') and '=' in a and env_hit(a.split('=', 1)[1]):
                return True
        elif skip_first:
            skip_first = False  # grep/rg/awk/sed: the first operand is the pattern or script, not a file
        elif env_hit(a):
            return True
    return False


def change_dir(args):
    """Track cd/pushd. The folder becomes unknown (None) for cd -, $VAR, a glob, ~user, a relative path from an unknown
    folder, and a folder that does not exist unless && makes sure the rest only runs after a good cd."""
    target = next((a for a in args if a == '-' or not a.startswith('-')), None)
    if target is None:
        new = common.HOME
    elif target == '-' or GLOB.search(target) or '$' in target or '`' in target or re.match(r'^~[^/]', target):
        new = None
    else:
        path = os.path.expanduser(target)
        if path.startswith('/'):
            new = os.path.normpath(path)
        elif STATE['cwd'] is None:
            new = None
        else:
            new = os.path.normpath(os.path.join(STATE['cwd'], path))
    if new and STATE['sep'] != '&&' and not os.path.isdir(new):
        new = None  # a failed cd leaves the old folder in place, and `;` runs the next command anyway
    STATE['cwd'] = new


def read_text(path):
    try:
        with open(resolve(path), encoding='utf-8', errors='replace') as fh:
            return fh.read(1 << 20)
    except (OSError, TypeError):
        return None


def heredoc_body(marker):
    try:
        return STATE['heredocs'][int(marker[len(HEREDOC_MARK):])]
    except (ValueError, IndexError):
        return None


def piped_text(prev):
    """The text the previous command of a pipeline prints, when that is knowable (echo, printf, cat), else None."""
    prev = strip_prefix(prev)
    if not prev:
        return None
    prog, (plain, redirs) = os.path.basename(prev[0]), split_redirects(prev[1:])
    if prog == 'echo':
        return ' '.join(a for a in plain if not re.match(r'^-[neE]+$', a)).replace('\\n', '\n')
    if prog == 'printf':
        return '\n'.join(plain).replace('\\n', '\n')
    if prog == 'cat':
        texts = [read_text(f) for f in plain if not f.startswith('-')]
        for op, target in redirs:
            if op == '<<' and target.startswith(HEREDOC_MARK):
                texts.append(heredoc_body(target))
            elif op == '<<<':
                texts.append(target)
            elif op == '<':
                texts.append(read_text(target))
        if texts and None not in texts:
            return '\n'.join(texts)
    return None


def sub_script(text, depth):
    """Judge text as a script run by a child shell: a cd or variable set there does not reach the caller."""
    saved = (STATE['cwd'], dict(STATE['vars']))
    try:
        return check_script(text, depth)
    finally:
        STATE['cwd'], STATE['vars'] = saved


def shell_decision(args, depth):
    """bash/sh/zsh: -c takes a script; without it the script comes from a heredoc, here-string, file or the pipe."""
    for k, a in enumerate(args[:-1]):
        if a == '-c' or (a.startswith('-') and not a.startswith('--') and 'c' in a[1:]):
            return sub_script(args[k + 1], depth + 1)
    plain, redirs = split_redirects(args)
    if any(a.startswith('<(') for a in plain):
        return 'deny', MSG['pipe_shell']
    operands = [a for a in plain if not a.startswith('-')]
    from_stdin = not operands or any(a == '-' or (a.startswith('-') and not a.startswith('--') and 's' in a[1:])
                                     for a in plain)
    if not from_stdin:
        return None
    texts = []
    for op, target in redirs:
        if op == '<<' and target.startswith(HEREDOC_MARK):
            texts.append(heredoc_body(target))
        elif op == '<<<':
            texts.append(target)
        elif op == '<':
            texts.append(read_text(target))
    if not texts and STATE['pipe']:
        text = piped_text(STATE['pipe'][-1])
        if text is None:
            return 'deny', MSG['pipe_shell']
        texts.append(text)
    for text in texts:
        decision = sub_script(text, depth + 1) if text else None
        if decision:
            return decision
    return None


def python_target(args):
    """('file' | 'module', name, the arguments after it) for a python command line, or (None, '', [])."""
    k = 0
    while k < len(args):
        a = args[k]
        if a in ('-W', '-X', '-Q'):
            k += 2
        elif a == '-m':
            return 'module', args[k + 1] if k + 1 < len(args) else '', args[k + 2:]
        elif a == '-c':
            return None, '', []
        elif a.startswith('-') and a != '-':
            k += 1
        else:
            return 'file', a, args[k + 1:]
    return None, '', []


def applying(prog_args):
    """True when a python command line runs the kit's apply.py with something that approves or completes proposals."""
    kind, name, rest = python_target(prog_args)
    if not kind:
        return False
    cwd = STATE['cwd'] if STATE['cwd'] is not None else os.getcwd()
    cwd = cwd or os.getcwd()
    core, kit_bin = os.path.realpath(common.CORE), os.path.realpath(os.path.join(common.CORE, 'bin'))
    if kind == 'module':
        is_apply = name == 'apply' and _inside(os.path.realpath(cwd), kit_bin)
    else:
        full = os.path.realpath(resolve(name))
        is_apply = os.path.basename(name) == 'apply.py' and (
            name.endswith(os.path.join('bin', 'apply.py')) or _inside(full, kit_bin) or _inside(os.path.realpath(cwd), core))
    if not is_apply:
        return False
    for a in rest:
        flag = a.split('=', 1)[0]
        # argparse takes any unique prefix: --a, --ap, --appr ... are --approve; --d, --do, --don are --done
        if len(flag) >= 3 and flag.startswith('--') and ('--approve'.startswith(flag) or '--done'.startswith(flag)):
            return True
    return False


def analyze(toks, depth):
    assigns = [t for t in toks if ASSIGN.match(t)]
    if assigns and len(assigns) == len(toks):
        for t in assigns:
            k, v = t.split('=', 1)
            STATE['vars'][k] = expand(v)
        return None
    STATE['envcfg'] = env_config(toks) if any('GIT_CONFIG' in t for t in toks) or any(
        k.startswith('GIT_CONFIG') for k in STATE['vars']) else []
    toks = [expand(t) for t in strip_prefix(toks)]
    if not toks:
        return None
    if toks[0] in ('export', 'declare', 'typeset', 'local', 'readonly'):
        for t in toks[1:]:
            if ASSIGN.match(t):
                k, v = t.split('=', 1)
                STATE['vars'][k] = expand(v)
        return None
    if toks[0] in ('cd', 'pushd'):
        change_dir(toks[1:])
        return None
    if toks[0] == 'popd':
        STATE['cwd'] = None
        return None
    for k, t in enumerate(toks[:-1]):
        if t in REDIRECTS and PROTECTED.hit(toks[k + 1]):
            return 'deny', MSG['lake']
    prog, args = os.path.basename(toks[0]), toks[1:]
    if prog in SHELLS:
        return shell_decision(args, depth)
    if prog == 'eval':
        return check_script(' '.join(args), depth + 1)
    if prog == 'xargs':
        k, files = 0, []
        while k < len(args) and args[k].startswith('-'):
            if args[k] in ('-a', '--arg-file') and k + 1 < len(args):
                files.append(args[k + 1])
            elif args[k].startswith('--arg-file='):
                files.append(args[k].split('=', 1)[1])
            k += 2 if args[k] in XARGS_VALUE or args[k] == '--arg-file' else 1
        if any(env_hit(f) for f in files):
            return 'deny', MSG['env']
        inner = os.path.basename(args[k]) if k < len(args) else ''
        if inner == 'mv':
            return 'deny', MSG['pattern_move']
        if inner in ('rm', 'unlink') and rm_parts(args[k + 1:])[0]:
            return 'deny', MSG['find_delete']
        if inner in READERS and any(feeds_env(c) for c in STATE['pipe'] if c):
            return 'deny', MSG['env']  # something that names an env file feeds the reader through the pipe
        return analyze(args[k:], depth + 1) if depth < MAX_DEPTH else ('deny', MSG['deep'])
    if prog == 'git':
        return git_decision(args)
    if prog == 'gh':
        return gh_decision(args)
    if prog in HTTP_CLIENTS and gate_branches() and any(API_MERGE.search(a) for a in args):
        return 'deny', MSG['api_merge']
    if prog == 'supabase' or (prog in ('npx', 'pnpx', 'bunx') and args[:1] and args[0].startswith('supabase')):
        return supabase_decision(args if prog == 'supabase' else args[1:])
    if prog in SQL_CLIENTS or (prog == 'wrangler' and args[:2] == ['d1', 'execute']):
        decision = sql_decision(prog, args)
        if decision or prog not in ('sqlite3', 'duckdb'):
            return decision
    if prog == 'rm':
        return rm_decision(args)
    if prog == 'find':
        if '-delete' in args or any(a in ('-exec', '-execdir', '-ok') and k + 1 < len(args) and
                                    os.path.basename(args[k + 1]) in ('rm', 'unlink', 'shred')
                                    for k, a in enumerate(args)):
            return 'deny', MSG['find_delete']
        if any(a in ('-exec', '-execdir') and k + 1 < len(args) and os.path.basename(args[k + 1]) == 'mv'
               for k, a in enumerate(args)):
            return 'deny', MSG['pattern_move']
        return None
    if prog == 'mv':
        dest, p = destination(args, 'tS')
        sources = [x for x in p if x != dest]
        if any(is_worktree(x) for x in sources):
            return 'deny', MSG['mv_wt']
        if any(PROTECTED.hit(x, ancestor=True) for x in sources) or (dest and PROTECTED.hit(dest)):
            return 'deny', MSG['lake']
        return move_decision(dest, sources, depth)
    if prog in ('cp', 'rsync', 'ditto', 'install', 'scp'):
        dest, p = destination(args, 'te' if prog == 'rsync' else ('PioFclSJ' if prog == 'scp' else 't'))
        if prog != 'scp' and dest and PROTECTED.hit(dest):
            return 'deny', MSG['lake']
        if any(env_hit(x) for x in p if x != dest):
            return 'deny', MSG['env']
        return None
    if prog in LAKE_WRITERS:
        return ('deny', MSG['lake']) if any(PROTECTED.hit(a) for a in args if not a.startswith('-')) else None
    if prog == 'ln':
        p = flag_parts(args, value_opts={'-t', '--target-directory', '-S', '--suffix'}, value_letters='tS')[2]
        dest = next((args[k + 1] for k, a in enumerate(args[:-1]) if a == '-t'), p[-1] if len(p) >= 2 else None)
        return ('deny', MSG['lake']) if dest and PROTECTED.hit(dest) else None
    if prog in ('sed', 'perl') and any(a.startswith('-i') or (prog == 'perl' and a.startswith('-p') and 'i' in a)
                                       for a in args):
        if any(PROTECTED.hit(f) for f in inplace_files(prog, args)):
            return 'deny', MSG['lake']
    if prog in ('sqlite3', 'duckdb'):
        p = flag_parts(args, value_opts={'-cmd', '-init', '-separator', '-nullvalue', '-newline', '-vfs'})[2]
        readonly = '-readonly' in args or '--readonly' in args or bool(p and re.search(r'[?&](mode=ro|immutable=1)', p[0]))
        if p and PROTECTED.hit(p[0]) and not PROTECTED.backup(p[0]) and not readonly:
            sql = ' '.join(p[1:])
            if not sql or DB_WRITE.search(sql) or any(a.startswith('<') for a in args):
                return 'deny', MSG['lake']
        if any(PROTECTED.hit(m.group(1)) and not PROTECTED.backup(m.group(1))
               for a in p[1:] for m in DB_DUMP_TO.finditer(a)):
            return 'deny', MSG['lake']
        return None
    if prog in READERS:
        return ('deny', MSG['env']) if reads_env(prog, args) else None
    if prog.startswith('python') and applying(args):
        return 'deny', MSG['apply']
    return None


class TooDeep(Exception):
    """Quotes and substitutions nested beyond what the scanner follows."""


def _close_paren(text, i, level=0):
    """Index of the ) that closes a $( whose body starts at i (len(text) when it never closes)."""
    if level > 3 * MAX_DEPTH:
        raise TooDeep()
    depth, n = 1, len(text)
    while i < n:
        ch = text[i]
        if ch == '\\':
            i += 2
            continue
        if ch == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if ch == '"':
            i = _skip_double(text, i + 1, level + 1)
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _skip_double(text, i, level=0):
    """Index just after the " that closes a double-quoted string whose body starts at i."""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '\\':
            i += 2
        elif ch == '"':
            return i + 1
        elif text.startswith('$(', i):
            i = _close_paren(text, i + 2, level + 1) + 1
        else:
            i += 1
    return n


def substitutions(text):
    """The inner text of every $( ) and backtick command substitution bash would run, also inside double quotes."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '\\':
            i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif text.startswith('$(', i):
            end = _close_paren(text, i + 2)
            out.append(text[i + 2:end])
            i = end + 1
        elif ch == '`':
            j = i + 1
            while j < n and text[j] != '`':
                j += 2 if text[j] == '\\' else 1
            out.append(re.sub(r'\\([`$\\])', r'\1', text[i + 1:j]))
            i = j + 1
        elif ch == '"':
            j = i + 1  # inside double quotes substitutions still run: scan to the closing quote
            while j < n and text[j] != '"':
                if text[j] == '\\':
                    j += 2
                elif text.startswith('$(', j):
                    end = _close_paren(text, j + 2)
                    out.append(text[j + 2:end])
                    j = end + 1
                elif text[j] == '`':
                    k = j + 1
                    while k < n and text[k] != '`':
                        k += 2 if text[k] == '\\' else 1
                    out.append(re.sub(r'\\([`$\\])', r'\1', text[j + 1:k]))
                    j = k + 1
                else:
                    j += 1
            i = j + 1
        else:
            i += 1
    return out


def heredoc_substitutions(norm):
    """Substitutions in the bodies of unquoted-delimiter heredocs named in norm: those run even though the text is data."""
    out = []
    for n in re.findall(HEREDOC_MARK + r'(\d+)', norm):
        n = int(n)
        if n < len(STATE['expands']) and STATE['expands'][n]:
            out += substitutions(STATE['heredocs'][n])
    return out


def check_tokens(tokens, depth, norm=''):
    """Judge each simple command in order, first decision wins. ( ) groups, pipeline stages and background jobs run in a
    child shell, so a cd or assignment there is undone afterwards. norm is the text the tokens came from: the command
    substitutions in it are scanned for branch changes right before the simple command they belong to."""
    stack, pipe = [], []
    events = split_commands(tokens)
    segs = top_segments(norm)
    per_command = len(segs) == sum(1 for e in events if e[0] == 'cmd')
    if not per_command:
        scan_moves(norm)  # cannot tell which command holds which: take them all up front
    seg = iter(segs)
    try:
        for kind, cmd, before, after in events:
            if kind == 'open':
                stack.append((STATE['cwd'], dict(STATE['vars'])))
                pipe = []
            elif kind == 'close':
                if stack:
                    STATE['cwd'], STATE['vars'] = stack.pop()
                pipe = [['<group>']]
            else:
                if STATE['undo'] and before != '&&':
                    restore_moves()
                if per_command:
                    scan_moves(next(seg))
                if before not in PIPES:
                    pipe = []
                forked = before in PIPES or after in PIPES or after == '&'
                saved = (STATE['cwd'], dict(STATE['vars'])) if forked else None
                STATE['pipe'], STATE['sep'], STATE['before'] = [list(c) for c in pipe], after, before
                try:
                    decision = analyze(cmd, depth)
                finally:
                    STATE['plain'] = False  # only the first command of the script may rely on PLAIN_RM
                    if saved:
                        STATE['cwd'], STATE['vars'] = saved
                if decision:
                    return decision
                pipe.append(cmd)
    finally:
        if depth == 0:
            restore_moves()
        STATE['envcfg'] = []
        if stack:
            STATE['cwd'], STATE['vars'] = stack[0]
    return None


def top_segments(text):
    """The text of each simple command of text, split at ; & | and line breaks outside quotes and substitutions, without
    the pieces that hold no command ({ } !)."""
    out, start, i, n = [], 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '\\':
            i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif ch == '"':
            i = _skip_double(text, i + 1)
        elif text.startswith('$(', i):
            i = _close_paren(text, i + 2) + 1
        elif ch == '`':
            j = i + 1
            while j < n and text[j] != '`':
                j += 2 if text[j] == '\\' else 1
            i = j + 1
        elif ch in ';&|\n()':
            out.append(text[start:i])
            start = i = i + 1
        else:
            i += 1
    out.append(text[start:])
    return [x for x in out if x.strip() and x.strip() not in ('{', '}', '!')]


def scan_moves(norm, depth=0):
    """A command substitution runs before the command it sits in: note the branch changes in it."""
    if depth > MAX_DEPTH:
        return
    for inner in substitutions(norm) + heredoc_substitutions(norm):
        text = normalize(inner)
        for cmd in simple_commands(tokens_of(text)):
            cmd = strip_prefix(cmd)
            head = git_head(cmd[1:]) if cmd and os.path.basename(cmd[0]) == 'git' else None
            if head:
                note_move(head[1], head[2], head[3], head[0])
            elif cmd and cmd[0] == 'gh' and gh_split(cmd[1:])[0][:2] == ['pr', 'checkout']:
                mark_moved(STATE['cwd'] or os.getcwd(), 'unknown')
        scan_moves(text, depth + 1)


def check_script(text, depth=0):
    if depth > MAX_DEPTH:
        return 'deny', MSG['deep']
    if depth == 0:
        STATE['moved'], STATE['undo'] = {}, []
    try:
        norm = normalize(text)
        if depth == 0:
            STATE['plain'] = bool(PLAIN_RM.match(text))
        decision = check_tokens(tokens_of(norm), depth, norm)
        if decision:
            return decision
        for inner in substitutions(norm) + heredoc_substitutions(norm):
            decision = sub_script(inner, depth + 1)
            if decision:
                return decision
    except (TooDeep, RecursionError):
        return 'deny', MSG['deep']
    return None


RM_HEAD = re.compile(r'^\s*rm((\s+(-[A-Za-z]+|--[a-z-]+))*)(\s+--)?(?=\s+\S)')


def trash_rewrite(text):
    """A lone recursive rm of plain literal paths becomes a trash.py call with the same arguments; None otherwise.
    The rewrite is returned as "allow", so it must not run code: every target has to be letters, digits and . _ - / ~ + @ : ,
    (spaces only inside quotes). No $, backtick, parentheses, ;, |, &, <, >, *, ? or line break."""
    if '\n' in text.strip() or HEREDOC.search(text):
        return None
    cmds = list(simple_commands(tokens_of(normalize(text))))
    if len(cmds) != 1 or cmds[0][0] != 'rm':
        return None
    recursive, force, targets = rm_parts(cmds[0][1:])
    if not recursive or not targets or all(disposable(t) for t in targets) or any(PROTECTED.hit(t, True) for t in targets):
        return None
    m = RM_HEAD.match(text)
    if not m:
        return None
    tail = text[m.end():]
    if not PLAIN_TAIL.match(tail) or tokens_of(tail) != targets:
        return None
    return TRASH + (' -f' if force else '') + ' --' + tail


def check_sql_tool(tool_input):
    sql = ' '.join(str(tool_input.get(k) or '') for k in ('query', 'sql', 'statement', 'migration'))
    return ('deny', MSG['sql']) if DESTRUCTIVE_SQL.search(sql) else None


ADVISOR_MODEL = 'fable'  # claude/agents/advisor.md


def check_agent(tool_input):
    """Ask the user before a subagent runs on a gated model (config gated_models), including the advisor agent while its
    model is gated. Removing fable from gated_models lets the advisor run without the prompt. The only "ask" in the guard."""
    kind = str(tool_input.get('subagent_type') or '').lower()
    model = str(tool_input.get('model') or '').lower()
    gated = [g.lower() for g in _listed(common.config().get('gated_models'))]
    hit = next((g for g in gated if g in model), None)
    if hit is None and kind == 'advisor':
        hit = next((g for g in gated if g in ADVISOR_MODEL), None)
    return ('ask', MSG['gated'] % hit) if hit else None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    try:
        tool = data.get('tool_name') or ''
        tool_input = data.get('tool_input') or {}
        result = None
        if tool == 'Bash':
            STATE['cwd'] = str(data.get('cwd') or os.getcwd())
            command = str(tool_input.get('command') or '')
            result = check_script(command)
            if result and result[1] == MSG['rm']:
                STATE['cwd'] = str(data.get('cwd') or os.getcwd())
                new = trash_rewrite(command)
                if new:
                    updated = dict(tool_input, command=new)
                    print(json.dumps({'hookSpecificOutput': {
                        'hookEventName': 'PreToolUse', 'permissionDecision': 'allow', 'updatedInput': updated,
                        'permissionDecisionReason': 'agent-core: recursive delete rewritten to move into the trash: '
                                                    + new}}))
                    return
        elif tool in ('Agent', 'Task'):
            result = check_agent(tool_input)
        elif SQL_MCP.match(tool):
            result = check_sql_tool(tool_input)
        if result:
            decision, why = result
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': decision,
                                                     'permissionDecisionReason': 'agent-core: ' + why}}))
    except Exception as exc:
        common.log_error('guard', exc)


if __name__ == '__main__':
    main()
