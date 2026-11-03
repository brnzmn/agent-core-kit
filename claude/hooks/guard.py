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
merging a PR into a gated branch before its review gate passes. A documented provider outage can use
one real passing review plus passing tests on the same head when config pr_gate enables that fallback.
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
WRAPPER_VALUE['sudo'].update({'--user', '--group', '--host', '--prompt', '--close-from', '--chdir', '--chroot',
                              '--command-timeout', '--other-user', '--role', '--type'})
WRAPPER_VALUE['env'].update({'--unset', '--chdir', '--split-string'})
WRAPPER_VALUE['nice'].add('--adjustment')
WRAPPER_VALUE['timeout'].update({'--signal', '--kill-after'})
KEYWORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until'}  # a command can follow these on the same line
SHELL_WHITESPACE = ' \t\n'  # shell word separators: CR and Unicode spaces remain literal word characters
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
STATE = {'cwd': '', 'cwd_candidates': None, 'heredocs': [], 'expands': [], 'vars': {}, 'restricted': set(), 'unknown': set(),
         'pipe': [], 'sep': ';', 'plain': False, 'deferred_body': False, 'body_file_unstable': False,
         'raw': [], 'moved': False, 'envcfg': [], 'inherit': [], 'penv': {}, 'pinherit': {}, 'xargs': False,
         'marks': {'active': set(), 'unquoted': set(), 'braced': set()}}  # cwd None = unknown; raw = the command's tokens before $VAR expansion
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
STRICT_REF = re.compile(r'\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})')
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
GH_BUDGET = 7.0  # seconds from start for all gh calls together; leaves room for startup and the kill below


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
        self.names = [_fold(name).split(os.sep) for name in sorted(names)]
        self.tails = sorted({'/' + '/'.join(q.split('/')[-3:]) + '/' for q in self.paths})
        body = '|'.join(re.escape(n) for n in sorted(names))
        self.name_re = re.compile(r'(^|[/=:])(' + body + r')(/|$)') if names else None
        self.backup_re = re.compile(r'(^|[/=:])(' + body + r')/backups/') if names else None

    @staticmethod
    def _candidates(token):
        found = {token}
        if '=' in token:
            found.add(token.split('=', 1)[1])
        candidates = []
        for value in found:
            if value.startswith('file:') or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', value):
                value = value.split('?', 1)[0]
            value = re.sub(r'^file:(//)?', '', value)
            if value:
                candidates.append(value)
        return candidates

    @staticmethod
    def _absolute(c, literal=False):
        """c as an absolute path, or None when a variable, substitution or unknown folder stops us resolving it."""
        if not literal:
            c = re.sub(r'^\$\{?HOME\}?(?=/|$)', common.HOME, c)
            if '$' in c or '`' in c:
                return None
        c = os.path.expanduser(c)
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
        if (self.paths or self.name_re) and getattr(token, 'path_uncertain', False):
            return True
        if self.name_re and self.name_re.search(token):
            return True
        if not self.paths and not self.name_re:
            return False
        for c in self._candidates(token):
            full = self._absolute(c, literal=isinstance(token, ExpandedOperand) and not token.path_uncertain)
            if full is None:
                bases = STATE['cwd_candidates']
                literal = isinstance(token, ExpandedOperand) and not token.path_uncertain
                if (STATE['cwd'] is None and bases is not None and
                        (literal or not re.search(r'[$`]', c)) and not os.path.isabs(os.path.expanduser(c))):
                    for base in bases:
                        candidate = ExpandedOperand(os.path.join(base, os.path.expanduser(c)))
                        candidate.path_uncertain = False
                        if self.hit(candidate, ancestor):
                            return True
                    continue
                if STATE['cwd'] is None or self._tail_hit(c):
                    return True
                continue
            if self.name_re and self.name_re.search(full):
                return True
            if GLOB.search(full):
                pat = _fold(os.path.normpath(full)).split(os.sep)
                if self.names:
                    for count, match in enumerate(glob.iglob(full)):
                        for resolved in (match, os.path.realpath(match)):
                            parts = _fold(os.path.normpath(resolved)).split(os.sep)
                            for name in self.names:
                                if any(parts[start:start + len(name)] == name for start in range(len(parts))):
                                    return True
                                if ancestor and any(parts[-n:] == name[:n] and os.path.lexists(
                                        os.path.join(resolved, *name[n:])) for n in range(1, len(name))):
                                    return True
                        if count >= 255:
                            return True  # a larger expansion cannot be established safe during this hook
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
            full = self._absolute(c, literal=isinstance(token, ExpandedOperand) and not token.path_uncertain)
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
    'alias': ('An alias set on the command line (-c alias.*, GIT_CONFIG_*) can run any git command, a force push to main '
              'included. Run the real git command instead.'),
    'stash': 'Dropping stashes is blocked. Leave them; list them in your final message.',
    'worktree': 'Removing or pruning worktrees is blocked. Use the worktree skill: git worktree move into _archive/.',
    'rewrite': 'History rewrites are blocked. Add a new commit instead.',
    'fake_verdict': ('Review verdicts and availability/test evidence count toward the merge gate, so only bin/pr_review.py '
                     'posts them. Run: ' + PR_REVIEW + ' <pr> auto.'),
    'repo_delete': 'Deleting a GitHub repo is blocked; ' + USER + ' deletes repos. Name the repo in your final message.',
    'review_wait': ('PR #%s goes into %s, a gated branch of %s. The review gate is incomplete for head %s. Missing: %s. '
                    'Run ' + _esc(PR_REVIEW) + ' %s auto from a checkout of the head. A configured provider-outage '
                    'fallback needs one real PASS and passing tests; a BLOCK still holds the merge.'),
    'review_block': ('PR #%s: the %s review of head %s says VERDICT: BLOCK. Fix the findings, push, and review the new '
                     'head before merging.'),
    'review_unread': 'gh pr merge is held: the PR\'s reviews could not be read (%s). Retry once gh works.',
    'api_merge': ('gh api cannot merge a PR around the review gate (pr_gate in config). Merge with gh pr merge <number>, '
                  'which checks the review and provider-outage evidence.'),
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
    'deep': ('The guard cannot reliably read this shell script, its syntax or its nested commands. '
             'Split it into separate, plain commands.'),
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
    # not -A/-u: the hook allows them with named paths (git add -A src/a.py), and a prefix rule can't see the paths
    (['git', 'add', ['.', './', ':/']], 'stage'),
    (['git', 'stage', ['.', './', ':/']], 'stage'),
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
        if e[0] in 'xuU':
            if len(e) == 1:
                raise TooDeep('incomplete ANSI-C escape')
            codepoint = int(e[1:], 16)
            if not 0 < codepoint <= 0x10ffff or 0xd800 <= codepoint <= 0xdfff:
                raise TooDeep('invalid ANSI-C character')
            return chr(codepoint)
        if e[0].isdigit():
            return chr(int(e, 8) & 0xFF)
        return plain.get(e, '\\' + e)
    return re.sub(r'\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|[0-7]{1,3}|.)', one, body, flags=re.S)


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
                out.append("'" + _ansi_c(text[i + 1:j]).replace("'", "'\"'\"'") + "'")
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


class ShellToken(str):
    """A decoded word with the quoting of this occurrence; equal text can have different shell syntax."""
    quoted = None
    assignment = None
    expansion_sites = None
    unquoted_expansion = None
    unquoted_pattern = False
    substitution_scripts = ()
    process_substitution = False



class ExpandedOperand(str):
    """An already-expanded argument, retaining the exact source occurrence and path uncertainty."""
    expansion_sites = frozenset()
    unquoted_expansion = False


def operand_value(value, source):
    token = ExpandedOperand(value)
    token.source_operand = getattr(source, 'source_operand', source)
    token.path_uncertain = unresolved_path(value, source)
    return token


def shell_keyword(word, choices=KEYWORDS):
    return word in choices and getattr(word, 'quoted', None) is False


def shell_assignment(word):
    return ASSIGN.match(word) and getattr(word, 'assignment', None) is True


def tokens_of(text):
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace = SHELL_WHITESPACE
        lex.whitespace_split = True
        lex.commenters = ''
        tokens = [ShellToken(word) for word in lex]
        note_quoting(text, tokens, record_marks=False)
        return tokens
    except ValueError:
        return []  # unbalanced quotes: bash refuses to run it, so there is nothing to judge


def note_quoting(text, tokens, record_marks=True):
    """Record in STATE['marks'] which words of the script (as shlex decodes them) hold a $ or backtick that is not
    single-quoted or escaped ('active'), one that is not quoted at all ('unquoted'), or an unquoted brace expansion
    ('braced'). Keep quoting per token occurrence as well, so quoted reserved words never open or close shell scopes.
    When its words do not match the tokenizer's, every word counts as all three and keyword identity stays unknown."""
    words, cur, i, n = [], None, 0, len(text)
    while i < n:
        c = text[i]
        i += 1
        if c in SHELL_WHITESPACE or c in ';&|()<>':
            if cur is not None:
                words.append(cur)
            cur = None
            if c not in SHELL_WHITESPACE:
                start = i - 1
                while i < n and text[i] in ';&|()<>':
                    i += 1
                words.append([text[start:i], False, False, '', False, False, set()])
            continue
        if cur is None:
            cur = ['', False, False, '', False, False, set()]  # decoded, active, unquoted, bare, quoted, NAME=, sites
        if c == '\\' and i < n:
            cur[4] = True
            cur[0] += text[i]
            i += 1
        elif c == "'":
            cur[4] = True
            j = text.find("'", i)
            j = n if j < 0 else j
            cur[0] += text[i:j]
            i = j + 1
        elif c == '"':
            cur[4] = True
            while i < n and text[i] != '"':
                if text[i] == '\\' and i + 1 < n:  # shlex drops the backslash only before " and \; bash also lets it escape $ and `
                    cur[0] += text[i + 1] if text[i + 1] in '"\\' else text[i:i + 2]
                    i += 2
                    continue
                if text[i] in '$`':
                    cur[1] = True
                    cur[6].add(len(cur[0]))
                cur[0] += text[i]
                i += 1
            i += 1
        else:
            if c == '=' and not cur[4] and ASSIGN.match(cur[0] + '='):
                cur[5] = True
            if c in '$`':
                cur[1] = cur[2] = True
                cur[6].add(len(cur[0]))
            cur[0] += c
            cur[3] += c
    if cur is not None:
        words.append(cur)
    marks = STATE['marks']
    if [w[0] for w in words] != tokens:
        if record_marks:
            for k in marks:
                marks[k].update(tokens)
        return
    for token, (word, active, unquoted, bare, quoted, assignment, sites) in zip(tokens, words):
        token.quoted = quoted
        token.assignment = assignment
        token.expansion_sites = frozenset(sites)
        token.unquoted_expansion = unquoted
        token.unquoted_pattern = bool(GLOB.search(bare) or ('{' in bare and (',' in bare or '..' in bare)))
        if not record_marks:
            continue
        if active:
            marks['active'].add(word)
        if unquoted:
            marks['unquoted'].add(word)
        if '{' in bare and (',' in bare or '..' in bare):
            marks['braced'].add(word)



def simple_commands(tokens):
    cmd = []
    for t in tokens:
        if getattr(t, 'quoted', False) is not True and (t in SEPARATORS or (t and all(c in ';&|()' for c in t))):
            if cmd:
                yield cmd
            cmd = []
        else:
            cmd.append(t)
    if cmd:
        yield cmd


def _split_commands(tokens):
    """[(kind, tokens, separator before, separator after)]; kind is 'cmd', or 'open'/'close' for a ( ) group."""
    events, cmd, prev = [], [], ';'

    def flush(nxt):
        nonlocal cmd, prev
        if cmd:
            events.append(('cmd', cmd, prev, nxt))
        cmd, prev = [], nxt

    for t in tokens:
        quoted = getattr(t, 'quoted', False) is True
        run = not quoted and bool(t) and all(c in ';&|()' for c in t)
        if not quoted and t in SEPARATORS and not run:  # { } !
            flush(t)
        elif run:
            for s in SEP_PART.findall(t):
                if s == '(':
                    entry = prev if not cmd else '('
                    flush('(')
                    events.append(('open', [], entry, '('))
                elif s == ')':
                    flush(')')
                    events.append(('close', [], ')', ')'))
                else:
                    flush(s)
        else:
            cmd.append(t)
    flush(';')
    return events


