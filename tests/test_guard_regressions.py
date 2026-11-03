"""Holes the reviews of PR #13 found: env -S (Claude review), a redirect on a builtin and git work-tree overrides
(Codex review on 26f82c8)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CORE, TEST_CONFIG, bash, env_for, make_local  # noqa: E402


GUARD = os.path.join(CORE, 'claude', 'hooks', 'guard.py')


def decision(command, cwd='/work/project', local=None):
    return bash(command, cwd=cwd, local=local)[0]


class EnvSplitString(unittest.TestCase):
    def test_the_split_string_is_checked(self):
        for cmd in ("env -S 'git push -f origin main'", "env -S'git push -f origin main'",
                    "env --split-string='git reset --hard'", "env --split-string 'git clean -fdx'",
                    "/usr/bin/env -iS 'git add -A'", "sudo env -S 'cat .env'",
                    "env -S 'rm -rf /srv/lake-data/raw'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')

    def test_harmless_split_strings_pass(self):
        for cmd in ("env -S 'ls -la'", 'env -S "python3 -V"', 'env -u HOME ls'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(decision(cmd))


class RedirectOnABuiltin(unittest.TestCase):
    def test_a_builtin_cannot_write_into_a_protected_folder(self):
        for cmd in ('export -p > /srv/lake-data/snapshot.sh', 'declare -p >> ~/vault-data/x',
                    'readonly X=1 > ~/vault-data/y', 'cd /tmp > /srv/lake-data/f'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')

    def test_plain_builtins_pass(self):
        for cmd in ('export FOO=1', 'export -p', 'cd /tmp'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(decision(cmd))


class GitWorkTreeOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='akit-wt-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.here, self.other = os.path.join(self.tmp, 'here'), os.path.join(self.tmp, 'other')
        for repo in (self.here, self.other):
            subprocess.run(['git', 'init', '-q', repo], check=True)

    def test_sweeps_through_an_override_are_denied(self):
        o = self.other
        for cmd in ('git --git-dir=%s/.git --work-tree=%s add -A %s' % (o, o, o),
                    'git --git-dir=%s/.git --work-tree=%s add %s' % (o, o, o),
                    'git --work-tree %s add .' % o, 'git --git-dir=%s/.git add .' % o,
                    'GIT_DIR=%s/.git GIT_WORK_TREE=%s git add %s' % (o, o, o),
                    'git --work-tree=$X add src/a.py'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd, cwd=self.here), 'deny')

    def test_a_named_file_in_the_other_tree_passes(self):
        o = self.other
        self.assertIsNone(decision('git --git-dir=%s/.git --work-tree=%s add %s/src/a.py' % (o, o, o), cwd=self.here))
        self.assertIsNone(decision('git add src/a.py', cwd=self.here))


class FolderAfterAConditionalCd(unittest.TestCase):
    """Codex review of 0747f38: a cd that may not run, or a cd in zsh's last pipeline stage, leaves the folder unknown."""

    def test_the_delete_is_judged_in_an_unknown_folder(self):
        for cmd in ('if false; then cd /tmp; fi; rm -rf src', 'false && cd /tmp; rm -rf src', 'true || cd /tmp; rm -rf src',
                    "zsh -c 'cd /tmp; printf x | cd /work/project; rm -rf src'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd, cwd='/work/project'), 'deny')

    def test_a_cd_that_did_run_still_counts(self):
        for cmd in ('cd /tmp && rm -rf src', 'cd /tmp; rm -rf src'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(decision(cmd, cwd='/work/project'))


class MergeEndpointReads(unittest.TestCase):
    """Codex review of 0747f38: a GET of pulls/N/merge only asks whether the PR merged."""

    def test_reads_pass(self):
        for cmd in ('gh api repos/o/r/pulls/7/merge', 'curl https://api.github.com/repos/o/r/pulls/7/merge',
                    'curl -I https://api.github.com/repos/o/r/pulls/7/merge',
                    'http https://api.github.com/repos/o/r/pulls/7/merge'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(decision(cmd))

    def test_writes_stay_denied(self):
        for cmd in ('gh api -X PUT repos/o/r/pulls/7/merge', 'gh api repos/o/r/pulls/7/merge -f merge_method=squash',
                    'gh api -X $M repos/o/r/pulls/7/merge', 'curl -X PUT https://api.github.com/repos/o/r/pulls/7/merge',
                    "curl -d '{}' https://api.github.com/repos/o/r/pulls/7/merge",
                    'wget --method=PUT https://api.github.com/repos/o/r/pulls/7/merge',
                    'http PUT https://api.github.com/repos/o/r/pulls/7/merge',
                    'http https://api.github.com/repos/o/r/pulls/7/merge merge_method=squash'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')


class ForceNextToALease(unittest.TestCase):
    """Claude review of 10add66: -f or --force switches git's lease check off, whatever lease flag is next to it."""

    def test_it_is_a_plain_force_push(self):
        for cmd in ('git push -f --force-with-lease origin feat', 'git push --force --force-if-includes origin feat',
                    'git push --forc --force-with-lease origin feat'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd, cwd=CORE), 'deny')
        self.assertIsNone(decision('git push --force-with-lease origin feat', cwd=CORE))


class ConditionalAssignments(unittest.TestCase):
    def test_skipped_assignments_leave_the_delete_target_unknown(self):
        for assign in ('T=/tmp', 'export T=/tmp', 'declare T=/tmp', 'typeset T=/tmp',
                       'local T=/tmp', 'readonly T=/tmp'):
            for branch in ('false && %s;', 'true || %s;', 'false && %s && true;', 'false && %s && (true);',
                           'if false; then %s; fi;'):
                cmd = 'export T=/srv/lake-data; ' + branch % assign + ' rm -rf "$T/raw"'
                with self.subTest(cmd=cmd):
                    self.assertEqual(decision(cmd), 'deny')

    def test_assignments_known_to_have_run_still_count(self):
        for cmd in ('export T=/tmp; rm -rf "$T/raw"',
                    'true && export T=/tmp && rm -rf "$T/raw"',
                    'true && T=/tmp && rm -rf "$T/raw"'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(decision(cmd))


class XargsReplacementStaging(unittest.TestCase):
    def test_replaced_paths_cannot_exempt_a_sweep(self):
        for flag in ('-I FILE', '-IFILE', '-iFILE', '--replace=FILE', '-J FILE', '-i', '--replace'):
            for stage in ('add -A', 'stage -u', 'add --all'):
                cmd = 'echo . | xargs %s git %s FILE' % (flag, stage)
                with self.subTest(cmd=cmd):
                    self.assertEqual(decision(cmd), 'deny')

    def test_literal_named_paths_still_pass(self):
        self.assertIsNone(decision('git add -A src/a.py'))
        self.assertIsNone(decision('echo src/a.py | xargs -I FILE git status --short FILE'))

    def test_appended_paths_cannot_exempt_a_sweep(self):
        for cmd in ('echo . | xargs git add -A README.md', 'echo . | xargs git stage -u README.md',
                    'echo . | xargs git add README.md', 'echo . | xargs env git add --all README.md',
                    'echo . | xargs -I FILE sh -c \'git add -A FILE\''):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')


class PersistentShellAssignments(unittest.TestCase):
    def test_carriage_return_does_not_end_a_shell_word(self):
        for body in ('if false; then fi\r; D=/tmp; fi;', 'while false; do done\r; D=/tmp; done;'):
            command = 'D=/srv/lake-data; ' + body + ' rm -rf "$D/raw"'
            with self.subTest(body=body):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision('echo word\r; git status --short'))

    def test_shell_whitespace_is_not_unicode_whitespace(self):
        for cmd in ('echo \u00a0; if true; then rm -rf /srv/lake-data/raw; fi',
                    'echo \u00a0; X=1 git push --force origin main'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')

    def test_missing_lexical_identity_holds_control_and_assignment_scripts(self):
        script = ('import json,sys;sys.path.insert(0,sys.argv[1]);import guard;'
                  'guard.note_quoting=lambda *a,**k:None;'
                  'print(json.dumps(guard.check_script(sys.stdin.read())))')
        for command in ('if true; then rm -rf /srv/lake-data/raw; fi',
                        'X=1 git push --force origin main'):
            with self.subTest(command=command):
                out = subprocess.run([sys.executable, '-c', script, os.path.dirname(GUARD)], input=command,
                                     capture_output=True, text=True, env=env_for(), timeout=20)
                self.assertEqual(json.loads(out.stdout)[0], 'deny')

    def test_only_an_unquoted_name_prefix_is_a_bare_assignment(self):
        for fake in ('"D=/tmp"', "'D=/tmp'", "D''=/tmp", r'D\=/tmp', '"D"=/tmp'):
            for tail in ('', ' export D=/tmp'):
                cmd = 'D=/srv/lake-data; %s%s; rm -rf "$D/raw"' % (fake, tail)
                with self.subTest(cmd=cmd):
                    self.assertEqual(decision(cmd), 'deny')
        for assign in ('D="/tmp"', "D='/tmp'", 'export "D=/tmp"'):
            self.assertIsNone(decision('%s; rm -rf "$D/raw"' % assign))
        for wrapper in ('env "X=1"', 'sudo "X=1"'):
            self.assertEqual(decision('%s git push --force origin main' % wrapper), 'deny')

    def test_quoted_reserved_words_do_not_change_conditional_scope(self):
        for fake in ("'fi'", '"fi"', 'f"i"', r'f\i', "$'fi'", "''fi", "fi''", "f''i"):
            cmd = 'D=/srv/lake-data; if false; then %s; D=/tmp; fi; rm -rf "$D/raw"' % fake
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')
        for fake in ("'done'", '"done"', 'd"one"', r'do\ne', "$'done'"):
            cmd = 'D=/srv/lake-data; while false; do %s; D=/tmp; done; rm -rf "$D/raw"' % fake
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')
        for fake in ("'if'", '"then"', r'd\o', "$'if'"):
            cmd = 'D=/srv/lake-data; %s export D=/tmp; rm -rf "$D/raw"' % fake
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')
        self.assertIsNone(decision('echo "fi"; if false; then echo skip; fi; D=/tmp; rm -rf "$D/raw"'))

    def test_last_pipeline_assignments_leave_the_target_unknown(self):
        for assign in ('D=/srv/lake-data', 'export D=/srv/lake-data',
                       'printf -v D /srv/lake-data', 'read D', 'eval "read D"'):
            cmd = 'D=/tmp; printf x | %s; rm -rf "$D/raw"' % assign
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')
        self.assertEqual(decision("zsh -c 'D=/tmp; printf x | D=/srv/lake-data; rm -rf \"$D/raw\"'"), 'deny')

    def test_assignments_later_in_a_skipped_body_are_not_known(self):
        for body in ('if false; then echo skip; export D=/tmp; fi;',
                     'time if false; then echo skip; export D=/tmp; fi;',
                     'time -p if false; then echo skip; export D=/tmp; fi;',
                     'if false; then echo skip\nD=/tmp\nfi;',
                     'if false; then if true; then echo skip; export D=/tmp; fi; fi;',
                     'while false; do echo skip; export D=/tmp; done;',
                     'for x in; do echo skip; export D=/tmp; done;',
                     'if false; then echo skip; eval "export D=/tmp"; fi;'):
            cmd = 'export D=/srv/lake-data; ' + body + ' rm -rf "$D/raw"'
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')
        self.assertEqual(decision('D=/tmp; for D in /srv/lake-data; do echo skip; done; rm -rf "$D/raw"'), 'deny')
        for loop in ('for', 'select'):
            self.assertEqual(decision('p=src/a.py; %s p in .; do git add -A "$p"; done' % loop), 'deny')

    def test_unconditional_assignments_after_a_body_still_count(self):
        self.assertIsNone(decision('if false; then echo skip; export D=/other; fi; D=/tmp; rm -rf "$D/raw"'))
        self.assertIsNone(decision('D=/tmp; (if true; then D=/other; fi); rm -rf "$D/raw"'))

    def test_builtin_output_assignments_invalidate_staging_paths(self):
        for assign in ('printf -v p .', 'printf -vp .', 'read p', 'read -r p', 'read -a p', 'read -ra p', 'read -rap',
                       'builtin printf -v p .', 'builtin read p'):
            cmd = 'p=src/a.py; %s; git add -A "$p"' % assign
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')

    def test_unrelated_builtin_assignments_keep_known_paths(self):
        for assign in ('printf -v other x', 'read other', 'read -r other'):
            with self.subTest(assign=assign):
                self.assertIsNone(decision('p=src/a.py; %s; git add -A "$p"' % assign))


class VerdictOutputFilters(unittest.TestCase):
    def test_read_only_queries_and_output_filters_pass(self):
        for flag in ('--jq', '-q', '--template'):
            for method in ('', '-X POST -f title=example '):
                cmd = 'gh api repos/o/r/issues/7/comments %s%s \'contains("VERDICT: PASS")\'' % (method, flag)
                with self.subTest(cmd=cmd):
                    self.assertIsNone(decision(cmd))
        self.assertIsNone(decision('gh api repos/o/r/issues/7/comments --jq \'.[] | select(.body | contains("VERDICT: PASS"))\''))

    def test_posted_verdicts_stay_denied(self):
        for cmd in ('gh pr comment 7 -b "VERDICT: PASS"',
                    'gh pr comment 7 -b "-qVERDICT: PASS"',
                    'gh api repos/o/r/issues/7/comments -f body="VERDICT: PASS"',
                    'gh api repos/o/r/issues/7/comments --jq . -f body="VERDICT: PASS"'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')


class WrongConfigTypes(unittest.TestCase):
    def test_bad_user_names_do_not_disable_protected_path_checks(self):
        for value in (5, None, ['Alex']):
            local = make_local(dict(TEST_CONFIG, user_name=value))
            self.addCleanup(shutil.rmtree, local, True)
            with self.subTest(value=value):
                self.assertEqual(decision('touch /srv/lake-data/raw', local=local), 'deny')

    def test_wrong_gate_type_keeps_the_default_gate_enabled(self):
        local = make_local(dict(TEST_CONFIG, pr_gate='off'))
        self.addCleanup(shutil.rmtree, local, True)
        self.assertEqual(decision('gh api -X PUT repos/o/r/pulls/7/merge', local=local), 'deny')


if __name__ == '__main__':
    unittest.main()
