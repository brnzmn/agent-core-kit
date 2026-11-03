"""Install/config regressions, using only temporary paths and the kit's neutral test configuration."""
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import CORE, HOOKS, TEST_CONFIG, bash, make_local  # noqa: E402

sys.path.insert(0, HOOKS)
import common  # noqa: E402

spec = importlib.util.spec_from_file_location('review_install_build', Path(CORE) / 'bin' / 'build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ScriptPaths(unittest.TestCase):
    def test_generated_commands_keep_the_script_path_literal(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('kit;false;#', 'kit&false', "kit with spaces and 'quotes'", 'ordinary-kit'):
                root = Path(folder) / name
                script = root / 'bin' / 'probe.py'
                script.parent.mkdir(parents=True)
                script.write_text('print("expected script")\n')
                with self.subTest(name=name), mock.patch.object(common, 'CORE', str(root)):
                    out = subprocess.run(common.kit_cmd('bin/probe.py', interpreter=sys.executable), shell=True,
                                         text=True, capture_output=True, timeout=10)
                    self.assertEqual(out.returncode, 0, out.stderr)
                    self.assertEqual(out.stdout, 'expected script\n')


class PrivateReplacements(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.target = self.folder / 'config.toml'
        self.target.write_bytes(b'original fixture\n')
        self.target.chmod(0o600)

    def test_configuration_is_private_before_final_permissions_are_applied(self):
        modes = []
        chmod = os.chmod

        def inspect(path, mode, *args, **kwargs):
            modes.append(stat.S_IMODE(os.stat(path).st_mode))
            self.assertEqual(Path(path).read_bytes(), b'replacement fixture\n')
            self.assertEqual(modes[-1], 0o600)
            return chmod(path, mode, *args, **kwargs)

        previous_umask = os.umask(0o022)
        try:
            with mock.patch.object(build.os, 'chmod', side_effect=inspect):
                build.write_bytes(str(self.target), b'replacement fixture\n', mode=0o600)
        finally:
            os.umask(previous_umask)
        self.assertEqual(modes, [0o600])
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_failed_permissions_preserve_the_original_and_remove_the_temporary_copy(self):
        with mock.patch.object(build.os, 'chmod', side_effect=PermissionError('test permissions failure')):
            with self.assertRaises(PermissionError):
                build.write_bytes(str(self.target), b'replacement fixture\n', mode=0o600)
        self.assertEqual(self.target.read_bytes(), b'original fixture\n')
        self.assertEqual(list(self.folder.iterdir()), [self.target])

    def test_an_existing_predictable_temporary_symlink_is_left_alone(self):
        victim = self.folder / 'untouched.txt'
        victim.write_text('keep me\n')
        (self.folder / 'config.toml.agent-core.tmp').symlink_to(victim)
        build.write_bytes(str(self.target), b'replacement fixture\n', mode=0o600)
        self.assertEqual(victim.read_text(), 'keep me\n')
        self.assertEqual(self.target.read_bytes(), b'replacement fixture\n')

    def test_a_symlinked_destination_keeps_its_link_and_requested_permissions(self):
        link = self.folder / 'settings.json'
        link.symlink_to(self.target)
        build.write_bytes(str(link), b'replacement fixture\n', mode=0o640)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.target.read_bytes(), b'replacement fixture\n')
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)


class ProtectedListEntries(unittest.TestCase):
    def test_an_invalid_sibling_cannot_disable_valid_protected_paths(self):
        for invalid in (None, 42, {}, [], '', '   ', False):
            local = make_local(dict(TEST_CONFIG, protected_dirs=['lake-data', invalid, '~/vault-data']))
            self.addCleanup(shutil.rmtree, local, True)
            for command in ('touch lake-data/raw', 'touch ~/vault-data/raw'):
                with self.subTest(invalid=invalid, command=command):
                    self.assertEqual(bash(command, local=local)[0], 'deny')
            self.assertIsNone(bash('cat lake-data/manifest.json', local=local)[0])

    def test_an_explicit_empty_list_preserves_the_kits_blank_policy(self):
        local = make_local(dict(TEST_CONFIG, protected_dirs=[]))
        self.addCleanup(shutil.rmtree, local, True)
        self.assertIsNone(bash('touch lake-data/raw', local=local)[0])
        self.assertEqual(common.DEFAULTS['protected_dirs'], [])


if __name__ == '__main__':
    unittest.main()