def split_commands(tokens):
    """Keep function bodies deferred instead of flattening their commands into the defining shell."""
    words = []
    for token in tokens:
        if getattr(token, 'quoted', False) is not True and token and all(c in ';&|()' for c in token):
            words.extend(ShellToken(part) for part in SEP_PART.findall(token))
        else:
            words.append(token)
    events, start, i = [], 0, 0
    while i < len(words):
        j = i + 1 if shell_keyword(words[i], {'function'}) else i
        if j >= len(words) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', words[j]):
            i += 1
            continue
        j += 1
        parentheses = (words[j:j + 2] == ['(', ')']
                       and all(getattr(w, 'quoted', False) is not True for w in words[j:j + 2]))
        if parentheses:
            j += 2
        while j < len(words) and words[j] == ';':  # a newline may precede the body
            j += 1
        if (j >= len(words) or words[j] not in ('{', '(') or getattr(words[j], 'quoted', False) is True
                or (not parentheses and not shell_keyword(words[i], {'function'}))):
            i += 1
            continue
        opening, closing, level, end = words[j], '}' if words[j] == '{' else ')', 1, j + 1
        while end < len(words) and level:
            if getattr(words[end], 'quoted', False) is not True:
                if words[end] == opening:
                    level += 1
                elif words[end] == closing:
                    level -= 1
            end += 1
        if level:
            return [('uncertain', [], ';', ';')]
        events.extend(_split_commands(words[start:i]))
        events.append(('function', words[j + 1:end - 1], ';', ';'))
        start, i = end, end
    events.extend(_split_commands(words[start:]))
    return events


def wrapper_value_flag(head, flag, values):
    if flag in values:
        return True
    if head == 'env' and flag.startswith('-') and not flag.startswith('--'):
        at = next((i for i, ch in enumerate(flag[1:], 1) if ch in 'uCS'), None)
        return at is not None and at == len(flag) - 1
    return False


def strip_reserved_time(toks, uncertain=None):
    """Only lexical time at the command-keyword position can run a builtin in this shell."""
    toks = list(toks)
    while toks and shell_keyword(toks[0], {'time'}):
        option = toks[1:2]
        if option and option[0].startswith('-') and not (option == ['-p'] and getattr(option[0], 'quoted', None) is False):
            break
        toks.pop(0)
        if uncertain is not None:
            uncertain.append(True)  # timed assignments and nested time differ between supported shells
        if toks and toks[0] == '-p' and getattr(toks[0], 'quoted', None) is False:
            toks.pop(0)
    return toks


def strip_prefix(toks, assignments=None, wrappers=None, uncertain=None):
    """Drop VAR=value assignments, shell keywords (then, do, ...) and wrappers such as env, sudo, time, nohup, timeout."""
    toks = list(toks)
    env_assignments, keyword_position = False, True
    while toks:
        timed = strip_reserved_time(toks, uncertain) if keyword_position else toks
        if len(timed) != len(toks):
            toks = timed
            continue
        head = os.path.basename(toks[0])
        if shell_assignment(toks[0]) or (env_assignments and ASSIGN.match(toks[0])):
            if assignments is not None:
                assignments.append(toks[0])
            keyword_position = False
            toks.pop(0)
        elif shell_keyword(toks[0]):
            toks.pop(0)
        elif head in WRAPPERS:
            if head == 'command' and uncertain is not None:
                uncertain.append(True)  # command can suppress builtins in zsh while Bash still executes them
            keyword_position = False
            if wrappers is not None:
                wrappers.append((toks[0], []))
            env_assignments = head in ('env', 'sudo')  # these commands parse quoted NAME=value arguments themselves
            toks.pop(0)
            values = WRAPPER_VALUE.get(head, ())
            while toks and toks[0].startswith('-'):
                flag = toks.pop(0)
                if wrappers is not None:
                    wrappers[-1][1].append(flag)
                if flag == '--':
                    break
                if wrapper_value_flag(head, flag, values) and toks:
                    toks.pop(0)
            if head == 'timeout' and toks and re.match(r'^[0-9.]+[smhd]?$', toks[0]):
                toks.pop(0)
        else:
            break
    return toks



def env_split_script(toks):
    """env -S / --split-string runs its value as a command line. Return that line plus the words after it, so it can be
    judged like a bash -c script, or None when the prefixes hold no env -S."""
    toks = list(toks)
    while toks:
        head = os.path.basename(toks[0])
        if shell_assignment(toks[0]) or shell_keyword(toks[0]):
            toks.pop(0)
            continue
        if head not in WRAPPERS:
            return None
        toks.pop(0)
        values = WRAPPER_VALUE.get(head, ())
        while toks and toks[0].startswith('-'):
            flag = toks.pop(0)
            if flag == '--':
                break
            value = None
            if head == 'env':
                if flag == '--split-string':
                    value = toks.pop(0) if toks else ''
                elif flag.startswith('--split-string='):
                    value = flag.split('=', 1)[1]
                elif not flag.startswith('--'):
                    at = next((i for i, ch in enumerate(flag[1:], 1) if ch in 'uCS'), None)
                    if at is not None and flag[at] == 'S':
                        value = flag[at + 1:] or (toks.pop(0) if toks else '')
            if value is not None:
                return ' '.join([value] + [shlex.quote(t) for t in toks])
            if wrapper_value_flag(head, flag, values) and toks:
                toks.pop(0)
        if head == 'timeout' and toks and re.match(r'^[0-9.]+[smhd]?$', toks[0]):
            toks.pop(0)
    return None


def widen(longs, risky):
    """The long flags plus each risky option one of them abbreviates. Git takes any unambiguous prefix of a long option
    (--har for --hard, --force-w for --force-with-lease, --mirr for --mirror), so a prefix counts as every risky option
    it could stand for. An exact risky option stays itself: --force is not also --force-with-lease."""
    out = set(longs)
    for f in longs:
        if len(f) > 2 and f not in risky:
            out.update(r for r in risky if r.startswith(f))
    return out


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
    operators = {'<', '>', '>>', '>|', '&>', '&>>', '<>', '<<', '<<-', '<<<', '>&', '<&'}

    def syntax(word):
        return getattr(getattr(word, 'source_operand', word), 'quoted', False) is not True

    def operator(word):
        return word in operators and syntax(word)

    plain, redirs, k = [], [], 0
    while k < len(args):
        a = args[k]
        if operator(a):
            redirs.append((a, args[k + 1] if k + 1 < len(args) else ''))
            k += 2
        elif a.isdigit() and syntax(a) and k + 1 < len(args) and operator(args[k + 1]):
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
            proc.wait(timeout=1)
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


def review_verdict(pr, who, head, authors, trust_bot=False, run_id=None):
    """'PASS', 'BLOCK' or None for the latest '## <Who> ...review' comment naming head, written by one of authors.
    Codex's GitHub review app on the head commit counts as a Codex review only when pr_gate.trust_codex_bot is true."""
    found = None
    for c in pr.get('comments') or []:
        if str((c.get('author') or {}).get('login') or '').lower() not in authors:
            continue
        body = str(c.get('body') or '').lstrip()
        lines = body.splitlines()
        if (len(lines) >= 2 and re.fullmatch('## ' + who + r'(?: adversarial)? review', lines[0])
                and lines[1] == 'head: ' + head
                and (run_id is None or (len(lines) >= 3 and lines[2] == 'run: ' + run_id))):
            m = re.findall(r'^VERDICT: (PASS|BLOCK)\s*$', body, re.M)
            found = m[-1] if m else found
    if who == 'Codex' and not found and trust_bot:
        for r in pr.get('reviews') or []:
            if (r.get('author') or {}).get('login') in CODEX_BOTS and (r.get('commit') or {}).get('oid') == head:
                found = 'PASS'
    return found


REVIEW_EVIDENCE = 'AGENT_CORE_REVIEW_EVIDENCE_V1: '
REVIEW_RUN = 'AGENT_CORE_REVIEW_RUN_V1: '


def passing_test_receipt(checks, command):
    return (bool(command) and isinstance(checks, dict) and checks.get('status') == 'PASS'
            and type(checks.get('count')) is int and checks['count'] > 0
            and type(checks.get('exit_code')) is int and checks['exit_code'] == 0
            and checks.get('command') == command)


def latest_availability(pr, head, authors):
    found = None
    for index, c in enumerate(pr.get('comments') or []):
        if str((c.get('author') or {}).get('login') or '').lower() not in authors:
            continue
        lines = str(c.get('body') or '').splitlines()
        if not lines or lines[0] != '## Agent-core review availability':
            continue
        records = [line[len(REVIEW_EVIDENCE):] for line in lines if line.startswith(REVIEW_EVIDENCE)]
        try:
            data = json.loads(records[0]) if len(records) == 1 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            found = (None, index)
        elif data.get('head') == head and data.get('base') == pr.get('baseRefName'):
            found = (data, index)
    return found


def latest_automatic_run(pr, head, authors):
    """Only the most recently started automatic attempt can finish and reopen the gate."""
    latest, active = None, None
    for index, c in enumerate(pr.get('comments') or []):
        if str((c.get('author') or {}).get('login') or '').lower() not in authors:
            continue
        lines = str(c.get('body') or '').splitlines()
        if not lines or lines[0] != '## Agent-core review run':
            continue
        records = [line[len(REVIEW_RUN):] for line in lines if line.startswith(REVIEW_RUN)]
        try:
            data = json.loads(records[0]) if len(records) == 1 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            latest = ({'status': 'INVALID'}, index)
            continue
        if data.get('head') != head or data.get('base') != pr.get('baseRefName'):
            continue
        run_id = data.get('run_id')
        if (not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id)
                or not isinstance(data.get('base_oid'), str)
                or not re.fullmatch(r'[a-f0-9]{40}', data['base_oid'])
                or data['base_oid'] != pr.get('baseRefOid')):
            latest = ({'status': 'INVALID'}, index)
        elif data.get('status') == 'RUNNING':
            active, latest = run_id, (data, index)
        elif run_id == active:
            latest = (data, index)
        elif active is None:
            latest = ({'status': 'INVALID'}, index)  # no corresponding trusted start
    return latest


def automatic_run_passes(pr, attempt, head, authors, gate, command):
    if attempt.get('status') != 'PASS':
        return False
    providers = attempt.get('providers')
    if not isinstance(providers, dict) or set(providers) != {'Claude', 'Codex'}:
        return False
    passed = [name for name, status in providers.items() if status == 'PASS']
    if any(review_verdict(pr, name, head, authors, run_id=attempt['run_id']) != 'PASS' for name in passed):
        return False
    if len(passed) == 2:
        return True
    unavailable = [name for name, status in providers.items() if status == 'UNAVAILABLE']
    reasons = attempt.get('reasons')
    return (gate.get('allow_single_on_unavailable') is True and len(passed) == len(unavailable) == 1
            and isinstance(reasons, dict) and reasons.get(unavailable[0]) in ('quota', 'auth', 'service', 'missing_cli')
            and passing_test_receipt(attempt.get('tests'), command))


def outage_tests_pass(pr, provider, head, authors, command):
    """The latest trusted outage receipt must name this head/base and pass the configured full test command."""
    if not command:
        return False
    found = None
    for c in pr.get('comments') or []:
        if str((c.get('author') or {}).get('login') or '').lower() not in authors:
            continue
        lines = str(c.get('body') or '').splitlines()
        if not lines or lines[0] != '## Agent-core review availability':
            continue
        records = [line[len(REVIEW_EVIDENCE):] for line in lines if line.startswith(REVIEW_EVIDENCE)]
        try:
            data = json.loads(records[0]) if len(records) == 1 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            found = None
        elif data.get('head') == head and data.get('base') == pr.get('baseRefName') and data.get('provider') == provider:
            found = data
    if (not found or found.get('status') != 'UNAVAILABLE'
            or found.get('base_oid') != pr.get('baseRefOid')
            or not re.fullmatch(r'[a-f0-9]{40}', str(found.get('base_oid') or ''))
            or found.get('reason') not in ('quota', 'auth', 'service', 'missing_cli')):
        return False
    checks = found.get('tests')
    return passing_test_receipt(checks, command)


