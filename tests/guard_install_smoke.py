"""Real systemd lifecycle test, exclusively on a disposable GitHub-hosted VM.

Never run this on a workstation, WSL, or a self-hosted runner. It installs only
Resource Guard under a newly created fixture account, with all delivery disabled.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from wsl_resource_guard import guard_install
from wsl_resource_guard.installer import install_lock, owner_install_lock, run_as

ACCOUNT = 'wrglifecycle'
HOME = Path('/home/fixture guard $USER %n')
UNIT = guard_install.UNIT
CONFIG = '''interval_seconds = 5
history_interval_seconds = 10
disk_drives = []
windows_toast_enabled = false
gmail_enabled = false
discord_enabled = false
webhook_enabled = false
push_enabled = false
email_heartbeat_enabled = false
weekly_report_enabled = false
'''


def command(*args, check=True):
    result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=210)
    if check and result.returncode:
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        result.check_returncode()
    return result


def user(owner, *args, check=True):
    try:
        return run_as(owner, *args, capture=True, timeout=210, cwd=REPO)
    except subprocess.CalledProcessError:
        if check:
            raise
        return None


def write_config(owner, state=None):
    path = HOME / '.config/wsl-resource-guard/config.toml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG + ('state_dir = ' + json.dumps(str(state)) + '\n' if state else ''))
    for target in (path, path.parent, path.parent.parent):
        os.chown(target, owner.pw_uid, owner.pw_gid)


def pid(owner, system):
    args = ('systemctl', *(() if system else ('--user',)), 'show', UNIT, '--property=MainPID', '--value')
    return int(command(*args).stdout.strip() if system else user(owner, *args).strip())


def collect_twice(owner, system, state):
    deadline = time.monotonic() + 45
    previous = None
    while time.monotonic() < deadline:
        try:
            data = json.loads((state / 'state.json').read_text())
            current = data['updated_at']
            if data['writer_pid'] == pid(owner, system):
                if previous is not None and current > previous:
                    return
                previous = current
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.5)
    raise AssertionError('Guard did not produce two new samples in its configured state directory')


@contextmanager
def injected_collection_failure():
    original_payload = guard_install.package_payload
    original_wait = guard_install.wait_for_guard
    def payload(*args, **kwargs):
        files = original_payload(*args, **kwargs)
        old = b'    state_path = settings.state_path / "state.json"'
        assert files['daemon.py'].count(old) == 1
        files['daemon.py'] = files['daemon.py'].replace(old, b'    raise RuntimeError("fixture collection failure")\n' + old)
        return files
    def wait(*args, **kwargs):
        return original_wait(*args, **kwargs, timeout=3)
    with patch.object(guard_install, 'package_payload', side_effect=payload), \
         patch.object(guard_install, 'wait_for_guard', side_effect=wait):
        yield


def expect_failed_upgrade(owner):
    with install_lock(), injected_collection_failure():
        try:
            guard_install.install(owner, True)
        except RuntimeError as exc:
            assert 'first sample' in str(exc), exc
        else:
            raise AssertionError('Active but non-collecting guard was accepted')


def main():
    if (os.geteuid() != 0 or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or 'microsoft' in Path('/proc/version').read_text().lower()
            or Path('/proc/1/comm').read_text().strip() != 'systemd'):
        raise SystemExit('This test requires a disposable GitHub-hosted systemd VM, never local WSL')
    try:
        pwd.getpwnam(ACCOUNT)
    except KeyError:
        pass
    else:
        raise SystemExit('Fixture account already exists; refusing to alter it')
    for path in (HOME, guard_install.SYSTEM_UNIT, guard_install.BACKUP_ROOT,
                 Path('/run/wsl-resource-guard-install.lock')):
        if path.exists() or path.is_symlink():
            raise SystemExit('Fixture installation path already exists; refusing to alter it')
    # Source is public test code. Allow only traversal through checkout parents,
    # restoring modes at the end; no extra checkout or project copy is created.
    modes = {p: p.stat().st_mode & 0o777 for p in REPO.parents if p != Path('/')}
    owner = None
    try:
        for path, mode in modes.items():
            path.chmod(mode | 0o001)
        command('useradd', '--create-home', '--home-dir', str(HOME), '--shell', '/bin/bash', ACCOUNT)
        owner = pwd.getpwnam(ACCOUNT)
        command('loginctl', 'enable-linger', ACCOUNT)
        command('systemctl', 'start', f'user@{owner.pw_uid}.service')
        write_config(owner)
        state = HOME / '.local/state/wsl-resource-guard'
        user(owner, '/bin/bash', str(REPO / 'install-user.sh'))
        collect_twice(owner, False, state)

        # A failed user -> system transition must restart/re-enable the user unit.
        expect_failed_upgrade(owner)
        assert not guard_install.SYSTEM_UNIT.exists()
        assert user(owner, 'systemctl', '--user', 'is-enabled', UNIT).strip() == 'enabled'
        collect_twice(owner, False, state)

        command('/bin/bash', str(REPO / 'install-root.sh'), '--owner', ACCOUNT)
        collect_twice(owner, True, state)
        assert user(owner, 'systemctl', '--user', 'is-enabled', UNIT, check=False) is None
        assert user(owner, 'systemctl', '--user', 'is-active', UNIT, check=False) is None
        before = pid(owner, True)
        assert user(owner, '/bin/bash', str(REPO / 'install-user.sh'), check=False) is None
        assert pid(owner, True) == before

        # Unsafe paths and an already-held installation lock fail before restart.
        write_config(owner, '/etc/wrg-forbidden-fixture')
        result = command('/bin/bash', str(REPO / 'install-root.sh'), '--owner', ACCOUNT, check=False)
        assert result.returncode != 0 and pid(owner, True) == before
        assert not Path('/etc/wrg-forbidden-fixture').exists()
        write_config(owner)
        with owner_install_lock(owner):
            result = command('/bin/bash', str(REPO / 'install-root.sh'), '--owner', ACCOUNT, check=False)
            assert result.returncode != 0 and pid(owner, True) == before

        # The systemd mount namespace must allow the configured custom directory.
        custom = HOME / 'custom state "literal"'
        write_config(owner, custom)
        command('/bin/bash', str(REPO / 'install-root.sh'), '--owner', ACCOUNT)
        collect_twice(owner, True, custom)
        assert 'ReadWritePaths="' in guard_install.SYSTEM_UNIT.read_text()
        expect_failed_upgrade(owner)
        collect_twice(owner, True, custom)
        assert command('systemctl', 'is-enabled', UNIT).stdout.strip() == 'enabled'
        print('Real user/system installation, conflict rejection, custom state and rollback passed')
    finally:
        if owner is not None:
            command('systemctl', 'disable', '--now', UNIT, check=False)
            user(owner, 'systemctl', '--user', 'disable', '--now', UNIT, check=False)
            guard_install.SYSTEM_UNIT.unlink(missing_ok=True)
            command('systemctl', 'daemon-reload')
            command('loginctl', 'disable-linger', ACCOUNT, check=False)
            command('systemctl', 'stop', f'user@{owner.pw_uid}.service', check=False)
            command('userdel', '--remove', ACCOUNT)
            if HOME.exists():
                shutil.rmtree(HOME)
            if guard_install.BACKUP_ROOT.exists():
                shutil.rmtree(guard_install.BACKUP_ROOT)
            Path('/run/wsl-resource-guard-install.lock').unlink(missing_ok=True)
        for path, mode in modes.items():
            path.chmod(mode)


if __name__ == '__main__':
    main()
