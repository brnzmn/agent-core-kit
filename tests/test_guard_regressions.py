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

    def test_prefix_environment_only_collects_assignment_positions(self):
        body = 'GIT_CONFIG_COUNT=0; true; GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=+HEAD:main git push origin'
        for prefix in ('export GIT_CONFIG_COUNT=$X;', 'export GIT_CONFIG_COUNT=1;'):
            command = prefix + " bash -c '" + body + "'"
            with self.subTest(command=command):
                self.assertIsNone(decision(command))
        for option in ('-p', '--prompt'):
            for count, prompt, expected in ((0, 1, None), (1, 0, 'deny')):
                command = ('GIT_CONFIG_COUNT=%s GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=+HEAD:main '
                           'sudo -E %s \'GIT_CONFIG_COUNT=%s\' git push origin HEAD') % (count, option, prompt)
                with self.subTest(command=command):
                    self.assertEqual(decision(command), expected)
        for wrapper in ('env', 'sudo -u root', 'timeout -s TERM 5 env'):
            command = (wrapper + ' "GIT_CONFIG_COUNT=1" "GIT_CONFIG_KEY_0=remote.origin.push" '
                       '"GIT_CONFIG_VALUE_0=+HEAD:main" git push origin')
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')

    def test_fresh_shells_retain_git_config_inheritance_without_trusting_locals(self):
        body = 'true; GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=+HEAD:main git push origin'
        for prefix in ('export GIT_CONFIG_COUNT=$X;', 'export GIT_CONFIG_COUNT=1;', 'GIT_CONFIG_COUNT=1;'):
            command = prefix + " bash -c '" + body + "'"
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        for command in ('export GIT_CONFIG_COUNT=0; bash -c \'%s\'' % body,
                        'export GIT_CONFIG_COUNT=$X; bash -c \'true; GIT_CONFIG_COUNT=0; %s\'' % body,
                        'readonly GIT_CONFIG_COUNT=1; bash -c \'true; GIT_CONFIG_COUNT=0; %s\'' % body):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))
        self.assertEqual(decision('P=README.md; bash -c \'true; git add -A "$P"\''), 'deny')
        self.assertIsNone(decision('readonly P=.; bash -c \'P=README.md; git add -A "$P"\''))

    def test_unknown_git_config_assignments_remain_visible_to_push_checks(self):
        for command in ('GIT_CONFIG_COUNT=$((100+1)) GIT_CONFIG_KEY_100=remote.origin.push '
                        'GIT_CONFIG_VALUE_100=+HEAD:main git push origin',
                        'GIT_CONFIG_COUNT=$X; GIT_CONFIG_KEY_0=remote.origin.push '
                        'GIT_CONFIG_VALUE_0=+HEAD:main git push origin',
                        'export GIT_CONFIG_COUNT="$X"; GIT_CONFIG_KEY_0=remote.origin.push '
                        'GIT_CONFIG_VALUE_0=+HEAD:main git push origin',
                        'GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=$K GIT_CONFIG_VALUE_0=true git push origin',
                        'GIT_CONFIG_PARAMETERS=$P; git push origin'):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        for prefix in ('GIT_CONFIG_COUNT=$X; GIT_CONFIG_COUNT=0;', '(GIT_CONFIG_COUNT=$X);'):
            self.assertIsNone(decision(prefix + ' GIT_CONFIG_KEY_0=remote.origin.push '
                                       'GIT_CONFIG_VALUE_0=+HEAD:main git push origin HEAD'))

    def test_active_unknown_assignment_values_do_not_become_literal_paths(self):
        for assign in ('F=$X', 'export F="$X"', 'declare F="$X"', 'readonly F="$X"',
                       'export A=README.md F="$A"'):
            command = '%s; git add "$F"' % assign
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision('P=README.md; F=$P; git add -A "$F"'))
        self.assertIsNone(decision('F=\'$X\'; git add -A "$F"'))

    def test_nested_shell_scripts_expand_in_their_own_scope(self):
        for command in ('p=README.md; bash -c \'p=.; git add -A "$p"\'',
                        'p=README.md; suffix=x; bash -c \'p=.; git add -A "$p"; echo \'"$suffix"',
                        'P=README.md; bash -c \'git add -A $P\''):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')

    def test_literal_and_outer_expansions_stay_distinct(self):
        for command in ('p=README.md; bash -c "p=.; git add -A \\"$p\\""',
                        'p=.; echo "$p"; git add -A \'$p\'',
                        'p=\'$q\'; q=.; git add -A "$p"',
                        'bash -c \'p=README.md; git add -A "$p"\''):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))

    def test_failed_readonly_assignments_cannot_establish_safe_paths(self):
        for declaration in ('readonly p=.', 'declare -r p=.', 'typeset -r p=.', 'p=.; readonly p'):
            for assign in ('export p=README.md', 'p=README.md', 'declare p=README.md'):
                command = '%s; %s; git add -A "$p"' % (declaration, assign)
                with self.subTest(command=command):
                    self.assertEqual(decision(command), 'deny')
        for command in ('p=README.md; printf x | readonly p=.; export p=README.md; git add -A "$p"',
                        'p=.; printf x | readonly p; export p=README.md; git add -A "$p"'):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        for command in ('readonly p=README.md; git add -A "$p"',
                        '(readonly p=.); p=README.md; git add -A "$p"',
                        'readonly p=.; bash -c \'p=README.md; git add -A "$p"\''):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))

    def test_unproven_declaration_forms_cannot_establish_safe_paths(self):
        for declaration in ('readonly -f p=README.md', 'declare -fr p=README.md', 'export -f p=README.md',
                            'declare -p p=README.md', 'local p=README.md', 'export -z p=README.md'):
            command = 'p=.; %s; git add -A "$p"' % declaration
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')

    def test_unset_variable_selection_invalidates_cached_paths(self):
        for unset in ('unset D', 'unset -v D', 'unset -- D', 'builtin unset D'):
            command = 'export D=/tmp; %s; rm -rf "$D/data"' % unset
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision('D=/tmp; unset -f D; rm -rf "$D/data"'))

    def test_only_shell_whitespace_delimits_keywords(self):
        for cmd in ('echo \u00a0; if true; then rm -rf /srv/lake-data/raw; fi',
                    'echo \u00a0; X=1 git push --force origin main',
                    'D=/srv/lake-data; if false; then fi\r; D=/tmp; fi; rm -rf "$D/raw"',
                    'D=/srv/lake-data; if false; then \rfi; D=/tmp; fi; rm -rf "$D/raw"',
                    'D=/srv/lake-data; while false; do done\r; D=/tmp; done; rm -rf "$D/raw"'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), 'deny')
        self.assertIsNone(decision('echo word\r'))

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


