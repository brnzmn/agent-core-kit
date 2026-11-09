"""Executable SQL and shell lines must remain visible after substitution and heredoc normalization."""
import sqlite3
import unittest

import helpers
import test_guard_review_inputs as inputs

class SqlOperandsAndHeredocs(unittest.TestCase):
    setUp = inputs.ReviewedGuardInputs.setUp
    decide = inputs.ReviewedGuardInputs.decide

    def test_substituted_sql_is_not_hidden_by_shell_markers(self):
        for command in ('psql -c "$(printf \'DROP TABLE records\')"',
                        'psql --command="$(printf \'DROP TABLE records\')"',
                        'psql -c"$(printf \'DROP TABLE records\')"',
                        'psql -c "`printf \'DROP TABLE records\'`"',
                        'SQL=$(printf \'DROP TABLE records\'); psql -c "$SQL"',
                        'psql -c "$QUERY"',
                        'mysql --execute="$(printf \'DROP TABLE records\')"'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')

    def test_substituted_sql_cannot_bypass_protected_sqlite_writes(self):
        db = self.lake / 'records.sqlite'
        with sqlite3.connect(db) as connection:
            connection.execute('create table records (value text)')
            connection.execute("insert into records values ('fixture')")
        for command in ('sqlite3 lake-data/records.sqlite "$(printf \'DELETE FROM records\')"',
                        'SQL=$(printf \'DELETE FROM records\'); sqlite3 lake-data/records.sqlite "$SQL"'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        with sqlite3.connect(db) as connection:
            self.assertEqual(connection.execute('select count(*) from records').fetchone()[0], 1)

    def test_sql_operand_checks_preserve_known_reads_and_connection_values(self):
        for command in ('psql -c "SELECT 1"', 'QUERY="SELECT 1"; psql -c "$QUERY"',
                        'psql -d "$DATABASE_URL" -c "SELECT 1"',
                        'psql --dbname="$DATABASE_URL" --command="SELECT 1"',
                        'psql -v ON_ERROR_STOP=1 -c "SELECT 1"',
                        'sqlite3 lake-data/records.sqlite "SELECT count(*) FROM records"'):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_unknown_psql_sql_variables_are_not_connection_values(self):
        for option in ('-v QUERY="$(printf \'DROP TABLE records;\')"',
                       '--set=QUERY="$(printf \'DROP TABLE records;\')"',
                       '--variable=QUERY="$(printf \'DROP TABLE records;\')"'):
            command = "psql %s <<'SQL'\n:QUERY\nSQL" % option
            with self.subTest(option=option):
                self.assertEqual(self.decide(command), 'deny')

    def test_connection_and_variable_options_do_not_replace_sql_stdin(self):
        for client in ('psql -v ON_ERROR_STOP=1', 'psql --set=ON_ERROR_STOP=1',
                       'psql --variable=ON_ERROR_STOP=1', 'psql -d app -U reader',
                       'psql app reader', 'psql -e app', 'mysql app', 'mysql -C app',
                       'psql -f -', 'psql --file=-'):
            opaque = 'printf "%s" "$(printf \'DROP TABLE records\')" | ' + client
            with self.subTest(client=client):
                self.assertEqual(self.decide(opaque), 'deny')
                self.assertEqual(self.decide('printf "%s" "DROP TABLE records;" | ' + client), 'deny')
                self.assertIsNone(self.decide('printf "%s" "SELECT 1;" | ' + client))

    def test_expanding_stdin_producers_require_proven_sql_bytes(self):
        for command in ('QUERY=$(printf \'DROP TABLE records\'); cat <<SQL | psql\n$QUERY\nSQL',
                        'QUERY=$(printf \'DROP TABLE records\'); cat <<<"$QUERY" | psql'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide("cat <<'SQL' | psql\nSELECT 1;\nSQL"))
        self.assertIsNone(self.decide('QUERY="SELECT 1;"; cat <<SQL | psql\n$QUERY\nSQL'))

    def test_unquoted_connection_values_cannot_add_sql_arguments(self):
        for connection in ('-h $HOST', '--host=$HOST', '$DATABASE_URL'):
            with self.subTest(connection=connection):
                self.assertEqual(self.decide('psql %s -c "SELECT 1"' % connection), 'deny')
        self.assertIsNone(self.decide('psql -h "$HOST" -c "SELECT 1"'))
        self.assertIsNone(self.decide('psql -d "$DATABASE_URL" -c "SELECT 1"'))
        self.assertIsNone(self.decide('psql -c "SELECT 1" -- "$DATABASE_URL"'))

    def test_quoted_unknown_positionals_cannot_become_sql_options(self):
        for command in ('DATABASE_URL=$(printf "%s" "-cDROP/**/TABLE/**/records"); psql "$DATABASE_URL" -c "SELECT 1"',
                        'docker exec db psql "$DATABASE_URL" -c "SELECT 1"'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')

    def test_unknown_sql_files_stdin_and_wrappers_cannot_skip_checks(self):
        (self.cwd / 'read.sql').write_text('SELECT 1;\n')
        for command in ('psql -f "$(printf read.sql)"', 'psql < "$(printf read.sql)"',
                        'printf "%s" "$(printf \'DROP TABLE records\')" | psql',
                        'psql <<SQL\n$(printf \'DROP TABLE records\')\nSQL',
                        'npx psql -c "$(printf \'DROP TABLE records\')"',
                        'docker exec db psql -c "$(printf \'DROP TABLE records\')"'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        for command in ('psql -f read.sql', 'psql < read.sql', 'printf "%s" "SELECT 1" | psql',
                        'psql <<\'SQL\'\nSELECT 1;\nSQL', 'docker exec db psql -c "SELECT 1"'):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_sql_package_runners_preserve_operand_roles(self):
        for runner in ('npx', 'npx -p postgres', 'pnpx', 'bunx', 'pnpm exec',
                       'yarn exec', 'npm exec --', 'bun x'):
            with self.subTest(runner=runner):
                self.assertEqual(self.decide(runner + ' psql -c "$QUERY"'), 'deny')
                self.assertIsNone(self.decide(runner + ' psql -d "$DATABASE_URL" -c "SELECT 1"'))

    def test_quoted_and_comment_heredoc_lookalikes_cannot_hide_commands(self):
        for command in ('ls # <<EOF\ngit reset --hard',
                        'echo "<<EOF"\ngit reset --hard',
                        "echo '<<EOF'\nrm -rf lake-data/raw",
                        'echo "first line\n<<EOF"\ngit reset --hard',
                        'echo \\<<EOF\ngit reset --hard'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')

    def test_unterminated_real_heredocs_fail_closed(self):
        for command in ('cat <<EOF\nordinary data', "cat <<'EOF'\nordinary data", 'cat <<-EOF\n\tordinary data'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')

    def test_arithmetic_shifts_are_not_heredoc_openers(self):
        self.assertIsNone(self.decide('echo $((1 << 2))'))
        self.assertEqual(self.decide('echo $((1 << 2)); git reset --hard'), 'deny')
        self.assertEqual(self.decide('echo $(( $(git reset --hard) + 1))'), 'deny')

    def test_real_heredoc_quoting_tabs_and_multiple_bodies_are_preserved(self):
        for command in ("cat <<'EOF'\ngit reset --hard\nEOF",
                        'cat <<"EOF"\ngit reset --hard\nEOF',
                        'cat <<\\EOF\ngit reset --hard\nEOF',
                        'cat <<-EOF\n\tordinary data\n\tEOF',
                        'cat <<FIRST <<\'SECOND\'\nfirst data\nFIRST\nsecond data\nSECOND',
                        'echo "<<EOF"\necho ordinary', 'ls # <<EOF\necho ordinary'):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))
        self.assertEqual(self.decide("cat <<'EOF'\nordinary data\nEOF\ngit reset --hard"), 'deny')

    def test_literal_heredoc_text_inside_quoted_substitutions_is_data(self):
        bodies = ("Don't break it", "it's fine", 'steps:\n1) first\n2) second', 'an unmatched ( paren',
                  'use `ls` and `git status` here', "Don't (maybe\n- 1) item `x`\n\"quoted\" # not a comment")
        for body in bodies:
            for command in ("git commit -m \"$(cat <<'EOF'\n%s\nEOF\n)\"" % body,
                            "git commit -m \"$(cat <<-'EOF'\n\t%s\n\tEOF\n)\"" % body):
                with self.subTest(command=command):
                    self.assertIsNone(helpers.bash(command)[0])  # the lane scratch dir would deny any git commit
            for command in ("gh pr create --title t --body \"$(cat <<'EOF'\n%s\nEOF\n)\"" % body,
                            "echo \"$(cat <<'EOF'\n%s\nEOF\n)\" \"$(cat <<'EOF'\nsecond: it's\nEOF\n)\"" % body):
                with self.subTest(command=command):
                    self.assertIsNone(self.decide(command))
        self.assertIsNone(self.decide("echo \"$(echo \"$(cat <<'EOF'\nDon't (1)\nEOF\n)\")\""))

    def test_commands_around_and_inside_quoted_substitution_heredocs_still_run_checks(self):
        for command in ("git commit -m \"$(cat <<'EOF'\nDon't\nEOF\n)\" && git reset --hard",
                        "git commit -m \"$(cat <<'EOF'\nDon't\nEOF\n)\"\ngit reset --hard"):
            with self.subTest(command=command):
                decision, out = helpers.bash(command)
                self.assertEqual(decision, 'deny')
                self.assertIn('reset', out['hookSpecificOutput']['permissionDecisionReason'].lower())
        for command in ("echo \"$(cat <<'EOF'\nDon't\nEOF\ngit reset --hard\n)\"",
                        "echo \"$(cat <<'EOF'\nDon't\nEOF\n) $(git reset --hard)\"",
                        "echo \"$(git reset --hard; cat <<'EOF'\nDon't\nEOF\n)\"",
                        "echo \"$(cat <<EOF\n$(git reset --hard)\nEOF\n)\"",
                        "echo \"$(cat <<EOF\n`git reset --hard`\nEOF\n)\"",
                        "echo \"$(echo \"$(cat <<'EOF'\nDon't\nEOF\ngit reset --hard\n)\")\""):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        self.assertIsNone(self.decide("echo \"$(cat <<'EOF'\n$(git reset --hard)\nEOF\n)\""))

    def test_substitution_heredoc_lookalikes_and_shifts_cannot_hide_commands(self):
        for command in ('echo "$(true # <<EOF\n)"\ngit reset --hard',
                        'echo "$(echo \'<<EOF\')"\ngit reset --hard',
                        'echo "$(echo "<<EOF")"\ngit reset --hard',
                        'echo "$(echo \\<<EOF)"\ngit reset --hard',
                        'echo "\\$(cat <<EOF"\ngit reset --hard',
                        'echo "$((1 << 2))"\ngit reset --hard',
                        'echo "$(echo $((1 << 2)))"\ngit reset --hard',
                        'echo \'$(cat <<EOF\'\ngit reset --hard',
                        'echo "$(cat <<EOF\ndata\n)"'):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), 'deny')
        for command in ('echo "$((1 << 2))"', 'echo "$(echo $((1 << 2)))"', 'echo "$(echo "<<EOF")"',
                        "echo \"$(cat <<'EOF'\nok\nEOF\n)\" \"<<EOF\""):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))


if __name__ == '__main__':
    unittest.main()
