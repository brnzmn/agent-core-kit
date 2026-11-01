"""secret.sh stores and reads tokens by agent/<provider>/<account>: the macOS keychain, or secret-tool on Linux.
The real stores are tested where they exist; a fake secret-tool on PATH covers the Linux code path anywhere else."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import BIN  # noqa: E402

S = os.path.join(BIN, 'secret.sh')
NAME = 'test/agent-core-unittest'
BASH = shutil.which('bash')


def run(*args, stdin='', env=None):
    return subprocess.run([S] + list(args), input=stdin, capture_output=True, text=True, env=env, timeout=30)


def real_secret_tool_works():
    """secret-tool is installed and can reach a keyring (a lookup of a missing item exits 1 without an error)."""
    if sys.platform == 'darwin' or not shutil.which('secret-tool'):
        return False
    try:
        out = subprocess.run(['secret-tool', 'lookup', 'service', 'agent/unittest-probe'], capture_output=True,
                             text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 1 and not out.stderr.strip()


class RoundTrip:
    """The same checks for every backend."""

    def test_round_trip_without_echoing_the_value(self):
        r = run('set', NAME, stdin='tok-123_abc\n', env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('tok-123', r.stdout + r.stderr)
        self.assertEqual(run('get', NAME, env=self.env).stdout.strip(), 'tok-123_abc')
        self.assertEqual(run('has', NAME, env=self.env).returncode, 0)
        self.assertIn(NAME, run('list', env=self.env).stdout.split())
        self.assertNotIn('tok-123', run('list', env=self.env).stdout)

    def test_set_again_replaces_the_value(self):
        run('set', NAME, stdin='first', env=self.env)
        self.assertEqual(run('set', NAME, stdin='second', env=self.env).returncode, 0)
        self.assertEqual(run('get', NAME, env=self.env).stdout.strip(), 'second')

    def test_rejects_bad_names_and_empty_values(self):
        self.assertEqual(run('set', 'no-slash', stdin='x', env=self.env).returncode, 2)
        self.assertEqual(run('get', 'Upper/case', env=self.env).returncode, 2)
        self.assertEqual(run('set', NAME, stdin='', env=self.env).returncode, 2)
        self.assertEqual(run('has', NAME, env=self.env).returncode, 1)
        self.assertEqual(run('get', NAME, env=self.env).returncode, 2)

    def test_usage_for_no_or_unknown_command(self):
        self.assertEqual(run(env=self.env).returncode, 0)
        self.assertIn('usage: secret.sh', run(env=self.env).stdout)
        self.assertEqual(run('frobnicate', env=self.env).returncode, 2)


@unittest.skipUnless(sys.platform == 'darwin', 'macOS keychain')
class Keychain(RoundTrip, unittest.TestCase):
    env = None

    def tearDown(self):
        subprocess.run(['security', 'delete-generic-password', '-s', 'agent/' + NAME], capture_output=True)

    def test_rejects_values_the_keychain_command_cannot_carry(self):
        self.assertEqual(run('set', NAME, stdin='a"b').returncode, 2)
        self.assertEqual(run('set', NAME, stdin='a\\b').returncode, 2)
        self.assertEqual(run('has', NAME).returncode, 1)


@unittest.skipUnless(real_secret_tool_works(), 'needs secret-tool with a reachable keyring')
class LinuxSecretStore(RoundTrip, unittest.TestCase):
    env = None

    def tearDown(self):
        subprocess.run(['secret-tool', 'clear', 'service', 'agent/' + NAME], capture_output=True)


FAKE_SECRET_TOOL = r'''#!/usr/bin/env python3
import json, os, sys
store = os.environ['FAKE_SECRETS']
with open(os.path.join(store, 'argv.log'), 'a') as fh:
    fh.write(json.dumps(sys.argv[1:]) + '\n')
cmd, rest = sys.argv[1], sys.argv[2:]
opts, attrs, i = {}, {}, 0
while i < len(rest):
    if rest[i] == '--all':
        i += 1
    elif rest[i].startswith('--label='):
        opts['label'] = rest[i].split('=', 1)[1]
        i += 1
    else:
        attrs[rest[i]] = rest[i + 1]
        i += 2
def items():
    return [json.load(open(os.path.join(store, f))) for f in sorted(os.listdir(store)) if f.endswith('.json')]
def hit(item):
    return all(item['attrs'].get(k) == v for k, v in attrs.items())
if cmd == 'store':
    name = attrs['service'].replace('/', '__') + '.json'
    with open(os.path.join(store, name), 'w') as fh:
        json.dump({'label': opts.get('label', ''), 'secret': sys.stdin.read(), 'attrs': attrs}, fh)
elif cmd == 'lookup':
    for item in items():
        if hit(item):
            sys.stdout.write(item['secret'])
            sys.exit(0)
    sys.exit(1)
elif cmd == 'search':
    found = [x for x in items() if hit(x)]
    for n, item in enumerate(found):
        print('[/fake/%d]' % n)
        print('label = ' + item['label'])
        print('secret = ' + item['secret'])
        for k, v in sorted(item['attrs'].items()):
            print('attribute.%s = %s' % (k, v))
        print()
    sys.exit(0 if found else 1)
else:
    sys.exit(2)
'''


@unittest.skipIf(sys.platform == 'darwin', 'macOS uses the real keychain')
@unittest.skipUnless(BASH, 'needs bash')
class FakeSecretTool(RoundTrip, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='akit-secret-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, 'store')
        fakebin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.store)
        os.makedirs(fakebin)
        tool = os.path.join(fakebin, 'secret-tool')
        with open(tool, 'w') as fh:
            fh.write(FAKE_SECRET_TOOL)
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, PATH=fakebin + os.pathsep + os.environ.get('PATH', ''), FAKE_SECRETS=self.store)

    def argv_log(self):
        with open(os.path.join(self.store, 'argv.log'), encoding='utf-8') as fh:
            return fh.read()

    def test_the_value_never_appears_in_arguments(self):
        run('set', NAME, stdin='tok-123_abc\n', env=self.env)
        run('get', NAME, env=self.env)
        self.assertNotIn('tok-123', self.argv_log())
        self.assertIn('service', self.argv_log())
        self.assertIn('agent/' + NAME, self.argv_log())

    def test_stored_value_has_no_trailing_newline_and_keeps_quotes(self):
        self.assertEqual(run('set', NAME, stdin='a"b\\c\n', env=self.env).returncode, 0)
        with open(os.path.join(self.store, 'agent__' + NAME.replace('/', '__') + '.json'), encoding='utf-8') as fh:
            item = json.load(fh)
        self.assertEqual(item['secret'], 'a"b\\c')
        self.assertEqual(item['attrs']['service'], 'agent/' + NAME)
        self.assertEqual(run('get', NAME, env=self.env).stdout, 'a"b\\c\n')


@unittest.skipUnless(BASH, 'needs bash')
class NoStore(unittest.TestCase):
    """No keychain and no secret-tool: one line saying what to install, exit code 2, nothing stored."""

    def setUp(self):
        self.empty = tempfile.mkdtemp(prefix='akit-nopath-')
        self.addCleanup(shutil.rmtree, self.empty, True)

    def secret(self, *args, stdin=''):
        return subprocess.run([BASH, S] + list(args), input=stdin, capture_output=True, text=True, timeout=30,
                              env={'PATH': self.empty, 'HOME': self.empty})

    def test_every_command_that_needs_a_store_explains_what_is_missing(self):
        for args in (('get', NAME), ('has', NAME), ('set', NAME), ('list',)):
            r = self.secret(*args, stdin='value')
            self.assertEqual(r.returncode, 2, args)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
            self.assertIn('secret-tool', r.stderr)
            self.assertEqual(r.stdout, '')

    def test_bad_names_are_still_reported_first(self):
        r = self.secret('get', 'no-slash')
        self.assertEqual(r.returncode, 2)
        self.assertIn('<provider>/<account>', r.stderr)

    def test_usage_needs_no_store(self):
        r = self.secret()
        self.assertEqual(r.returncode, 0)
        self.assertIn('usage: secret.sh', r.stdout)


if __name__ == '__main__':
    unittest.main()