class KitReviewGuardRegressions(unittest.TestCase):
    def test_xargs_readers_keep_unknown_paths_unresolved(self):
        for command in (r'D=README.md; printf -v D .; xargs git add -A "$D"',
                        r'D=README.md; printf -v D .env; xargs cat "$D"'):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        for command in (r'D=README.md; xargs cat "$D"',
                        r"xargs cat '$D'"):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))

    def test_invalidated_write_paths_keep_their_expansion_provenance(self):
        self.assertEqual(decision('TMPDIR=/srv/lake-data; printf -v TMPDIR /srv/lake-data; rm -rf "$TMPDIR/raw"'), 'deny')
        prefix = 'D=/srv/lake-data; printf -v D /srv/lake-data; '
        for writer in ('touch "$D/raw"', 'printf x | tee "$D/raw"', 'printf x > "$D/raw"',
                       'cp input "$D/raw"', 'mv input "$D/raw"',
                       'cp --target-directory="$D" input', 'mv -t"$D" input'):
            with self.subTest(writer=writer):
                self.assertEqual(decision(prefix + writer), 'deny')
        for command in ('D=/srv/lake-data; printf "%s" "$D"; touch \'$D/raw\'',
                        'D=/srv/lake-data; printf -v D /srv/lake-data; printf "$D" > \'$D/raw\'',
                        'D=/srv/lake-data; printf -v D /srv/lake-data; cp input \'$D/raw\'',
                        'D=/tmp/guard-control; touch "$D"'):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))

    def test_unknown_folder_cannot_hide_relative_protected_writes(self):
        local = make_local(dict(TEST_CONFIG, protected_dirs=['/etc']))
        self.addCleanup(shutil.rmtree, local, True)
        for command in ('if true; then cd /etc; fi; touch raw',
                        'if true; then cd /etc; fi; printf x > raw',
                        'if true; then cd /etc; fi; cp input raw',
                        'false && cd /tmp; touch raw'):
            with self.subTest(command=command):
                self.assertEqual(decision(command, local=local), 'deny')
        for command in ('if true; then cd /etc; fi; touch /tmp/guard-control',
                        'if true; then cd /etc; fi; cp input /tmp/guard-control',
                        'cd /tmp; touch guard-control'):
            with self.subTest(command=command):
                self.assertIsNone(decision(command, local=local))
        empty = make_local(dict(TEST_CONFIG, protected_dirs=[]))
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertIsNone(decision('if true; then cd /etc; fi; touch raw', local=empty))

    def test_literal_dollar_paths_still_resolve_under_a_protected_folder(self):
        local = make_local(dict(TEST_CONFIG, protected_dirs=['/etc']))
        self.addCleanup(shutil.rmtree, local, True)
        for command in ("cd /etc; touch '$D'", "cd /etc; touch '$HOME/raw'",
                        "cd /etc; printf x > '$D'", "cd /etc; cp input '$D'"):
            with self.subTest(command=command):
                self.assertEqual(decision(command, local=local), 'deny')
        self.assertIsNone(decision("cd /tmp; touch '$HOME/raw'", local=local))

    def test_unknown_reader_paths_cannot_hide_env_files(self):
        for reader in ('cat "$D"', 'sed -n 1p "$D"', 'grep value "$D"', 'awk -f "$D" data.txt'):
            command = 'D=.env; printf -v D .env; ' + reader
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        for command in ('D=README.md; cat "$D"', "D=.env; printf -v D .env; cat '$D'",
                        'D=.env; printf "%s" "$D"; cat \'$D\'',
                        'P=value; printf -v P value; grep -e "$P" README.md'):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))

    def test_http_method_spellings_cannot_turn_merge_writes_into_reads(self):
        endpoint = 'https://api.github.com/repos/o/r/pulls/7/merge'
        for command in ('curl -sX PUT ' + endpoint, 'curl -sXPUT ' + endpoint,
                        'curl -ivX PATCH ' + endpoint, 'wget --method PUT ' + endpoint,
                        'wget --method=PUT ' + endpoint, 'curl --data-b example ' + endpoint,
                        'wget --post-d=example ' + endpoint):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        for command in ('curl -sX GET ' + endpoint, 'curl -sXHEAD ' + endpoint,
                        'curl -H "X-Example: PUT" ' + endpoint,
                        'wget --method GET ' + endpoint, 'wget --method=HEAD ' + endpoint):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))


