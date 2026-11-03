#!/usr/bin/env python3
"""After building the test image, verify isolation with dummy data and a throwaway Git checkout."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'bin'))
import review_worker

CHECK = """import os, sys, unittest
from pathlib import Path

class Isolation(unittest.TestCase):
    def test_machine_and_credentials_are_not_shared(self):
        self.assertNotIn('AGENT_REVIEW_CANARY', os.environ)
        self.assertFalse(Path(sys.argv[-1]).exists())
        self.assertFalse(Path('/var/run/docker.sock').exists())
        self.assertNotEqual(os.getuid(), 0)
        self.assertFalse(any(l.split()[1:2] == ['00000000'] for l in Path('/proc/net/route').read_text().splitlines()))

unittest.main(argv=['check'], exit=True)
"""


def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / 'repo'
        outside = Path(tmp) / 'private-marker'
        outside.write_text('dummy host data')
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        probe = repo / 'check.py'
        probe.write_text(CHECK)
        subprocess.run(['git', 'add', 'check.py'], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'synthetic isolation probe'], cwd=repo, check=True)
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
        probe.write_text('raise RuntimeError("read uncommitted content")')
        os.environ['AGENT_REVIEW_CANARY'] = 'synthetic'
        result = review_worker.execute(str(repo), 'example/probe', ['python3', 'check.py', str(outside)], head)
        print(result.stdout, end='')
        return result.returncode


if __name__ == '__main__':
    sys.exit(main())