def merge_decision(args, repo=()):
    """Both providers pass, or one real provider passes with verified outage/test evidence. A BLOCK always holds.
    The gate off, other branches and repos not listed pass."""
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
    if STATE['cwd'] is None:
        name = r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+'
        host = r'[A-Za-z0-9.-]+(?::[0-9]+)?'
        explicit_repo = len(repo) == 2 and re.fullmatch(r'(?:https?://)?(?:' + host + r'/)?' + name + r'/?', repo[1])
        explicit_url = bool(sel) and re.fullmatch(r'https?://' + host + r'/' + name + r'/pull/[0-9]+/?', sel[0])
        if not explicit_repo and not explicit_url:
            return 'deny', ('gh pr merge is held: the repository is unknown after a directory change. '
                            'Use -R owner/repo or a full PR URL.')
    gh = os.environ.get('AGENT_CORE_GH') or 'gh'
    try:
        out = gh_run([gh, 'pr', 'view'] + sel[:1] + repo + ['--json', 'number,url,baseRefName,baseRefOid,headRefOid,'
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
    attempt = latest_automatic_run(pr, head, authors)
    availability = latest_availability(pr, head, authors)
    command = common.review_test_command(slug)
    if availability and (not attempt or availability[1] > attempt[1]) and (
            not availability[0] or not passing_test_receipt(availability[0].get('tests'), command)):
        return 'deny', 'The latest automatic review tests failed or are unverified. Run pr_review.py with auto again.'
    if attempt:
        if automatic_run_passes(pr, attempt[0], head, authors, gate, command):
            return None
        return 'deny', 'The latest automatic review run is not a verified pass. Run pr_review.py with auto again.'
    missing = [who for who, v in verdicts.items() if not v]
    if missing:
        if len(missing) == 1 and gate.get('allow_single_on_unavailable') is True:
            present = 'Codex' if missing[0] == 'Claude' else 'Claude'
            # A bot comment counted by the legacy two-provider policy is not a real fallback review.
            if (review_verdict(pr, present, head, authors) == 'PASS'
                    and outage_tests_pass(pr, missing[0], head, authors, common.review_test_command(slug))):
                return None
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
            value_flags = GH_MERGE_VALUE | {'-f', '--field', '--raw-field', '-X', '--method', '-H', '--header',
                                           '-q', '--jq', '--template', '--json', '--hostname', '--input',
                                           '--cache', '-p', '--preview'}
            if a in value_flags and k + 1 < len(args):
                rest.append(args[k + 1])
                k += 1  # a body/template value that starts -R is still an option value
        k += 1
    return words, repo, rest


def api_method(args):
    """The method of a gh api call: -X DELETE, -X=DELETE, -XDELETE, -iX DELETE, --method DELETE or --method=DELETE; the last wins."""
    method = next(((value or '?').lstrip('=') for flag, value in reversed(gh_api_parts(args)[0]) if flag == '--method'), '')
    return method.upper()


BODY_FLAGS = {'gh': ('-f', '-F', '--field', '--raw-field', '--input'),
              'curl': ('-d', '--data', '--data-raw', '--data-binary', '--data-urlencode', '--data-ascii', '-F', '--form',
                       '--form-string', '-T', '--upload-file', '--json'),
              'wget': ('--post-data', '--post-file', '--body-data', '--body-file')}
HTTP_WORDS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}


def reads_only(prog, args):
    """True when a gh api or HTTP client call only reads: GET or HEAD, with no request body (gh api and httpie switch to
    POST when fields are given). A method the guard cannot read counts as a write."""
    flags = BODY_FLAGS.get('gh' if prog == 'gh' else prog, ())
    body = any(a in flags or (a.startswith('--') and len(a.split('=', 1)[0]) > 2 and any(
        f.startswith(a.split('=', 1)[0]) for f in flags if f.startswith('--'))) for a in args)
    if prog == 'gh':
        options, _, unknown = gh_api_parts(args)
        if unknown:
            return False
        method = api_method(args)
        body = any(flag in ('--field', '--raw-field', '--input') for flag, _ in options)
    elif prog == 'curl':
        method, upload = '', False
        for k, a in enumerate(args):
            flag = a.split('=', 1)[0]
            if flag.startswith('--expand-'):
                return False  # curl variable expansion can turn a method, upload or payload into a write
            if a == '-:' or (flag.startswith('--') and len(flag) > 2 and
                              any(name.startswith(flag) for name in ('--config', '--next'))):
                return False  # config files and separate transfers can change a request the guard cannot see
            request = None
            if flag.startswith('--') and len(flag) > 2 and '--request'.startswith(flag):
                request = a.split('=', 1)[1] if '=' in a else (args[k + 1] if k + 1 < len(args) else '?')
            elif a.startswith('-') and not a.startswith('--'):
                for n, letter in enumerate(a[1:]):
                    if letter == 'X':
                        request = a[n + 2:] or (args[k + 1] if k + 1 < len(args) else '?')
                        break
                    if letter in 'EKCbcDdFPHhmoUQreyYtzTuAw':  # short options that consume the rest or next word
                        if letter == 'K':
                            return False
                        body = body or letter in 'dFT'
                        upload = upload or letter == 'T'
                        break
            upload = upload or (flag.startswith('--') and len(flag) > 2 and '--upload-file'.startswith(flag))
            if request is not None:
                if request.upper() not in ('GET', 'HEAD'):
                    return False
                method = request.upper()
        if body and not upload and ('-G' in args or '--get' in args):
            body = False  # -G sends the data as a query string
        return not body  # an explicit read method never excuses an unknown request body or upload
    elif prog == 'wget':
        method = ''
        for k, a in enumerate(args):
            flag = a.split('=', 1)[0]
            if (a.startswith('-e') and not a.startswith('--')) or (flag.startswith('--') and len(flag) > 2 and
                    any(name.startswith(flag) for name in ('--config', '--execute'))):
                return False
            if flag.startswith('--') and len(flag) > 2 and '--method'.startswith(flag):
                method = a.split('=', 1)[1] if '=' in a else (args[k + 1] if k + 1 < len(args) else '?')
                if method.upper() not in ('GET', 'HEAD'):
                    return False
    else:  # httpie and xh: an optional method word, then items; key=value, key:=json and key@file make a body
        method = next((a.upper() for a in args if a.upper() in HTTP_WORDS), '')
        body = any(re.match(r'^[^-][^=:@]*(=(?!=)|:=|@)', a) for a in args if '://' not in a)
    method = method.upper()
    if method:
        return method in ('GET', 'HEAD') and not re.search(r'[$`]', method)
    return not body


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


VERDICT_TEXT = re.compile(r'VERDICT:\s*(PASS|BLOCK)')
COMMENT_CMDS = (['pr', 'comment'], ['issue', 'comment'], ['pr', 'review'])


def posts_verdict(words, args):
    """True when a gh comment, review or api call carries text the merge gate would read as a review verdict, in its
    arguments, a body or input file, a heredoc, a here-string or the pipe into it. Only bin/pr_review.py posts verdicts,
    from its own process, so the gate cannot be satisfied by the agent writing one by hand."""
    if words[:2] not in COMMENT_CMDS and words[:1] != ['api']:
        return False
    if words[:1] == ['api'] and reads_only('gh', args):
        return False
    plain, redirs = split_redirects(args)
    filtered, skip, value = [], False, False
    for a in plain:
        if skip:
            skip = False
        elif value:
            filtered.append(a)
            value = False
        elif a in ('--jq', '-q', '--template', '-t'):
            skip = True
        elif not a.startswith(('--jq=', '--template=', '-q', '-t')):
            filtered.append(a)
            value = a in ('-b', '--body', '-f', '-F', '--field', '--raw-field', '--body-file', '--input')
    plain = filtered  # local output filters never publish text
    texts, files, unknown = list(plain), [], False
    if words[:1] == ['api']:
        for flag, value in gh_api_parts(plain)[0]:
            if flag in ('--field', '--raw-field', '--input'):
                unknown = unknown or value is None or getattr(value, 'path_uncertain', False)
            if flag == '--input':
                files.append(value)
            elif flag == '--field' and value is not None and '=@' in value:
                files.append(operand_value(value.split('=@', 1)[1], value))
    else:
        for k, a in enumerate(plain):
            if a in ('-F', '--body-file') and k + 1 < len(plain):
                files.append(plain[k + 1])
            elif a.startswith('--body-file='):
                files.append(operand_value(a.split('=', 1)[1], a))
            elif a.startswith('-F') and len(a) > 2:
                files.append(operand_value(a[2:], a))
            if a in ('-b', '--body', '-F', '--body-file') and k + 1 < len(plain):
                unknown = unknown or getattr(plain[k + 1], 'path_uncertain', False)
            elif a.startswith(('--body=', '--body-file=', '-b', '-F')):
                unknown = unknown or getattr(a, 'path_uncertain', False)
    for path in files:
        text = gh_body_file(path, args)
        unknown = unknown or text is None
        if text is not None:
            texts.append(text)
    for op, target in redirs:
        if op == '<<' and target.startswith(HEREDOC_MARK):
            text = heredoc_body(target)
        elif op == '<<<':
            text = None if getattr(target, 'path_uncertain', False) else target
        elif op == '<':
            text = gh_body_file(target, ())
        else:
            continue
        unknown = unknown or text is None
        if text is not None:
            texts.append(text)
    capable = words[:2] in COMMENT_CMDS or (words[:1] == ['api'] and api_comment_endpoint(args))
    return (unknown and capable) or any(VERDICT_TEXT.search(t) or REVIEW_EVIDENCE.strip() in t or 'AGENT_CORE_REVIEW_RUN_V1:' in t for t in texts if t)


def gh_decision(args):
    words, repo, rest = gh_split(args)
    if posts_verdict(words, args):
        return 'deny', MSG['fake_verdict']
    if words[:2] == ['pr', 'merge']:
        return merge_decision(rest, repo)
    if words[:1] == ['api'] and gate_branches() and (graphql_merges(args) or any(GRAPHQL_MERGE.search(a) for a in args) or (
            any(API_MERGE.search(a) for a in args) and not reads_only('gh', args))):
        return 'deny', MSG['api_merge']  # a GET of pulls/N/merge only asks whether it merged
    if words[:2] == ['repo', 'delete']:
        return 'deny', MSG['repo_delete']
    key = api_destroys(args) if words[:1] == ['api'] else None
    if key:
        return 'deny', MSG[key]
    if words[:2] == ['pr', 'checkout'] and STATE['moved'] != 'main':
        STATE['moved'] = 'unknown'  # the PR's branch is not known here
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


def stage_sweeps(specs, where, tree=None):
    """True when a git add pathspec list stages a whole tree: ., .., :/, :(top), a folder that is the repo root, only
    exclusions, or a command substitution whose result cannot be known. tree is the work tree a --git-dir/--work-tree
    override names (see git_tree): '' when it cannot be told, which counts as a sweep."""
    if tree == '':
        return True
    root = tree or common.repo_root(where)
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


def known_variable(name):
    if name in STATE['vars']:
        return STATE['vars'][name]
    if (name == 'HOME' and name not in STATE['unknown'] and '*' not in STATE['restricted']
            and name not in STATE['pinherit']):
        return os.environ.get('HOME')  # no passwd fallback when the original shell's HOME was unset
    return None


def unresolved_path(value, source):
    """Unquoted path references are one field only when their exact expansions cannot split or glob."""
    if isinstance(source, ExpandedOperand):
        return source.path_uncertain
    if not expands(source):
        return False
    if (getattr(source, 'unquoted_expansion', False) and not unknown_expansion(source)
            and '*' not in STATE['restricted'] and 'IFS' not in STATE['vars']
            and 'IFS' not in STATE['unknown'] and 'IFS' not in STATE['pinherit']):
        return not value or any(c in SHELL_WHITESPACE for c in value) or bool(GLOB.search(value)) or any(
            c in value for c in '{}')
    return True


def unknown_expansion(token):
    """An active expansion with no known value; a literal dollar in a known value is never expanded again."""
    if getattr(token, 'substitution_scripts', ()):
        return True
    sites = getattr(token, 'expansion_sites', None)
    if sites is None:
        return bool(re.search(r'[$`]', token))
    refs = [m for m in STRICT_REF.finditer(token) if m.start() in sites]
    return bool(sites - {m.start() for m in refs}) or any(
        known_variable(m.group(1) or m.group(2)) is None for m in refs)



def expands(token):
    """True when a raw token holds a $ or backtick the shell will act on and the guard cannot see the result of: not
    single-quoted or escaped, and, inside double quotes, not just references to variables the guard knows (one word)."""
    sites = getattr(token, 'expansion_sites', None)
    if sites is not None:
        if not sites:
            return False
        if token.unquoted_expansion:
            return True
        return unknown_expansion(token)
    marks = STATE['marks']
    if not re.search(r'[$`]', token) or token not in marks['active']:
        return False
    if token in marks['unquoted']:
        return True  # unquoted, a value with spaces splits into several words
    names = [a or b for a, b in STRICT_REF.findall(token)]
    left = STRICT_REF.sub('', token)
    return bool(re.search(r'[$`]', left)) or any(n not in STATE['vars'] for n in names) or bool(
        re.search(r'[$`]', expand(token)))



def git_tree(pre, where):
    """The work tree git uses when --git-dir / --work-tree (before the subcommand) or GIT_DIR / GIT_WORK_TREE move it off
    the repo around `where`: that folder, '' when it cannot be told, or None when nothing moves it. With only a git dir
    named, git takes the current folder as the top of the work tree."""
    named = {}
    for k, a in enumerate(pre):
        for opt in ('--work-tree', '--git-dir'):
            if a == opt and k + 1 < len(pre):
                named[opt] = pre[k + 1]
            elif a.startswith(opt + '='):
                named[opt] = a.split('=', 1)[1]
    for opt, var in (('--work-tree', 'GIT_WORK_TREE'), ('--git-dir', 'GIT_DIR')):
        if opt not in named:
            value = STATE['penv'].get(var, STATE['vars'].get(var))
            if value is not None:
                named[opt] = value
    if not named:
        return None
    tree = named.get('--work-tree')
    if tree is None:
        return where
    if not tree or re.search(r'[$`]', tree):
        return ''
    return os.path.normpath(os.path.join(where, os.path.expanduser(tree)))


def git_head(args):
    """(folder, subcommand, its arguments, -c settings) of a git call, or None when there is no subcommand. The settings
    are -c and --config-env on the command line and GIT_CONFIG_* variables set before it."""
    i, where, config = 0, STATE['cwd'] or os.getcwd(), list(STATE['envcfg'])
    while i < len(args):
        if args[i] == '-C' and i + 1 < len(args):
            where = os.path.join(where, os.path.expanduser(args[i + 1]))
        if args[i] == '-c' and i + 1 < len(args):
            config.append(args[i + 1])
        elif args[i] == '--config-env' and i + 1 < len(args):
            config.append(config_env(args[i + 1]))
        elif args[i].startswith('--config-env='):
            config.append(config_env(args[i].split('=', 1)[1]))
        if args[i] in GIT_VALUE:
            i += 2
        elif args[i].startswith('-'):
            i += 1
        else:
            break
    return (where, args[i], args[i + 1:], config) if i < len(args) else None


def config_env(entry):
    """key=value for a --config-env=key=ENVNAME setting: the value is what the variable ENVNAME holds, from the assignments in
    front of the command, the variables set earlier and the hook's own environment; $ENVNAME when it cannot be told."""
    key, eq, name = entry.rpartition('=')
    if not eq:
        key, name = entry, ''
    value = STATE['penv'].get(name, STATE['vars'].get(name, None if name in STATE['unknown'] else os.environ.get(name)))
    return '%s=%s' % (key, '$' + name if value is None else value)



def prefix_env(toks):
    """The assignments in validated prefix positions, never script arguments or wrapper option values."""
    env = {}
    assignments = []
    strip_prefix(toks, assignments=assignments)
    for t in assignments:
        k, v = expand(t).split('=', 1)
        env[k] = '$' + k if k.startswith('GIT_CONFIG') and unknown_expansion(t) else v
    prefix = toks[:len(toks) - len(strip_prefix(toks))]
    for k, token in enumerate(prefix):
        program = os.path.basename(token)
        if program in ('sudo', 'doas'):
            env.setdefault('HOME', '$HOME')  # only a child sees this wrapper's changed environment
        elif program == 'env':
            flags = prefix[k + 1:]
            for i, raw_flag in enumerate(flags):
                flag = expand(raw_flag)
                name, eq, value = flag.partition('=')
                next_value = flags[i + 1] if i + 1 < len(flags) else ''
                unknown_flag = flag.startswith('-') and unknown_expansion(raw_flag)
                clearing = flag == '-' or unknown_flag or (len(name) > 2 and '--ignore-environment'.startswith(name)) or (
                    flag.startswith('-') and not flag.startswith('--') and 'i' in flag[1:])
                at = flag.find('u', 1) if flag.startswith('-') and not flag.startswith('--') else -1
                tail = flag[at + 1:] if at >= 0 else ''
                target = tail or expand(next_value)
                unknown_target = unknown_expansion(raw_flag) or (not eq and not tail and unknown_expansion(next_value))
                unsetting = ((len(name) > 2 and '--unset'.startswith(name) and
                              ((value if eq else expand(next_value)) == 'HOME' or unknown_target)) or
                             (at >= 0 and (target == 'HOME' or unknown_target)))
                if clearing or unsetting:
                    env.setdefault('HOME', '$HOME')
    return env






def env_config(toks):
    """The settings GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n / GIT_CONFIG_PARAMETERS give a git command,
    as key=value strings. They are read from the hook's own environment, the variables set earlier, the prefix of the
    shells around the command and the assignments in front of it (expanded), the later ones winning. A count that cannot
    be read, or is above what is read, with a key set cannot be vouched for."""
    env = {k: v for k, v in os.environ.items() if k.startswith('GIT_CONFIG')}
    env.update({k: '$' + k for k in STATE['unknown'] if k.startswith('GIT_CONFIG')})
    env.update(STATE['vars'])
    env.update(STATE['pinherit'])
    env.update(prefix_env(toks))
    keys = [k for k in env if k.startswith('GIT_CONFIG_KEY_')]
    try:
        count = int(env.get('GIT_CONFIG_COUNT') or 0)
    except ValueError:
        count = 65 if keys else 0
    unreadable = env.get('GIT_CONFIG_PARAMETERS') == '$GIT_CONFIG_PARAMETERS'
    unreadable = unreadable or any(env.get('GIT_CONFIG_%s_%d' % (part, n)) == '$GIT_CONFIG_%s_%d' % (part, n)
                                  for n in range(min(count, 64)) for part in ('KEY', 'VALUE'))
    out = ['remote.unreadable-settings.push='] if count > 64 or unreadable else []
    out += ['%s=%s' % (env['GIT_CONFIG_KEY_%d' % n], env.get('GIT_CONFIG_VALUE_%d' % n, '')) for n in range(min(count, 64))
            if env.get('GIT_CONFIG_KEY_%d' % n)]
    # 'key=value' before git 2.31, 'key'='value' after it
    return out + [m.group(1) + ('' if m.group(2) is None else '=' + m.group(2))
                  for m in re.finditer(r"'([^']*)'(?:='([^']*)')?", env.get('GIT_CONFIG_PARAMETERS', ''))]



def branch_class(name):
    """'main' for a ref whose last component is main or master (origin/main and feature/main too), 'unknown' for a name that
    cannot be told (-, @{-1}, a variable), None for any other branch or a path."""
    if not name or name == '-' or '@{' in name or '$' in name or '`' in name:
        return 'unknown'
    return 'main' if name.split('/')[-1] in MAINS else None


SWITCH_LONG = {'--quiet', '--force', '--merge', '--conflict', '--detach', '--track', '--orphan', '--create', '--force-create',
               '--patch', '--ours', '--theirs', '--ignore-skip-worktree-bits', '--overwrite-ignore', '--recurse-submodules',
               '--pathspec-from-file', '--pathspec-file-nul', '--discard-changes', '--progress', '--ignore-other-worktrees',
               '--source', '--unified'}


def create_name(rest):
    """The new branch name of git checkout / switch: -b NAME, -cNAME, -qBNAME, -qB NAME (the create letter can sit in a
    bundle of short flags; what follows it is the name, or the next argument when nothing does), --create NAME,
    --force-create=NAME, --orphan NAME. The last one wins. A letter that takes a value (-t's optional mode, -U) ends the bundle: what
    follows it is that value (-tdirect), not more flags. Options with a separate value are skipped over."""
    name, k = None, 0
    while k < len(rest) and rest[k] != '--':
        a = rest[k]
        if a in ('--create', '--force-create'):
            name, k = (rest[k + 1] if k + 1 < len(rest) else None), k + 1
        elif a.startswith(('--create=', '--force-create=')):
            name = a.split('=', 1)[1]
        elif a == '--orphan':
            name, k = (rest[k + 1] if k + 1 < len(rest) else None), k + 1
        elif a.startswith('--orphan='):
            name = a.split('=', 1)[1]
        elif a in ('--conflict', '--pathspec-from-file', '--source', '--unified'):
            k += 1
        elif a.startswith('-') and not a.startswith('--'):
            at = next((i for i, ch in enumerate(a) if i and ch in 'bBcCtU'), None)
            if at is not None and a[at] in 'bBcC':
                name = a[at + 1:] or (rest[k + 1] if k + 1 < len(rest) else None)
                k += 0 if a[at + 1:] else 1
        k += 1
    return name


def note_move(sub, rest, config, where):
    """Remember in STATE['moved'] ('main' or 'unknown') that this git command may leave main, or a branch that cannot be
    told, checked out for the commands after it. The branch read at hook time is stale from then on, and nothing clears
    it: not a later switch, not another worktree, not the end of a chain. Moving to another named branch changes nothing
    for a later push when main was not left before it."""
    kind, switched = None, False
    if any(c.lower().startswith('alias.') for c in config) or sub not in GIT_KNOWN:
        kind = 'unknown'  # an alias or an external git-<name> can do anything
    elif sub in ('checkout', 'switch'):
        made = {'-b', '-B', '-c', '-C', '--create', '--force-create'}
        make = create_name(rest)
        flags = flag_parts(rest, value_opts=made | {'--orphan', '--source', '-s', '--conflict', '--pathspec-from-file'},
                           value_letters='bBcCs')
        p = flags[2]
        target = make or (p[0] if p else None)
        if target and not (sub == 'checkout' and '--' in rest and not make):  # checkout [<tree>] -- <paths> restores files
            kind = branch_class(target)
            switched = bool(make) or sub == 'switch' or not os.path.lexists(os.path.join(where, target))
            switched = switched and '--detach' not in flags[1] and not flags[0] & {'d'}
        if flags[1] - SWITCH_LONG and kind != 'main':
            kind = 'unknown'  # an abbreviation (--force-cre), a negation (--no-create) or something new: git may read it as anything
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
    if kind and STATE['moved'] != 'main' or kind == 'main':
        STATE['moved'] = kind
    elif switched and STATE['moved'] == 'main':
        STATE['moved'] = 'unknown'  # still not pushable, but not "on main" either: the message says to push on its own


def risky_config(entry, forcing=True):
    """True for a key=value git setting that makes a later push one nobody can read here: a saved refspec, a mirror that is
    not switched off, and a push.default that pushes to the upstream or to every matching branch (only current, simple and
    nothing are plain; without a force push.default is left alone on the command line)."""
    key, _, value = entry.partition('=')
    key, value = key.strip().lower(), value.strip().lower()
    if re.match(r'remote\..*\.push$', key):
        return True
    if re.match(r'remote\..*\.mirror$', key):
        return value not in ('false', 'no', 'off', '0')
    return key == 'push.default' and forcing and value not in ('current', 'simple', 'nothing')


RM_LONG = {'--force', '--cached', '--dry-run', '--ignore-unmatch', '--quiet', '--sparse', '--pathspec-from-file',
           '--pathspec-file-nul'}


def forced_rm(rest, where, tree=None):
    """git rm that is forced, keeps the files in the tree out of the index only with --cached, and names the whole tree,
    a pathspec file, or nothing at all (xargs and pipes feed the paths). -n does not excuse it. A --no-<option> or an
    abbreviated long option (--fo, --no-dry) may undo or stand for anything, so it counts as forcing and voids --cached."""
    s, l, p = flag_parts(rest, value_opts={'--pathspec-from-file'})
    odd = bool(l - RM_LONG)
    if not ('f' in s or '--force' in l or odd) or ('--cached' in l and not odd):
        return False
    return not p or '--pathspec-from-file' in l or stage_sweeps(p, where, tree)


VAR_USE = re.compile(r'\$[A-Za-z_{0-9@*]')


def raw_words(args, rest, ref):
    """The words of the command as typed (before $VAR expansion) that became the argument ref of git's subcommand; the
    argument itself when the raw words do not line up with the arguments."""
    raw = STATE['raw']
    if len(raw) != len(args) + 1:
        return [ref]
    return [w for w, a in zip(raw[1 + len(args) - len(rest):], rest) if a == ref]


def git_decision(args):
    args, _ = split_redirects(args)
    STATE['raw'], _ = split_redirects(STATE['raw'])
    head = git_head(args)
    if not head:
        return None
    where, sub, rest, config = head
    tree = git_tree(args[:len(args) - len(rest) - 1], where)
    note_move(sub, rest, config, where)
    if sub not in GIT_KNOWN and any(c.lower().startswith('alias.') for c in config):
        return 'deny', MSG['alias']  # the alias is what runs, and it is not readable here
    if sub in LANE_SUBS:
        owner = lane_owner(where)
        if owner and owner != (os.environ.get('AGENT_CORE_TOOL') or 'claude'):
            return 'deny', MSG['lane']
    if sub in ('add', 'stage'):
        if STATE['xargs']:
            return 'deny', MSG['stage']  # xargs appends or replaces paths the guard has not seen
        parsed, end = [], False
        for argument in rest:
            option, eq, value = argument.partition('=')
            if argument == '--':
                end = True
            if not end and len(option) > 2 and '--pathspec-from-file'.startswith(option):
                argument = '--pathspec-from-file' + ('=' + value if eq else '')
            parsed.append(argument)
        s, l, p = flag_parts(parsed, value_opts={'--chmod', '--pathspec-from-file'})
        sweeping = bool(s & {'A', 'u'} or l & {'--all', '--update', '--no-ignore-removal'})
        # An unknown folder (unless -C names one) gives no repo root to compare with, so only a plain relative pathspec
        # can be judged then. A word the shell expands or splits ($VAR, brace expansion) is not what the guard sees.
        known = STATE['cwd'] is not None or any(
            a == '-C' and k + 1 < len(args) and os.path.isabs(os.path.expanduser(args[k + 1])) for k, a in enumerate(args))
        if (any(expands(t) for t in STATE['raw']) or any(x in STATE['marks']['braced'] for x in p) or (not known and (
                sweeping or any(x.startswith(('/', '~', ':')) or '..' in x.split('/') for x in p)))):
            return 'deny', MSG['stage']
        if tree is not None and sweeping:
            return 'deny', MSG['stage']  # -A/-u against a work tree named on the command line or in GIT_DIR/GIT_WORK_TREE
        if sweeping:
            # -A and -u stage only the named paths; a bare one, a pathspec file, a pathspec the shell may expand to nothing
            # (a glob) or a tree-wide pathspec is the sweep
            if not p or '--pathspec-from-file' in l or any(re.search(r'[*?\[{]', x) for x in p) or stage_sweeps(p, where, tree):
                return 'deny', MSG['stage']
        elif stage_sweeps(p, where, tree):
            return 'deny', MSG['stage']
    elif sub == 'commit':
        s, l, p = flag_parts(rest, value_opts={'--message', '--file', '--author', '--date', '--template', '--reuse-message',
                                               '--reedit-message', '--fixup', '--squash', '--trailer', '--cleanup',
                                               '-m', '-F', '-C', '-c', '-t'}, value_letters='mFCct')
        if 'a' in s or '--all' in l:
            return 'deny', MSG['stage']
    elif sub == 'push':
        s, l, p = flag_parts(rest, value_opts={'--repo', '--receive-pack', '--exec', '--push-option', '-o'}, value_letters='o')
        # a lease needs a flag past --force- (--force-w, --force-with-lease); --forc may be plain --force, so it is, and
        # -f or --force anywhere turns git's lease check off
        plain = 'f' in s or any(len(f) > 2 and '--force'.startswith(f) for f in l)
        lease = not plain and any(f.startswith('--force-') for f in l)
        l = widen(l, {'--force', '--force-with-lease', '--force-if-includes', '--delete', '--mirror', '--all', '--branches'})
        force = 'f' in s or bool(l & {'--force', '--force-with-lease', '--force-if-includes'})
        deleting = 'd' in s or '--delete' in l
        plus = colon_delete = main_hit = wild = unresolved = False
        for ref in p[1:]:
            plus = plus or ref.startswith('+')
            src, sep, dst = ref.lstrip('+').partition(':')
            colon_delete = colon_delete or (bool(sep) and not src)
            main_hit = main_hit or (dst if sep else src) in MAIN_REFS or bool(MAIN_DST.search(dst if sep else src))
            wild = wild or '*' in ref
            raw_refs = [(word, queried_branch(word)) for word in raw_words(args, rest, ref)]
            main_hit = main_hit or any(branch_class(branch) == 'main' for _, branch in raw_refs if branch)
            unresolved = unresolved or (bool(VAR_USE.search(ref)) and any(VAR_USE.search(word) and
                word in STATE['marks']['active'] and branch is None for word, branch in raw_refs))
        # a refspec or mirror in the command line config (remote.<name>.push / .mirror, push.default) is a push nobody can read
        configured = any(risky_config(c, force or plus) for c in config)
        if configured or ((force or plus or deleting or colon_delete) and main_hit):
            return 'deny', MSG['force_main']
        if (force or plus) and (l & {'--all', '--branches'} or wild or unresolved):
            return 'deny', MSG['force_main']
        if (force or plus) and (len(p) < 2 or any(r.lstrip('+') in ('HEAD', '@', '$') or '$(' in r or '`' in r for r in p[1:])):
            # no refspec (or one a substitution fills in), or HEAD: the current branch is what gets force-pushed, and an unreadable branch counts as main;
            # so is main as an earlier part of this command left it
            if STATE['moved'] == 'main' or current_branch(where) in (None, 'main', 'master'):
                return 'deny', MSG['force_main']
            if STATE['moved'] or STATE['cwd'] is None:  # switched to a branch we cannot name, or a folder we cannot follow
                return 'deny', MSG['branch_unknown']
        if plus or (force and not lease):
            return 'deny', MSG['force']
        if deleting or colon_delete:
            return 'deny', MSG['delete_remote']
        if '--mirror' in l:
            return 'deny', MSG['mirror']
    elif sub == 'reset':
        mode_args = rest[:rest.index('--')] if '--' in rest else rest
        if any(unknown_expansion(raw) or unresolved_path(ref, raw)
               for ref in mode_args for raw in raw_words(args, rest, ref)):
            return 'deny', MSG['reset']  # an inherited or generated word can be --hard rather than a revision
        if '--hard' in widen(flag_parts(rest)[1], {'--hard'}):
            return 'deny', MSG['reset']
    elif sub == 'clean':
        s, l, p = flag_parts(rest, value_opts={'-e', '--exclude'}, value_letters='e')
        if not ('n' in s or '--dry-run' in l):  # whatever -c clean.requireForce says
            return 'deny', MSG['clean']
    elif sub in ('checkout', 'restore'):
        s, l, p = flag_parts(rest, value_opts={'-b', '-B', '--orphan', '--source', '-s', '--conflict', '--pathspec-from-file'},
                             value_letters='bBs')
        l = widen(l, {'--force'})  # never widened into a flag that allows more (--staged)
        if sub == 'restore' and ('S' in s or '--staged' in l) and not ('W' in s or '--worktree' in l):
            return None
        if set(p) & {'.', './', ':/', '*'} or (sub == 'checkout' and ('f' in s or '--force' in l)):
            return 'deny', MSG['discard']
    elif sub == 'switch':
        s, l, p = flag_parts(rest, value_opts={'-c', '-C', '--create', '--force-create', '--orphan', '--conflict'},
                             value_letters='cC')
        if 'f' in s or widen(l, {'--force', '--discard-changes'}) & {'--force', '--discard-changes'}:
            return 'deny', MSG['discard']
    elif sub == 'update-ref':
        s, l, p = flag_parts(rest, value_opts={'-m'}, value_letters='m')
        l = widen(l, {'--delete', '--stdin'})
        if 'd' in s or '--delete' in l:
            return 'deny', MSG['branch']
        if '--stdin' in l or (p and p[0] in MAIN_REFS):
            return 'deny', MSG['move_main']
        if p and p[0] == 'HEAD' and (STATE['moved'] or current_branch(where) in (None, 'main', 'master')):
            return 'deny', MSG['move_main']
    elif sub == 'branch':
        s, l, p = flag_parts(rest)
        l = widen(l, {'--delete', '--force', '--move', '--copy'})
        force = 'f' in s or '--force' in l
        if 'D' in s or (('d' in s or '--delete' in l) and force):
            return 'deny', MSG['branch']
        if force and not s & {'d', 'm', 'M', 'c', 'C'} and p and p[0] in MAINS:
            return 'deny', MSG['move_main']
        if p and p[-1] in MAINS and (s & {'M', 'C'} or (s & {'m', 'c'} or l & {'--move', '--copy'}) and force):
            # a forced rename or copy onto main; the first `git branch -M main` of a new repo has no main to overwrite
            if not (len(p) == 1 and current_branch(where) == p[0]) and branch_exists(where, p[-1]):
                return 'deny', MSG['move_main']
    elif sub == 'remote':
        if next((a for a in rest if not a.startswith('-')), None) == 'add' and any(a in ('--mirror', '--mirror=push') for a in rest):
            return 'deny', MSG['mirror']  # a mirror remote makes every plain push overwrite every ref there
    elif sub == 'config':
        s, l, p = flag_parts(rest, value_opts={'-f', '--file', '--blob', '--type', '-t', '--default', '--comment', '--value'}, value_letters='ft')
        p = p[1:] if p[:1] == ['set'] else p
        reading = s & {'l', 'e'} or l & {'--list', '--unset', '--unset-all', '--edit'} or any(x.startswith('--get') for x in l)
        if len(p) >= 2 and not reading and risky_config(p[0] + '=' + p[1]):
            return 'deny', MSG['force_main']  # a saved refspec or mirror makes every later `git push` a push nobody can read
    elif sub == 'rm':
        if forced_rm(rest, where, tree):
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
    here = os.path.normpath(STATE['cwd'] or '/')
    whole = recursive and any(whole_cwd(t) and not _temp_path(os.path.normpath(os.path.join(
        here, os.path.expanduser(t)))) for t in targets)
    if whole and not PROTECTED.hit(here, ancestor=True) and not any(
            PROTECTED.hit(t, ancestor=True) for t in targets if not GLOB.search(t)):
        return 'deny', MSG['rm_cwd']
    if any(PROTECTED.hit(t, ancestor=recursive) for t in targets):
        return 'deny', MSG['lake']
    if recoverable_files(targets):
        return 'deny', MSG['rm']
    if whole:
        return 'deny', MSG['rm_cwd']  # whatever the folder is called: a build-folder name does not make its parent disposable
    if recursive and (not targets or not all(disposable(t) or plain_files(t) for t in targets)):
        return 'deny', MSG['rm']
    return None


def destination(args, value_letters):
    values = {'-t', '--target-directory', '-e', '--rsh', '--exclude', '--include', '--filter', '-S', '--suffix'}
    s, l, p = flag_parts(args, value_opts=values, value_letters=value_letters)
    k = 0
    while k < len(args):
        a = args[k]
        if a == '--':
            break
        if a.startswith('--'):
            flag, eq, value = a.partition('=')
            if flag == '--target-directory':
                return (operand_value(value, a) if eq else (args[k + 1] if k + 1 < len(args) else None)), p
            k += 1 + (flag in values and not eq)
            continue
        if a.startswith('-'):
            taking = next((n for n, char in enumerate(a[1:], 1) if char in value_letters), None)
            if taking is not None:
                value = a[taking + 1:]
                if a[taking] == 't':
                    return (operand_value(value, a) if value else (args[k + 1] if k + 1 < len(args) else None)), p
                k += not value
        k += 1
    return (p[-1] if len(p) >= 2 else None), p


def inplace_files(prog, args):
    """The files sed -i / perl -i would rewrite: positionals after the script, not the script itself."""
    if prog == 'sed':
        return [args[k] for k in sed_arguments(args)[3]]
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
    sites = getattr(tok, 'expansion_sites', None)
    def value(match):
        known = known_variable(match.group(1))
        return match.group(0) if known is None else known
    return VAR_REF.sub(lambda m: value(m) if sites is None or m.start() in sites else m.group(0), tok)



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
    opts, k = args, 0
    while k < len(args):  # after a -- that is not an option's value everything is an operand
        a = args[k]
        k += 1
        if a == '--':
            opts = args[:k - 1]
            break
        if a in SEARCH_VALUE or a in PATTERN_OPTS or (a[:1] == '-' and a[1:2] != '-' and next(
                (ch == a[-1] for ch in a[1:] if ch in SEARCH_LETTERS[prog]), False)):
            k += 1
    patterns = [a.split('=', 1)[1] for a in opts if a.startswith('--regexp=')]
    patterns += [opts[k + 1] for k, a in enumerate(opts[:-1]) if a in ('-e', '--regexp')]
    given = s & {'e', 'f'} or any(a in PATTERN_OPTS or a.startswith(('--file=', '--expression=')) for a in opts)
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
    return (env_hit(pattern) or env_hit(plain) or plain == 'env' or
            bool(re.search(r'(?<![A-Za-z0-9_])\.env(?!\.(example|sample|template|dist|defaults?)$)', plain)))


def feeds_env(cmd):
    """True when an upstream pipeline command names env files for the reader after it."""
    names, patterns = listed_names(cmd)
    return any(env_hint(t) for t in names) or any(pattern_env(q) for q in patterns)


def structured_reads_env(prog, args, file_operand):
    """jq filters and option values are not filenames; yq's automatic filename detection stays conservative."""
    expression, values_only, literal, k = False, False, False, 0
    value_flags = {'--indent', '--width', '-w', '-I', '-p', '-o', '--input-format', '--output-format',
                   '--front-matter', '--yaml-output-grammar-version', '--yml-out-ver'}
    file_flags = {'-f', '--from-file', '-L', '--library-path', '--split-exp-file', '--run-tests'}
    while k < len(args):
        a = args[k]
        flag, eq, attached = a.partition('=')
        if not literal and a == '--':
            literal = True
        elif not literal and flag in ('--arg', '--argjson'):
            k += 2  # name and literal value
        elif not literal and flag in ('--rawfile', '--slurpfile', '--argfile'):
            if k + 2 < len(args) and file_operand(k + 2, args[k + 2]):
                return True
            k += 2  # name and filename
        elif not literal and flag in file_flags:
            index = k if eq else k + 1
            value = attached if eq else (args[index] if index < len(args) else '')
            if value and file_operand(index, value):
                return True
            expression = expression or flag in ('-f', '--from-file', '--run-tests')
            k += not eq
        elif not literal and flag == '--expression' and prog == 'yq':
            expression = True
            k += not eq
        elif not literal and flag in value_flags:
            k += not eq
        elif not literal and a in ('--args', '--jsonargs'):
            values_only = True
        elif not literal and a.startswith('-') and a != '-':
            if not a.startswith('--'):
                at = next((n for n, ch in enumerate(a[1:], 1) if ch in 'fL'), None)
                if at is not None:
                    value = a[at + 1:]
                    index = k if value else k + 1
                    value = value or (args[index] if index < len(args) else '')
                    if value and file_operand(index, value):
                        return True
                    expression = expression or a[at] == 'f'
                    k += index != k
        elif prog == 'yq' and not expression and a in ('eval', 'e', 'eval-all', 'ea'):
            pass
        elif not expression:
            # Go yq treats an existing first operand as a file, unlike jq/Python yq. Use --expression for an
            # ambiguous filter such as .env so it cannot become an env-file read when that file exists.
            if prog == 'yq' and file_operand(k, a):
                return True
            expression = True
        elif not values_only and file_operand(k, a):
            return True
        k += 1
    return False



def reads_env(prog, args):
    """True when a reader program is given an env file (or a glob matching one) to read, unless it can only print
    variable names, counts or file names (names_only)."""
    if names_only(prog, args):
        return False
    raw_args = STATE['raw'][-len(args):] if args else []
    args, redirects = split_redirects(args)
    raw_args = [getattr(arg, 'source_operand', arg) for arg in args]

    def file_operand(index, value):
        raw = getattr(value, 'source_operand', raw_args[index] if index < len(raw_args) else value)
        return env_hit(value) or unresolved_path(value, raw)

    if any(op.startswith('<') and op not in ('<<', '<<-', '<<<') and file_operand(0, target)
           for op, target in redirects):
        return True
    if prog in PATTERN_FIRST and any(getattr(raw, 'unquoted_expansion', False) and unresolved_path(value, raw)
                                    for value, raw in zip(args, raw_args)):
        return True  # a skipped pattern/value must not split into additional input files
    if prog == 'sed':
        scripts, script_files, inplace, files = sed_arguments(args)
        if inplace and scripts and not script_files and all(quiet_inplace_substitution(script) for script in scripts):
            return False
        return any(file_operand(index, value) for index, value in script_files) or any(
            file_operand(index, args[index]) for index in files)
    if prog in ('jq', 'yq'):
        if any(unresolved_path(value, raw) or getattr(raw, 'unquoted_pattern', False)
               for value, raw in zip(args, raw_args)):
            return True  # skipped filter/option words must not expand into extra flags or filenames
        return structured_reads_env(prog, args, file_operand)

    skip_first = prog in PATTERN_FIRST and not supplied_search_pattern(prog, args)
    literal, k = False, 0
    while k < len(args):
        a = args[k]
        k += 1
        if not literal and a == '--':
            literal = True
        elif not literal and a in FILE_FLAGS and k < len(args):  # -g .env*, --include .env, -f patterns
            if file_operand(k, args[k]):
                return True
            k += 1
        elif not literal and prog in PATTERN_FIRST and a in PATTERN_OPTS | SEARCH_VALUE and k < len(args):
            k += 1  # a search pattern, script or context count is not a file operand
        elif not literal and a.startswith('-') and a != '-':
            if a.startswith('--') and '=' in a:
                flag, value = a.split('=', 1)
                if flag in FILE_FLAGS and file_operand(k - 1, value):
                    return True
                if flag not in PATTERN_OPTS and env_hit(value):
                    return True
            elif prog in PATTERN_FIRST:
                takes = SEARCH_LETTERS.get(prog, 'efFv')
                at = next((n for n, char in enumerate(a[1:], 1) if char in takes), None)
                if at is not None:
                    attached = a[at + 1:]
                    index = k - 1 if attached else k
                    value = attached or (args[k] if k < len(args) else '')
                    if a[at] in 'fg' and value and file_operand(index, value):
                        return True
                    k += not attached and k < len(args)
        elif skip_first:
            skip_first = False  # grep/rg/awk/sed: the first operand is the pattern or script, not a file
        elif file_operand(k - 1, a):
            return True
    return False


def change_dir(args):
    """Track cd/pushd. The folder becomes unknown (None) for cd -, $VAR, a glob, ~user, a relative path from an unknown
    folder, and a folder that does not exist unless && makes sure the rest only runs after a good cd."""
    target = next((a for a in args if a == '-' or not a.startswith('-')), None)
    if target is None:
        target = known_variable('HOME')
    if target is None or target == '-' or GLOB.search(target) or '$' in target or '`' in target or re.match(r'^~[^/]', target):
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
    set_cwd(new)


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
        return printf_output(plain)
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


def sub_script(text, depth, fresh=False):
    """Judge text as a script run by a child shell: a cd or variable set there does not reach the caller."""
    saved = (STATE['cwd'], dict(STATE['vars']), set(STATE['restricted']), set(STATE['unknown']), STATE['cwd_candidates'])
    if fresh:
        # Git config can be inherited before its keys appear in a later command. Other parent locals remain unknown,
        # and readonly/local attributes do not survive exec. No export-attribute interpreter is needed to fail closed.
        unknown = STATE['unknown'] | {k for k in STATE['vars'] if not k.startswith('GIT_CONFIG')}
        if '*' in STATE['restricted']:
            unknown.update(k for k in os.environ if k.startswith('GIT_CONFIG'))
            unknown.update(('HOME', 'IFS'))
        STATE['vars'] = {k: v for k, v in STATE['vars'].items() if k.startswith('GIT_CONFIG') and k not in unknown}
        STATE['restricted'], STATE['unknown'] = set(), unknown
    try:
        return check_script(text, depth)
    finally:
        STATE['cwd'], STATE['vars'], STATE['restricted'], STATE['unknown'], STATE['cwd_candidates'] = saved





def shell_decision(args, depth):
    """bash/sh/zsh: -c takes a script; without it the script comes from a heredoc, here-string, file or the pipe."""
    for k, a in enumerate(args[:-1]):
        if a == '-c' or (a.startswith('-') and not a.startswith('--') and 'c' in a[1:]):
            return sub_script(args[k + 1], depth + 1, fresh=True)
    plain, redirs = split_redirects(args)
    if any(a.startswith('<(') or getattr(getattr(a, 'source_operand', a), 'process_substitution', False) for a in plain):
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
        decision = sub_script(text, depth + 1, fresh=True) if text else None
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


def nested(run, *args):
    """Run a nested script, shell or command with the git settings the command in front of it carries (GIT_CONFIG_* in its
    prefix) still in force."""
    saved = STATE['inherit'], STATE['pinherit']
    STATE['inherit'], STATE['pinherit'] = STATE['envcfg'], STATE['penv']
    try:
        return run(*args)
    finally:
        STATE['inherit'], STATE['pinherit'] = saved


def external_scope(toks):
    wrappers = []
    head = strip_prefix(toks, wrappers=wrappers)
    transparent = lambda w, flags: (w == 'builtin' and all(f == '--' for f in flags)
                                    or w == 'command' and all(f == '--' or re.fullmatch(r'-p+', f) for f in flags))
    return any(not transparent(w, flags) for w, flags in wrappers) or bool(head and '/' in head[0])


UNKNOWN_BUILTIN_TARGET = object()


def builtin_assignments(toks):
    """Variables assigned from builtin output are unknown, even when their preceding values were literal paths."""
    head = strip_prefix(toks)
    if not head or external_scope(toks):
        return set()
    prog = os.path.basename(head[0])
    args, _ = split_redirects(head[1:])
    names = []
    if prog == 'printf' and args:
        if args[0] == '-v':
            names = args[1:2]
        elif args[0].startswith('-v'):
            names = [args[0][2:]]
    elif prog == 'read':
        _, _, names = flag_parts(args, value_opts={'-a', '-A', '-d', '-i', '-k', '-n', '-N', '-p', '-t', '-u'},
                                 value_letters='aAdiknNptu')
        for k, arg in enumerate(args):
            if arg == '--':
                break
            if not arg.startswith('-') or arg.startswith('--'):
                continue
            for n, letter in enumerate(arg[1:]):
                if letter in 'aAdiknNptu':
                    if letter in 'aA':
                        names += [arg[n + 2:]] if arg[n + 2:] else args[k + 1:k + 2]
                    break
        names = names or ['REPLY']
    elif prog == 'unset':
        short, _, names = flag_parts(args)
        if 'f' in short and 'v' not in short:
            names = []  # -f only removes functions, not a cached variable of the same name
    out = set()
    for name in names:
        name = expand(name).split('[', 1)[0].split('?', 1)[0]
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            return {UNKNOWN_BUILTIN_TARGET}  # an unreadable destination can also replace variables created later
        out.add(name)
    return out



def invalidate_variable(name):
    STATE['vars'].pop(name, None)
    STATE['unknown'].add(name)  # absence must not make unknown git config look like an unset/default value



def set_variable(name, value, unknown):
    if name in STATE['restricted'] or '*' in STATE['restricted'] or unknown:
        invalidate_variable(name)  # the assignment can fail; it must not establish a new safe path
    else:
        STATE['unknown'].discard(name)
        STATE['vars'][name] = value





def analyze(toks, depth, raw=None, mutations=None):
    if not external_scope(toks):
        return analyze_command(toks, depth, raw, mutations)
    saved = (STATE['cwd'], dict(STATE['vars']), set(STATE['restricted']), set(STATE['unknown']), STATE['cwd_candidates'])
    try:
        return analyze_command(toks, depth, raw)
    finally:
        STATE['cwd'], STATE['vars'], STATE['restricted'], STATE['unknown'], STATE['cwd_candidates'] = saved


def analyze_command(toks, depth, raw=None, mutations=None):
    toks = strip_reserved_time(toks)
    assigns = [t for t in toks if shell_assignment(t)]
    if assigns and len(assigns) == len(toks):
        for t in assigns:
            k, v = expand(t).split('=', 1)
            set_variable(k, v, unknown_expansion(t))
        return None
    STATE['penv'] = dict(STATE['pinherit'], **prefix_env(toks))
    STATE['envcfg'] = STATE['inherit'] + env_config(toks)
    split = env_split_script(toks)
    if split is not None:
        return nested(sub_script, split, depth + 1) if depth < MAX_DEPTH else ('deny', MSG['deep'])
    stripped = strip_prefix(toks)
    if STATE['deferred_body'] and stripped and unknown_expansion(stripped[0]):
        return 'deny', MSG['deep']
    STATE['raw'] = (toks if raw is None else raw)[len(toks) - len(stripped):]  # index-aligned with toks, before expansion
    assigned = builtin_assignments(toks)  # keep external-wrapper provenance when classifying parent builtin writes
    toks = [operand_value(expand(t), t) for t in stripped]
    if UNKNOWN_BUILTIN_TARGET in assigned:
        return 'deny', MSG['deep']
    for name in assigned:
        invalidate_variable(name)
    if not toks:
        return None
    for k, t in enumerate(toks[:-1]):  # before the early returns below: export -p > file still writes the file
        if t in REDIRECTS and PROTECTED.hit(toks[k + 1]):
            return 'deny', MSG['lake']
    if toks[0] in ('export', 'declare', 'typeset', 'local', 'readonly'):
        unknown_inputs = [unknown_expansion(t) for t in stripped]  # every argument expanded before this builtin ran
        if any(unknown_inputs[i] and not ASSIGN.match(t) for i, t in enumerate(stripped[1:], 1)):
            return 'deny', MSG['deep']  # an opaque selector can become any variable name or NAME=value assignment
        short, longs, named = flag_parts(toks[1:])
        safe_flags = {'export': {'n'}, 'readonly': set(), 'declare': {'r', 'x'}, 'typeset': {'r', 'x'}}
        scalar = (toks[0] in safe_flags and not longs and short <= safe_flags[toks[0]]
                  and not any(a.startswith('+') for a in toks[1:]))
        if not scalar and named:
            STATE['unknown'].update(STATE['vars'])
            STATE['vars'].clear()
            STATE['restricted'].add('*')  # unsupported attributes/selection can invalidate scalar assignment assumptions
        for i, t in enumerate(toks[1:], 1):
            if ASSIGN.match(t):
                k, v = t.split('=', 1)
                set_variable(k, v, unknown_inputs[i])  # the argument has already undergone its one shell expansion
        if toks[0] == 'export' and 'n' in short and any(n.split('=', 1)[0] == 'HOME' for n in named):
            invalidate_variable('HOME')  # a child shell may no longer inherit the tool's HOME
        if 'f' not in short and (toks[0] == 'readonly' or (toks[0] != 'export' and 'r' in short)):
            STATE['restricted'].update(n.split('=', 1)[0] for n in named
                                       if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:=.*)?', n))
        return None
    if toks[0] in ('cd', 'pushd'):
        change_dir(toks[1:])
        return None
    if toks[0] == 'popd':
        set_cwd(None)
        return None
    prog, args = os.path.basename(toks[0]), toks[1:]
    if prog in SHELLS:
        return nested(shell_decision, args, depth)
    if prog == 'eval':
        return nested(check_script, ' '.join(args), depth + 1, mutations)
    if prog == 'xargs':
        k, files = 0, []
        while k < len(args) and args[k].startswith('-'):
            if args[k] in ('-a', '--arg-file') and k + 1 < len(args):
                files.append(args[k + 1])
            elif args[k].startswith('--arg-file='):
                files.append(args[k].split('=', 1)[1])
            k += 2 if args[k] in (XARGS_VALUE - {'-i'}) or args[k] == '--arg-file' else 1
        if any(env_hit(f) for f in files):
            return 'deny', MSG['env']
        inner = os.path.basename(args[k]) if k < len(args) else ''
        if inner == 'mv':
            return 'deny', MSG['pattern_move']
        if inner in ('rm', 'unlink') and rm_parts(args[k + 1:])[0]:
            return 'deny', MSG['find_delete']
        if inner in READERS and any(feeds_env(c) for c in STATE['pipe'] if c):
            return 'deny', MSG['env']  # something that names an env file feeds the reader through the pipe
        if k < len(args) and re.search(r'[$`]', args[k]):
            return 'deny', MSG['deep']  # the program xargs runs is not known
        saved, STATE['xargs'] = STATE['xargs'], True
        shell_state = (STATE['cwd'], dict(STATE['vars']), set(STATE['restricted']), set(STATE['unknown']), STATE['cwd_candidates'])
        try:
            return nested(analyze, args[k:], depth + 1, STATE['raw'][1 + k:]) if depth < MAX_DEPTH else ('deny', MSG['deep'])
        finally:
            STATE['xargs'] = saved
            STATE['cwd'], STATE['vars'], STATE['restricted'], STATE['unknown'], STATE['cwd_candidates'] = shell_state
    if prog == 'git':
        return git_decision(args)
    if prog == 'gh':
        return gh_decision(args)
    if prog in HTTP_CLIENTS and gate_branches() and any(API_MERGE.search(a) for a in args) and not reads_only(prog, args):
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
    return [body for _, _, body, _ in substitution_spans(text)]


def heredoc_substitutions(norm):
    """Substitutions in the bodies of unquoted-delimiter heredocs named in norm: those run even though the text is data."""
    out = []
    for n in re.findall(HEREDOC_MARK + r'(\d+)', norm):
        n = int(n)
        if n < len(STATE['expands']) and STATE['expands'][n]:
            out += substitutions(STATE['heredocs'][n])
    return out


def check_tokens(tokens, depth, deferred=False, mutations=None):
    """Judge each simple command in order, first decision wins. ( ) groups, pipeline stages and background jobs run in a
    child shell, so a cd or assignment there is undone afterwards."""
    stack, pipe, scopes = [], [], ['function'] if deferred else []
    control = KEYWORDS | {'for', 'select', 'case', 'fi', 'done', 'esac'}
    uncertain_scope = any((t in control and getattr(t, 'quoted', None) is None)
                          or (ASSIGN.match(t) and getattr(t, 'assignment', None) is None) for t in tokens)
    if uncertain_scope:
        return 'deny', MSG['deep']
    cond_cd = False  # a cd that ran only if an earlier command succeeded or failed, still inside its && chain
    cond_vars = set()  # assignments are known only while the same successful && chain continues
    try:
        for kind, cmd, before, after in split_commands(tokens):
            if cond_cd and before != '&&':
                set_cwd(None, cond_cd if isinstance(cond_cd, tuple) else None)
                cond_cd = False  # only these complete old/success states survive the chain
            if cond_vars and before != '&&':
                for name in cond_vars:
                    invalidate_variable(name)
                cond_vars.clear()
            if kind == 'uncertain':
                return 'deny', MSG['deep']
            if kind == 'function':
                STATE['body_file_unstable'] = True  # a defined function can replace the apparent producer of a body
                saved = (STATE['cwd'], dict(STATE['vars']), set(STATE['restricted']), set(STATE['unknown']), STATE['cwd_candidates'])
                writes = set()
                was_deferred, STATE['deferred_body'] = STATE['deferred_body'], True
                set_cwd(None)
                STATE['vars'], STATE['restricted'] = {}, set()
                STATE['unknown'] = set(os.environ) | set(saved[1]) | saved[3]  # caller state can change before a later call
                try:
                    decision = check_tokens(cmd, depth + 1, deferred=True, mutations=writes) if depth < MAX_DEPTH else ('deny', MSG['deep'])
                    names = writes - {None}
                    moved = None in writes
                finally:
                    STATE['cwd'], STATE['vars'], STATE['restricted'], STATE['unknown'], STATE['cwd_candidates'] = saved
                    STATE['deferred_body'] = was_deferred
                if decision:
                    return decision
                for name in names:
                    invalidate_variable(name)
                STATE['restricted'].update(names)  # a later call can still mutate these names after another assignment
                if mutations is not None:
                    mutations.update(names)
                if moved:
                    return 'deny', MSG['deep']  # later calls can move again, even after an intervening explicit cd
                pipe = []
            elif kind == 'open':
                stack.append((STATE['cwd'], dict(STATE['vars']), set(STATE['restricted']), set(STATE['unknown']), STATE['cwd_candidates'],
                              list(scopes), cond_cd, set(cond_vars)))
                cond_cd, cond_vars = False, set()  # the group is already on this chain's success branch
                pipe = []
            elif kind == 'close':
                if stack:
                    STATE['cwd'], STATE['vars'], STATE['restricted'], STATE['unknown'], STATE['cwd_candidates'], scopes, cond_cd, cond_vars = stack.pop()
                pipe = [['<group>']]
            else:
                for inner in command_substitutions(cmd):
                    decision = sub_script(inner, depth + 1)
                    if decision:
                        return decision
                if before not in PIPES:
                    pipe = []
                forked = before in PIPES or after in PIPES or after == '&'
                saved = (STATE['cwd'], dict(STATE['vars']), set(STATE['restricted']), set(STATE['unknown']), STATE['cwd_candidates']) if forked else None
                STATE['pipe'], STATE['sep'] = [list(c) for c in pipe], after
                scoped, loop_names = bool(scopes), set()
                scope_cmd = cmd
                while scope_cmd and shell_keyword(scope_cmd[0], {'time'}):
                    scope_cmd = scope_cmd[2:] if scope_cmd[1:2] == ['-p'] else scope_cmd[1:]
                for k, word in enumerate(scope_cmd):
                    if not shell_keyword(word, control):
                        break
                    if word in ('if', 'while', 'until', 'for', 'select', 'case'):
                        scopes.append('fi' if word == 'if' else ('esac' if word == 'case' else 'done'))
                        scoped = True
                        if word in ('for', 'select') and k + 1 < len(scope_cmd):
                            loop_names.add(scope_cmd[k + 1])
                    elif word in ('fi', 'done', 'esac'):
                        scoped = True
                        if scopes and scopes[-1] == word:
                            scopes.pop()
                    elif word not in ('then', 'do', 'else', 'elif'):
                        break
                ambiguous = []
                head = strip_prefix(cmd, uncertain=ambiguous)
                external = external_scope(cmd)
                moves = not external and bool(head) and os.path.basename(head[0]) in ('cd', 'pushd', 'popd')
                assigned = strip_reserved_time(cmd)
                while assigned and shell_keyword(assigned[0]):
                    assigned.pop(0)
                if external:
                    assigned = []
                elif head and head[0] in ('export', 'declare', 'typeset', 'local', 'readonly'):
                    assigned = head[1:]
                elif not all(shell_assignment(t) for t in assigned):
                    assigned = []  # command-prefix variables do not persist in the shell
                names = {t.split('=', 1)[0] for t in assigned if ASSIGN.match(t)} | builtin_assignments(cmd) | loop_names
                old_vars, old_cwd = dict(STATE['vars']), STATE['cwd']
                old_restricted = set(STATE['restricted'])
                old_unknown = set(STATE['unknown'])
                try:
                    decision = analyze(cmd, depth, mutations=mutations)
                    note_body_file_writes(cmd)
                    names.update(k for k in set(old_vars) | set(STATE['vars'])
                                 if k not in old_vars or k not in STATE['vars'] or old_vars[k] != STATE['vars'][k])
                    moved = moves or STATE['cwd'] != old_cwd
                    restricted_added = STATE['restricted'] - old_restricted
                    names.update(restricted_added)
                    names.update(STATE['unknown'] - old_unknown)
                finally:
                    STATE['plain'] = False  # only the first command of the script may rely on PLAIN_RM
                    if saved:
                        STATE['cwd'], STATE['vars'], STATE['restricted'], STATE['unknown'], STATE['cwd_candidates'] = saved
                if decision:
                    return decision
                if mutations is not None and not saved:
                    mutations.update(names)
                    if moved:
                        mutations.add(None)  # cwd mutation intent matters even when unknown remains unknown
                if ambiguous and not external and not saved:
                    for name in names:
                        invalidate_variable(name)
                    if moved:
                        set_cwd(None)
                if moved and before in PIPES and after not in PIPES:
                    set_cwd(None)  # zsh (and bash with lastpipe) run the last stage in this shell: its cd may stick
                elif moved and not saved and (scoped or after == '&&' or before in ('&&', '||') or (cmd and shell_keyword(cmd[0]))):
                    # if/then/else/do or && / ||: the cd may not run. Within a && chain after it, it did
                    if after == '&&' and before != '||' and not shell_keyword(cmd[0]) and not scoped:
                        cond_cd = (old_cwd, STATE['cwd']) if before not in ('&&', '||') and old_cwd is not None and STATE['cwd'] is not None else True
                    else:
                        set_cwd(None)
                if names and before in PIPES and after not in PIPES:
                    STATE['restricted'].update(restricted_added)
                    for name in names:
                        invalidate_variable(name)  # zsh and bash lastpipe can retain the last stage's assignments
                elif names and not saved and (scoped or before in ('&&', '||') or (cmd and shell_keyword(cmd[0]))):
                    if after == '&&' and before != '||' and not shell_keyword(cmd[0]) and not scoped:
                        cond_vars.update(names)
                    else:
                        for name in names:
                            invalidate_variable(name)
                pipe.append(cmd)
    finally:
        STATE['envcfg'] = []
        if stack:
            STATE['cwd'], STATE['vars'], STATE['restricted'], STATE['unknown'], STATE['cwd_candidates'] = stack[0][:5]
    return None




def scan_moves(norm, depth=0):
    """A command substitution runs before the command it sits in: note the branch changes in it first."""
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
                if STATE['moved'] != 'main':
                    STATE['moved'] = 'unknown'
        scan_moves(text, depth + 1)


def check_script(text, depth=0, mutations=None):
    if depth > MAX_DEPTH:
        return 'deny', MSG['deep']
    if depth == 0:
        STATE['moved'] = False
        STATE['body_file_unstable'] = False
    try:
        norm = normalize(text)
        if depth == 0:
            STATE['plain'] = bool(PLAIN_RM.match(text))
        scan_moves(norm)
        tokens = execution_tokens(norm)
        decision = check_tokens(tokens, depth, mutations=mutations)
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
    recovery = recoverable_files(targets)
    if (not recursive and not recovery or not targets or (not recovery and all(disposable(t) for t in targets))
            or any(PROTECTED.hit(t, True) for t in targets)):
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
    if hit is None and kind in ('advisor', 'fable-advisor'):
        hit = next((g for g in gated if g in ADVISOR_MODEL), None)
    return ('ask', MSG['gated'] % hit) if hit else None



NO_CONTENT = {
    'grep': ({'c', 'l', 'L', 'q'}, {'--count', '--files-with-matches', '--files-without-match', '--quiet', '--silent'}),
    'egrep': ({'c', 'l', 'L', 'q'}, {'--count', '--files-with-matches', '--files-without-match', '--quiet', '--silent'}),
    'fgrep': ({'c', 'l', 'L', 'q'}, {'--count', '--files-with-matches', '--files-without-match', '--quiet', '--silent'}),
    'rg': ({'c', 'l', 'q'}, {'--count', '--count-matches', '--files-with-matches', '--files-without-match', '--quiet', '--files'}),
    'ag': ({'c', 'l'}, {'--count', '--files-with-matches'}),
    'ack': ({'c', 'l'}, {'--count', '--files-with-matches'}),
}


GH_API_FLAGS = {'--include', '--paginate', '--silent', '--slurp', '--verbose', '--help', '--allow-escape-sequences'}


GH_API_SHORT = {'X': '--method', 'f': '--raw-field', 'F': '--field', 'H': '--header',
                'q': '--jq', 't': '--template', 'p': '--preview'}


GH_API_VALUES = {'--method', '--field', '--raw-field', '--header', '--input', '--jq', '--template',
                 '--hostname', '--cache', '--preview'}


def gh_api_parts(args):
    """API options with their values, endpoints, and whether an option's meaning is unknown."""
    options, positionals, unknown, literal, k = [], [], False, False, 0
    while k < len(args):
        a = args[k]
        if a == '--' and not literal:
            literal = True
        elif literal or not a.startswith('-') or a == '-':
            positionals.append(a)
        elif a.startswith('--'):
            flag, eq, value = a.partition('=')
            if flag in GH_API_VALUES:
                if not eq:
                    k += 1
                    value = args[k] if k < len(args) else None
                else:
                    value = operand_value(value, a)
                options.append((flag, value))
                unknown = unknown or value is None
            elif flag not in GH_API_FLAGS:
                unknown = True
        else:
            for n, letter in enumerate(a[1:], 1):
                if letter in GH_API_SHORT:
                    value = a[n + 1:]
                    if not value:
                        k += 1
                        value = args[k] if k < len(args) else None
                    else:
                        value = operand_value(value, a)
                    options.append((GH_API_SHORT[letter], value))
                    unknown = unknown or value is None
                    break
                if letter not in 'ih':
                    unknown = True
        k += 1
    return options, positionals, unknown


def gh_stdin_text(args):
    """The effective stdin request body, or None when its producer cannot be inspected exactly."""
    _, redirs = split_redirects(args)
    text, redirected = None, False
    for op, target in redirs:
        if op == '<<' and target.startswith(HEREDOC_MARK):
            text, redirected = heredoc_body(target), True
        elif op == '<<<':
            text, redirected = (None if getattr(target, 'path_uncertain', False) else target), True
        elif op == '<':
            text, redirected = gh_body_file(target, ()), True
    if not redirected and STATE['pipe']:
        if STATE['body_file_unstable']:
            return None
        text = piped_text(STATE['pipe'][-1])
    return text


def gh_body_file(path, args):
    if path == '-':
        return gh_stdin_text(args)
    if STATE['body_file_unstable'] or path is None or getattr(path, 'path_uncertain', False):
        return None
    text = read_text(path)
    return None if text is not None and len(text) >= 1 << 20 else text


def note_body_file_writes(command):
    """A prior command can replace a request body after the hook has read its old contents."""
    plain, redirs = split_redirects(command)
    if any(op not in ('<', '<<', '<<<') for op, _ in redirs):
        STATE['body_file_unstable'] = True
        return
    if not plain or all(shell_assignment(token) for token in plain):
        return
    head = strip_prefix(plain)
    pure = {'echo', 'printf', 'cat', 'true', 'false', ':', 'cd', 'pushd', 'popd',
            'export', 'declare', 'typeset', 'local', 'readonly', 'unset', 'read'}
    if not head or head[0] not in pure:
        STATE['body_file_unstable'] = True


def graphql_merges(args):
    """Known GraphQL reads pass; an unreadable query cannot prove it avoids a merge mutation."""
    options, endpoints, _ = gh_api_parts(args)
    if not any(re.search(r'(?:^|/)graphql(?:\?|$)', endpoint) for endpoint in endpoints):
        return False
    queries = []
    for flag, value in options:
        if flag == '--input':
            text = gh_body_file(value, args)
            if text is None:
                return True
            try:
                body = json.loads(text)
                query = body.get('query') if isinstance(body, dict) else None
            except ValueError:
                return True
            if not isinstance(query, str):
                return True
            queries.append(query)
        elif flag in ('--field', '--raw-field') and value is not None:
            name, eq, query = value.partition('=')
            if name != 'query' or not eq:
                continue
            if getattr(value, 'path_uncertain', False):
                return True
            if flag == '--field' and query.startswith('@'):
                query = gh_body_file(query[1:], args)
                if query is None:
                    return True
            queries.append(query)
    return any(GRAPHQL_MERGE.search(query) for query in queries)


def api_comment_endpoint(args):
    endpoints = [value for value in gh_api_parts(args)[1] if value != 'api']
    if not endpoints:
        return True
    endpoint = endpoints[0]
    return (getattr(endpoint, 'path_uncertain', False) or bool(re.search(
        r'(?:^|/)(?:issues|pulls)/(?:[^/]+/)?(?:comments|reviews)(?:/|$)', endpoint)))


def queried_branch(token):
    scripts = getattr(token, 'substitution_scripts', ())
    if (len(scripts) != 1 or getattr(token, 'process_substitution', False)
            or not re.fullmatch(r'\$\{__agent_core_substitution_\d+\}', token)
            or not re.fullmatch(r'\s*git\s+branch\s+--show-current\s*', scripts[0])
            or STATE['cwd'] is None):
        return None
    return current_branch(STATE['cwd'])


def recoverable_files(targets):
    """Existing databases and file batches over 1 GB must reach trash.py's review and recovery checks."""
    total = 0
    for target in targets:
        if STATE['cwd'] is None or re.search(r'[$`*?\[{\\]', target):
            continue
        path = os.path.join(STATE['cwd'], os.path.expanduser(target))
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        try:
            total += os.path.getsize(path)
            if path.lower().endswith(('.db', '.sqlite', '.sqlite3', '.duckdb')):
                return True
            with open(path, 'rb') as stream:
                if stream.read(16) == b'SQLite format 3\x00':
                    return True
        except OSError:
            return True  # an existing file that cannot be inspected is not proven disposable
    return total > 1 << 30


def sed_arguments(args):
    """Separate scripts, script-file operands and input files without interpreting script text as options."""
    scripts, script_files, files, inplace, literal, k = [], [], [], False, False, 0
    while k < len(args):
        a = args[k]
        if not literal and a == '--':
            literal = True
        elif not literal and a.startswith('--'):
            flag, eq, value = a.partition('=')
            if flag in ('--expression', '--file'):
                index = k if eq else k + 1
                value = value if eq else (args[index] if index < len(args) else '')
                if flag == '--expression':
                    scripts.append(value)
                else:
                    script_files.append((index, value))
                k += not eq
            elif flag == '--in-place':
                inplace = True
        elif not literal and a.startswith('-') and a != '-':
            for n, char in enumerate(a[1:], 1):
                if char in 'ef':
                    value = a[n + 1:]
                    index = k if value else k + 1
                    value = value or (args[index] if index < len(args) else '')
                    if char == 'e':
                        scripts.append(value)
                    else:
                        script_files.append((index, value))
                    k += index != k
                    break
                if char == 'i':
                    inplace = True
                    if n == len(a) - 1 and k + 1 < len(args) and args[k + 1] == '':
                        k += 1  # BSD sed's empty backup suffix
                    break  # the rest is the backup suffix, never more flags
        elif not scripts and not script_files:
            scripts.append(a)
        else:
            files.append(k)
        k += 1
    return scripts, script_files, inplace, files


def quiet_inplace_substitution(script):
    """A single substitution with no execute/write commands can only print into sed's in-place output file."""
    if len(script) < 4 or script[0] != 's' or script[1].isspace() or script[1] == '\\':
        return False
    delimiter, k = script[1], 2
    for _ in range(2):
        while k < len(script) and script[k] != delimiter:
            if script[k] in '\r\n':
                return False
            k += 2 if script[k] == '\\' else 1
        if k >= len(script):
            return False
        k += 1
    return bool(re.fullmatch(r'[gpI0-9]*', script[k:]))


NAMES_ONLY_SED = re.compile(r'^s(?P<d>[^\\\s])=\.\*(?P=d)[^\\&]*(?P=d)[gp]{0,2}$')

def names_only(prog, args):
    """True when the read cannot print an env value: sed whose every script is s/=.*/<text>/ (cuts each line at its
    first =, so only names are left), or a search that prints counts, file names or nothing (grep -c/-l/-L/-q)."""
    if prog == 'sed':
        scripts, script_files, _, _ = sed_arguments(args)
        return bool(scripts) and not script_files and all(NAMES_ONLY_SED.match(x.strip()) for x in scripts)
    if prog in ('grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack'):
        s, l, _ = flag_parts(args, value_opts=SEARCH_VALUE | PATTERN_OPTS, value_letters=SEARCH_LETTERS.get(prog, ''))
        short, long_ = NO_CONTENT[prog]
        return bool(s & short or l & long_)
    return False


def supplied_search_pattern(prog, args):
    takes = SEARCH_LETTERS.get(prog, 'efFv')
    for arg in args:
        if arg == '--':
            break
        if arg in PATTERN_OPTS or arg.startswith(('--regexp=', '--file=', '--expression=')):
            return True
        if arg.startswith('-') and not arg.startswith('--'):
            first = next((ch for ch in arg[1:] if ch in takes), None)
            if first in ('e', 'f'):
                return True
    return False


def set_cwd(value, candidates=None):
    STATE['cwd'], STATE['cwd_candidates'] = value, candidates


def printf_output(args):
    """Reproduce only printf's literal escapes, %s and %%; unsupported formatting is unknown output."""
    if args[:1] == ['--']:
        args = args[1:]
    if not args or args[0].startswith('-'):
        return None
    values = [expand(arg) for arg in args]
    if any(unresolved_path(value, arg) for value, arg in zip(values, args)):
        return None
    fmt, values = values[0], values[1:]
    parts, fields, i = [], 0, 0
    escapes = {'n': '\n', 't': '\t', 'r': '\r', 'a': '\a', 'b': '\b', 'f': '\f', 'v': '\v', '\\': '\\'}
    while i < len(fmt):
        ch = fmt[i]
        if ch == '%':
            if i + 1 == len(fmt) or fmt[i + 1] not in ('s', '%'):
                return None
            fields += fmt[i + 1] == 's'
            parts.append(None if fmt[i + 1] == 's' else '%')
            i += 2
        elif ch == '\\':
            if i + 1 == len(fmt) or fmt[i + 1] not in escapes:
                return None
            parts.append(escapes[fmt[i + 1]])
            i += 2
        else:
            parts.append(ch)
            i += 1
    output, index, length = [], 0, 0
    while True:
        for part in parts:
            value = part
            if part is None:
                value = values[index] if index < len(values) else ''
                index += 1
            length += len(value)
            if length > MAX_CHARS:
                return None
            output.append(value)
        if not fields or index >= len(values):
            return ''.join(output)


def substitution_spans(text, processes=False):
    """Active substitution spans, preserving source quotes and execution order for the containing command."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '\\':
            i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif text.startswith('$(', i) or (processes and text[i:i + 2] in ('<(', '>(')):
            end = _close_paren(text, i + 2)
            out.append((i, min(n, end + 1), text[i + 2:end], ch != '$'))
            i = end + 1
        elif ch == '`':
            j = i + 1
            while j < n and text[j] != '`':
                j += 2 if text[j] == '\\' else 1
            out.append((i, min(n, j + 1), re.sub(r'\\([`$\\])', r'\1', text[i + 1:j]), False))
            i = j + 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == '\\':
                    j += 2
                elif text.startswith('$(', j):
                    end = _close_paren(text, j + 2)
                    out.append((j, min(n, end + 1), text[j + 2:end], False))
                    j = end + 1
                elif text[j] == '`':
                    k = j + 1
                    while k < n and text[k] != '`':
                        k += 2 if text[k] == '\\' else 1
                    out.append((j, min(n, k + 1), re.sub(r'\\([`$\\])', r'\1', text[j + 1:k]), False))
                    j = k + 1
                else:
                    j += 1
            i = j + 1
        else:
            i += 1
    return out


def execution_tokens(norm):
    """Keep generated words opaque, with their scripts attached to the exact command that executes them."""
    parts, scripts, last, number = [], {}, 0, 0
    for start, end, body, process in substitution_spans(norm, processes=True):
        marker = '__agent_core_substitution_%d' % number
        while marker in norm or marker in STATE['vars']:
            number += 1
            marker = '__agent_core_substitution_%d' % number
        number += 1
        parts.extend((norm[last:start], '${' + marker + '}'))
        scripts[marker] = (body, process)
        last = end
    parts.append(norm[last:])
    masked = ''.join(parts)
    tokens = tokens_of(masked)
    note_quoting(masked, tokens)
    associated = set()
    for token in tokens:
        matches = {marker: item for marker, item in scripts.items() if '${' + marker + '}' in token}
        associated.update(matches)
        token.substitution_scripts = tuple(body for body, _ in matches.values())
        token.process_substitution = any(process for _, process in matches.values())
    if associated != set(scripts):
        raise TooDeep()  # malformed scope must not discard an extracted script instead of checking it
    return tokens


def command_substitutions(cmd):
    for token in cmd:
        yield from getattr(token, 'substitution_scripts', ())
        if token.startswith(HEREDOC_MARK):
            try:
                index = int(token[len(HEREDOC_MARK):])
            except ValueError:
                continue
            if index < len(STATE['expands']) and STATE['expands'][index]:
                yield from substitutions(STATE['heredocs'][index])


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
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                         'permissionDecisionReason': 'agent-core: Guard could not check this action. See the hook error log.'}}))
        common.log_error('guard', exc)


if __name__ == '__main__':
    main()