class ProvenPathFields(unittest.TestCase):
    def test_known_single_field_paths_keep_reads_and_writes_allowed(self):
        for command in ('D=/tmp/guard-control; cat $D/notes.txt',
                        'D=/tmp/guard-control; touch $D/raw',
                        'D=/tmp/guard-control; cp input $D/output',
                        'D=/tmp/guard-control; chmod 600 $D/output',
                        'G="$HOME/ordinary"; cp "$G" "$G.bak"',
                        'cat "$HOME/ordinary"',
                        'bash -c \'cat "$HOME/ordinary"\''):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))

    def test_unquoted_paths_require_a_known_nonempty_single_field(self):
        for command in ('D="/srv/lake-data /tmp"; touch $D/raw',
                        'D="/tmp/*"; touch $D/raw', 'D=""; touch $D',
                        'IFS=/; D=/tmp/guard-control; touch $D/raw',
                        'read IFS; D=/tmp/guard-control; touch $D/raw',
                        'unset IFS; D=/tmp/guard-control; touch $D/raw',
                        'declare -z other=x; D=/tmp/guard-control; touch $D/raw',
                        'D=/srv/lake-data; printf -v D /srv/lake-data; touch $D/raw',
                        'D=/srv/lake-data; touch $D/raw'):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision('D=/tmp/control\rword; touch $D/raw'))

    def test_home_fallback_respects_invalidations_and_child_overrides(self):
        for command in ('printf -v HOME /srv/lake-data; cat "$HOME/notes.txt"',
                        'unset HOME; cat "$HOME/notes.txt"',
                        'declare -p HOME; cat "$HOME/notes.txt"',
                        'HOME=/srv/lake-data; bash -c \'touch "$HOME/raw"\'',
                        'env HOME=/srv/lake-data bash -c \'touch "$HOME/raw"\'',
                        'export -n HOME; bash -c \'cat "$HOME/notes.txt"\'',
                        'export -n HOME=/tmp/control; bash -c \'cat "$HOME/notes.txt"\''):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        self.assertIsNone(decision('HOME=/tmp/guard-control; cat "$HOME/notes.txt"'))

    def test_home_prefix_changes_only_affect_the_child_environment(self):
        for command in ('env -u HOME bash -c \'cat "$HOME/ordinary"\'',
                        'env -vuHOME bash -c \'cat "$HOME/ordinary"\'',
                        'env -vu HOME bash -c \'cat "$HOME/ordinary"\'',
                        'env --unset=HOME bash -c \'cat "$HOME/ordinary"\'',
                        'env -i bash -c \'cat "$HOME/ordinary"\'',
                        'env --ignore-environment bash -c \'cat "$HOME/ordinary"\'',
                        'sudo bash -c \'cat "$HOME/ordinary"\'',
                        'doas sh -c \'cat "$HOME/ordinary"\'',
                        'declare -p HOME; bash -c \'cat "$HOME/ordinary"\''):
            with self.subTest(command=command):
                self.assertEqual(decision(command), 'deny')
        for command in ('env -u HOME cat "$HOME/ordinary"',
                        'sudo cat "$HOME/ordinary"',
                        'env -u OTHER bash -c \'cat "$HOME/ordinary"\''):
            with self.subTest(command=command):
                self.assertIsNone(decision(command))

    def test_unknown_env_unset_targets_hold_protected_destinations(self):
        local = make_local(dict(TEST_CONFIG, protected_dirs=['/srv/lake-data']))
        self.addCleanup(shutil.rmtree, local, True)
        for command in ('env --unset "$REMOVE" bash -c \'cp input "$HOME/srv/lake-data/raw"\'',
                        'env --unset="$REMOVE" bash -c \'cp input "$HOME/srv/lake-data/raw"\'',
                        'env -u "$REMOVE" bash -c \'cp input "$HOME/srv/lake-data/raw"\''):
            with self.subTest(command=command):
                self.assertEqual(decision(command, local=local), 'deny')
        self.assertIsNone(decision('REMOVE=OTHER; env --unset "$REMOVE" bash -c \'cp input "$HOME/srv/lake-data/raw"\'', local=local))

    def test_staging_still_holds_unquoted_expansions(self):
        self.assertEqual(decision('p=README.md; git add -A $p'), 'deny')


if __name__ == '__main__':
    unittest.main()
