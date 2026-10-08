"""Installation primitives shared by the explicit administrator entrypoints."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import tempfile
import time
import traceback
import uuid


def resolve_owner(name: str | None):
    if name is None:
        name = os.environ.get('SUDO_USER') if os.geteuid() == 0 else pwd.getpwuid(os.getuid()).pw_name
    if not name:
        raise ValueError('Specify --owner for a root invocation without a non-root SUDO_USER')
    owner = pwd.getpwnam(name)
    if owner.pw_uid == 0:
        raise ValueError('The installation owner must be a non-root account')
    if not Path(owner.pw_dir).is_absolute() or any(c in owner.pw_dir for c in '\n\r\x00'):
        raise ValueError('Invalid owner home directory')
    return owner


def run(*args: str, capture=False, timeout=None) -> str:
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None, timeout=timeout)
    return result.stdout or ''


def run_as(owner, *args: str, capture=False, **kwargs) -> str:
    env = dict(os.environ, HOME=owner.pw_dir, USER=owner.pw_name, LOGNAME=owner.pw_name,
               XDG_RUNTIME_DIR=f'/run/user/{owner.pw_uid}',
               DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{owner.pw_uid}/bus')
    identity = dict(user=owner.pw_uid, group=owner.pw_gid, extra_groups=os.getgrouplist(owner.pw_name, owner.pw_gid)) if os.geteuid() == 0 else {}
    result = subprocess.run(args, check=True, text=True, env=env,
                            stdout=subprocess.PIPE if capture else None, **identity, **kwargs)
    return result.stdout or ''


def owner_call(owner, callback, *, timeout=None):
    """Run filesystem work after permanently dropping root; never chown user paths."""
    if os.geteuid() != 0:
        if os.geteuid() != owner.pw_uid:
            raise RuntimeError('Owner UID does not match invoking user')
        if timeout is None:
            return callback()
    pid = os.fork()
    if pid == 0:
        try:
            if os.geteuid() == 0:
                os.setgroups([])
                os.setgid(owner.pw_gid)
                os.setuid(owner.pw_uid)
            os.environ.update(HOME=owner.pw_dir, USER=owner.pw_name, LOGNAME=owner.pw_name)
            callback()
        except BaseException:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    try:
        if timeout is None:
            _, status = os.waitpid(pid, 0)
        else:
            deadline = time.monotonic() + timeout
            while True:
                finished, status = os.waitpid(pid, os.WNOHANG)
                if finished:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Owner installation query timed out')
                time.sleep(min(.01, max(0, deadline - time.monotonic())))
    except BaseException:
        try:
            finished, _ = os.waitpid(pid, os.WNOHANG)
            if not finished:
                # Still our unreaped child, so its PID cannot be reused.
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass
        raise
    if status != 0:
        raise RuntimeError('Owner-UID installation step failed; see preceding error')


def owner_query(owner, callback, *, timeout=10):
    """Return small installation facts without opening owner paths as root.

    Only installer-owned callbacks are accepted; this is not a socket operation.
    The payload is capped below Linux PIPE_BUF so the child can finish before
    owner_call waits for it. Never pass file contents or secrets through here.
    """
    read_fd, write_fd = os.pipe()
    try:
        def collect():
            encoded = json.dumps(callback(), allow_nan=False).encode('utf-8')
            if len(encoded) > 4096:
                raise ValueError('Owner installation facts exceed limit')
            os.write(write_fd, encoded)
        owner_call(owner, collect, timeout=timeout)
        os.close(write_fd)
        write_fd = -1
        return json.loads(os.read(read_fd, 4097))
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def systemd_escape(value: str, *, command: bool = False, quoted: bool = True) -> str:
    """Encode a unit literal using the receiving directive's parser."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Control characters are not allowed in unit values')
    if command and any(c in value for c in '\\"\''):
        raise ValueError('systemd executable paths cannot contain quotes or backslashes')
    if not quoted:
        # Single-path directives do not unquote or C-unescape. Preserve their
        # literal spaces/quotes, but reject suffixes eaten by the unit parser.
        if value.endswith(('\\', ' ')):
            raise ValueError('Ambiguous trailing whitespace or backslash in unit path')
        return value.replace('%', '%%')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    # Generated ExecStart directives use ':' to disable environment expansion.
    return value


def validate_root_path(path: Path, *, trusted_base: Path = Path('/')) -> None:
    """Reject links and writable/untrusted ancestors before privileged filesystem work."""
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError(f'Unsafe installation path: {path}')
    try:
        relative = path.relative_to(trusted_base)
    except ValueError as exc:
        raise RuntimeError(f'Path escapes installation root: {path}') from exc
    current = trusted_base
    for component in ('', *relative.parts):
        if component:
            current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f'Unsafe symlink in installation path: {current}')
        # trusted_base is an explicit test seam; real callers always start at /.
        if trusted_base == Path('/') and (info.st_uid != 0 or info.st_mode & 0o022):
            raise RuntimeError(f'Installation path must be root-owned and not group/world writable: {current}')


