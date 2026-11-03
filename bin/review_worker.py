"""Run PR-controlled tests in a disposable, offline Docker worker. The parent alone can publish reviews."""
import os
import selectors
import time
from pathlib import Path
import subprocess
import tempfile
import uuid

IMAGE = 'agent-core-review-tests:1'
BOOT = '''set -eu
mkdir -p "$HOME" /work/repo
git -C /work/repo init -q
git -C /work/repo -c safe.directory=/snapshot fetch -q --depth=1 file:///snapshot "$1"
git -C /work/repo checkout -q -b reviewed-head FETCH_HEAD
cd /work/repo
shift
exec "$@"
'''


def worker_args(client, snapshot, head, name, slug, command):
    args = client + ['run', '--rm', '--pull=never', '--name', name, '--network=none', '--read-only',
                     '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=1000:1000',
                     '--cpus=2', '--memory=2g', '--pids-limit=256', '--log-driver=none',
                     '--tmpfs', '/work:rw,exec,nosuid,nodev,uid=1000,gid=1000,mode=700,size=1g',
                     '--tmpfs', '/tmp:rw,exec,nosuid,nodev,size=512m',
                     '--tmpfs', '/var/tmp:rw,exec,nosuid,nodev,size=128m',
                     '--tmpfs', '/home/node:rw,nosuid,nodev,uid=1000,gid=1000,mode=700,size=128m',
                     '--env', 'HOME=/home/node',
                     '--mount', 'type=bind,source=' + str(snapshot) + ',target=/snapshot,readonly']
    return args + ['--workdir', '/work', '--entrypoint', '/bin/sh', IMAGE, '-c', BOOT, 'worker', head] + command


def execute(root, slug, command):
    """Only a shallow bare snapshot is shared. Never pass the host checkout, Docker socket, CLI config or env."""
    def trusted(*argv, **kw):
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              cwd=root, timeout=120, **kw)
        if proc.returncode:
            raise ValueError('%s failed: %s' % (argv[0], proc.stderr.strip()[-500:]))
        return proc.stdout.strip()

    head = trusted('git', 'rev-parse', '--verify', 'HEAD^{commit}')
    endpoint = trusted('docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}')
    if not endpoint.startswith('unix:///'):
        raise ValueError('a local Docker engine is required for isolated review tests')
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp).resolve()
        config, snapshot = scratch / 'client', scratch / 'objects.git'
        config.mkdir()
        trusted('git', 'init', '-q', '--bare', '--template=', str(snapshot))
        trusted('git', '-C', str(snapshot), 'fetch', '-q', '--no-tags', '--depth=1',
                Path(root).as_uri(), head)
        # An empty client config also excludes proxy passwords Docker can otherwise inject into containers.
        client = ['docker', '--host', endpoint, '--config', str(config)]
        name = 'agent-core-tests-' + uuid.uuid4().hex
        args = worker_args(client, snapshot, head, name, slug, command)
        deadline = time.monotonic() + 1800
        output = bytearray()
        with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              cwd=scratch) as proc:
            try:
                with selectors.DefaultSelector() as pending:
                    pending.register(proc.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not pending.select(remaining):
                            raise ValueError('isolated review tests timed out')
                        block = os.read(proc.stdout.fileno(), 65536)
                        if not block:
                            break
                        output.extend(block)
                        if len(output) > 4 * 1024 * 1024:
                            raise ValueError('isolated review test output exceeded 4 MiB')
                code = proc.wait(timeout=max(0, deadline - time.monotonic()))
            except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
                proc.kill()
                # Only the private container created by this execution; --rm removes it when it exits.
                subprocess.run(client + ['container', 'kill', name], capture_output=True, timeout=30)
                raise
        return subprocess.CompletedProcess(command, code, output.decode(errors='replace'), '')
