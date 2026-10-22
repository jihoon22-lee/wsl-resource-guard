"""Packaged Paseo unit lifecycle with disposable processes, never the real daemon."""
from pathlib import Path
import os
import pwd
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wsl_resource_guard.guard_install import render_unit


def ctl(*args, check=True):
    return subprocess.run(['systemctl', '--user', *args], check=check,
                          capture_output=True, text=True, timeout=20)


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('disposable service did not settle')


def main():
    name = f'wrg-paseo-test-{uuid.uuid4().hex}.service'
    link = Path.home() / '.config/systemd/user' / name
    template = Path(__file__).resolve().parents[1] / 'packaging/examples/paseo-daemon.service.in'
    with tempfile.TemporaryDirectory(prefix='wrg-paseo-') as directory:
        home = Path(directory)
        (home / '.paseo').mkdir()
        binary = home / '.local/bin/paseo'
        binary.parent.mkdir(parents=True)
        binary.write_text('''#!/usr/bin/python3
import os, subprocess, signal
from pathlib import Path
root = Path(os.environ['HOME'])
child = subprocess.Popen(['/usr/bin/sleep', '600'], start_new_session=True)
with (root / 'starts').open('a') as out:
    out.write(f'{os.getpid()} {child.pid}\\n')
signal.pause()
''')
        binary.chmod(0o755)
        owner = pwd.getpwuid(os.getuid())
        unit = render_unit(template, owner).replace(str(Path.home()), str(home))
        unit = '\n'.join(line for line in unit.splitlines() if not line.startswith(('User=', 'Group=')))
        unit = unit.replace('WantedBy=multi-user.target', 'WantedBy=default.target')
        unit = unit.replace('RestartSec=5', 'RestartSec=0.1')

        def starts():
            return [tuple(map(int, line.split())) for line in (home / 'starts').read_text().splitlines()] if (home / 'starts').exists() else []

        def gone(pid):
            stat = Path(f'/proc/{pid}/stat')
            return not stat.exists() or stat.read_text().split(')')[1].split()[0] == 'Z'

        try:
            # A persistent unit is a real file; disable removes linked aliases.
            link.write_text(unit + '\n')
            ctl('daemon-reload')
            ctl('enable', name)
            assert ctl('is-active', name, check=False).stdout.strip() == 'inactive'
            ctl('start', name)
            wait_for(lambda: len(starts()) == 1)
            first = starts()[0]
            ctl('disable', name)
            assert ctl('is-active', name).stdout.strip() == 'active'
            ctl('kill', '--kill-whom=main', '--signal=SIGKILL', name)
            wait_for(lambda: len(starts()) == 2)
            wait_for(lambda: all(gone(pid) for pid in first))
            assert int(ctl('show', name, '-p', 'NRestarts', '--value').stdout) >= 1
            second = starts()[1]
            ctl('restart', name)
            wait_for(lambda: len(starts()) == 3)
            wait_for(lambda: all(gone(pid) for pid in second))
            ctl('stop', name)
            wait_for(lambda: all(gone(pid) for pid in starts()[-1]))
            time.sleep(.3)
            assert len(starts()) == 3
            assert ctl('is-active', name, check=False).stdout.strip() == 'inactive'
            print('PASS: enable/disable independent of running; crash recovery; restart/stop clean descendants; manual stop stays stopped')
        finally:
            ctl('disable', '--now', name, check=False)
            ctl('reset-failed', name, check=False)
            link.unlink(missing_ok=True)
            ctl('daemon-reload')
    assert not link.exists()


if __name__ == '__main__':
    main()