@contextmanager
def install_lock(path: Path = Path('/run/wsl-resource-guard-install.lock')):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another installation is in progress') from exc
        yield
    finally:
        os.close(fd)


class FileTransaction:
    """Per-file recovery journal; rollback touches only paths this transaction wrote."""
    def __init__(self, backup: Path):
        self.backup = backup
        backup.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.entries = []
        self._save()

    def _save(self):
        (self.backup / 'manifest.json').write_text(json.dumps(self.entries))

    def snapshot(self, path: Path):
        if any(entry['path'] == str(path) for entry in self.entries):
            return
        entry = {'path': str(path), 'kind': 'absent'}
        if path.is_symlink():
            entry.update(kind='link', target=os.readlink(path))
        elif path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f'Expected a regular file: {path}')
            name = f'{len(self.entries):04d}.bak'
            (self.backup / name).write_bytes(path.read_bytes())
            entry.update(kind='file', backup=name, mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)
        self.entries.append(entry)
        self._save()

    def write(self, path: Path, content: bytes, mode=0o644, gid=None):
        self.snapshot(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.wrg-install-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(content)
                os.fchmod(output.fileno(), mode)
                if gid is not None:
                    os.fchown(output.fileno(), 0, gid)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def symlink(self, path: Path, target: Path):
        self.snapshot(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f'.wrg-install-{uuid.uuid4().hex}'
        try:
            temporary.symlink_to(target)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def rollback(self):
        self.restore(self.backup)

    @staticmethod
    def restore(backup: Path):
        entries = json.loads((backup / 'manifest.json').read_text())
        for entry in reversed(entries):
            path = Path(entry['path'])
            if entry['kind'] == 'absent':
                path.unlink(missing_ok=True)
                continue
            temporary = path.parent / f'.wrg-restore-{uuid.uuid4().hex}'
            try:
                if entry['kind'] == 'link':
                    temporary.symlink_to(entry['target'])
                else:
                    with temporary.open('xb') as output:
                        output.write((backup / entry['backup']).read_bytes())
                        os.fchmod(output.fileno(), entry['mode'])
                        if os.geteuid() == 0:
                            os.fchown(output.fileno(), entry['uid'], entry['gid'])
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


def unit_state(name: str, owner=None) -> dict:
    prefix = ['systemctl'] if owner is None else ['systemctl', '--user']
    runner = run if owner is None else lambda *args, **kw: run_as(owner, *args, **kw)
    def query(verb):
        try:
            return runner(*prefix, verb, name, capture=True).strip()
        except subprocess.CalledProcessError as exc:
            return (exc.stdout or '').strip()
    return {'enabled': query('is-enabled'), 'active': query('is-active') == 'active'}


def restore_unit(name: str, state: dict, owner=None):
    prefix = ['systemctl'] if owner is None else ['systemctl', '--user']
    runner = run if owner is None else lambda *args, **kw: run_as(owner, *args, **kw)
    runner(*prefix, 'daemon-reload')
    # A unit absent before installation has no file after rollback.
    if state['enabled'] == 'enabled-runtime':
        # Installation enables persistently; restoring /run links alone would leave
        # its /etc links behind and incorrectly enable the service after reboot.
        runner(*prefix, 'disable', name)
    if state['enabled'] in ('enabled', 'enabled-runtime'):
        runner(*prefix, 'enable', *(['--runtime'] if state['enabled'] == 'enabled-runtime' else []), name)
    elif state['enabled'] not in ('not-found', ''):
        runner(*prefix, 'disable', name)
    if state['active']:
        runner(*prefix, 'restart', name)
    elif state['enabled'] not in ('not-found', ''):
        runner(*prefix, 'stop', name)


@contextmanager
def owner_install_lock(owner):
    """Keep an owner-UID child holding the user lock during privileged installation."""
    path = Path(owner.pw_dir) / '.local/state/wsl-resource-guard/install.lock'
    if os.geteuid() != 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        with install_lock(path):
            yield
        return
    ready_read, ready_write = os.pipe()
    done_read, done_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.close(done_write)
        try:
            os.setgroups([])
            os.setgid(owner.pw_gid)
            os.setuid(owner.pw_uid)
            path.parent.mkdir(parents=True, exist_ok=True)
            with install_lock(path):
                os.write(ready_write, b'1')
                os.close(ready_write)
                os.read(done_read, 1)
        except BaseException:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    os.close(ready_write)
    os.close(done_read)
    try:
        if os.read(ready_read, 1) != b'1':
            raise RuntimeError('Owner installation lock unavailable; another installation may be in progress')
        yield
    finally:
        os.close(ready_read)
        os.close(done_write)
        os.waitpid(pid, 0)
