"""Generic user/system guard installation with owner-UID writes and recovery."""
from __future__ import annotations

import argparse
import grp
import hashlib
import math
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

from .installer import (FileTransaction, install_lock, owner_call, owner_install_lock, owner_query, resolve_owner, restore_unit,
                        run, run_as, systemd_escape, unit_state, validate_root_path)
from .service_install import package_payload

SOURCE = Path(__file__).resolve().parents[1]
SYSTEM_UNIT = Path('/etc/systemd/system/wsl-resource-guard.service')
UNIT = 'wsl-resource-guard.service'
BACKUP_ROOT = Path('/var/backups/wrg-guard')


def render_unit(template: Path, owner, state_path: Path | None = None) -> str:
    state = state_path or Path(owner.pw_dir) / '.local/state/wsl-resource-guard'
    values = {'@OWNER@': owner.pw_name, '@GROUP@': grp.getgrgid(owner.pw_gid).gr_name,
              '@HOME@': owner.pw_dir, '@HOME_PATH@': owner.pw_dir, '@STATE@': str(state)}
    lines = []
    for line in template.read_text().splitlines():
        command = line.startswith('ExecStart=')
        quoted = line.partition('=')[0] not in ('WorkingDirectory', 'ConditionPathIsDirectory', 'User', 'Group')
        line = re.sub('|'.join(map(re.escape, values)),
                      lambda match: systemd_escape(values[match[0]], command=command, quoted=quoted), line)
        lines.append(line)
    return '\n'.join(lines) + '\n'


def guard_context(home: Path, system: bool) -> dict:
    """Read/validate in the owner's process, before changing installed files."""
    from .config import Settings
    from .safe_read import read_text
    config = home / '.config/wsl-resource-guard/config.toml'
    def fingerprint():
        try:
            return hashlib.sha256(read_text(config, max_bytes=1024 * 1024).encode()).hexdigest()
        except FileNotFoundError:
            if config.is_symlink():
                raise ValueError('Broken configuration symlink')
            return None
    before = fingerprint()
    settings = Settings.load(config)
    if any(warning.startswith('state_dir:') for warning in settings.load_warnings):
        raise ValueError('Invalid configured state_dir')
    raw = settings.state_dir
    state = home if raw == '~' else home / raw[2:] if raw.startswith('~/') else Path(raw)
    if not state.is_absolute():
        if system:
            raise ValueError('System state_dir must be absolute or start with ~/')
        state = home / state
    systemd_escape(str(state))
    systemd_escape(str(home))
    if system:
        if state == home or '..' in state.parts or not state.is_relative_to(home):
            raise ValueError('System state_dir must be below the owner HOME')
        # No symlink components: the unit's write exception must name the same
        # directories the owner prepared. Existing regular files are not dirs.
        current = home
        for part in ('', *state.relative_to(home).parts):
            if part:
                current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('System state_dir requires regular directories without symlinks')
    if fingerprint() != before:
        raise RuntimeError('Configuration changed during installation preflight')
    return {'state_path': str(state), 'config_sha256': before}


def prepare_state_path(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    fd, probe = tempfile.mkstemp(prefix='.install-write-check-', dir=state)
    try:
        os.write(fd, b'fixture')
    finally:
        os.close(fd)
        os.unlink(probe)


def _sample_identity(state: Path) -> dict:
    from .daemon import load_state
    data = load_state(state / 'state.json')
    try:
        mtime = (state / 'state.json').stat().st_mtime
    except FileNotFoundError:
        return {}
    return {**{key: data.get(key) for key in ('writer_pid', 'writer_start_ticks', 'updated_at')},
            'mtime': mtime}


def wait_for_guard(owner, system: bool, state: Path, started_at: float, *, timeout=180):
    from .processes import process_start_ticks
    deadline = time.monotonic() + timeout
    runner = run if system else lambda *args, **kwargs: run_as(owner, *args, **kwargs)
    prefix = ('systemctl',) if system else ('systemctl', '--user')
    while True:
        try:
            budget = lambda: max(.01, min(10, deadline - time.monotonic()))
            pid = int(runner(*prefix, 'show', UNIT, '--property=MainPID', '--value', capture=True,
                             timeout=budget()).strip())
            sample = owner_query(owner, lambda: _sample_identity(state), timeout=budget())
            token = process_start_ticks(pid) if pid > 0 else None
            updated = sample.get('updated_at')
            if (pid > 0 and token is not None and sample.get('writer_pid') == pid
                    and sample.get('writer_start_ticks') == token
                    and type(updated) in (int, float) and math.isfinite(updated)
                    and updated >= started_at and sample.get('mtime', 0) >= started_at):
                return
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Guard did not write its first sample after startup')
        time.sleep(min(.5, max(0, deadline - time.monotonic())))


def ensure_no_system_guard(path: Path = SYSTEM_UNIT):
    if path.exists() or path.is_symlink():
        raise RuntimeError('A system guard is already installed; use install-root.sh to upgrade it')
    result = subprocess.run(['systemctl', 'show', UNIT, '--property=LoadState', '--value'],
                            capture_output=True, text=True, check=False)
    if result.returncode == 0 and result.stdout.strip() not in ('', 'not-found'):
        raise RuntimeError('A system guard is already installed; use install-root.sh to upgrade it')


def validate_system_owner(owner):
    if SYSTEM_UNIT.exists():
        users = [line.partition('=')[2].strip().strip('"') for line in SYSTEM_UNIT.read_text().splitlines() if line.startswith('User=')]
        if users != [owner.pw_name]:
            raise RuntimeError('Existing system guard owner differs; explicit migration is required')
    registry = Path('/var/lib/wrg-services/registry.json')
    validate_root_path(registry)
    if registry.exists() and json.loads(registry.read_text()).get('owner') != owner.pw_name:
        raise RuntimeError('Existing registry owner differs; explicit migration is required')


# These are daemon-owned outputs, not arbitrary files in the user's state directory.
DAEMON_STATE_FILES = ('state.json', 'config-request-result.json', 'weekly-report.json', 'push-expired.json')
HISTORY_NAME = re.compile(r'(?:disk-)?history-\d{4}-\d{2}-\d{2}\.jsonl')


def _daemon_state_names(state: Path) -> set[str]:
    names = set(DAEMON_STATE_FILES)
    if state.exists():
        names.update(path.name for path in state.iterdir() if HISTORY_NAME.fullmatch(path.name))
    return names


def snapshot_state(backup: Path, state: Path | None = None):
    """Record daemon output presence and bytes as the owner for later recovery."""
    from .config import load_daemon_settings
    state = state if state is not None else load_daemon_settings().state_path
    destination = backup / 'state'
    destination.mkdir(mode=0o700)
    (backup / 'state-path.txt').write_text(str(state))
    files = {}
    for name in sorted(_daemon_state_names(state)):
        path = state / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            files[name] = {'present': False}
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f'Expected a regular daemon state file: {path}')
        shutil.copyfile(path, destination / name)
        files[name] = {'present': True, 'mode': stat.S_IMODE(info.st_mode)}
    (backup / 'state-manifest.json').write_text(json.dumps({'version': 1, 'files': files}))


def restore_state(backup: Path):
    """Preserve failed-run output before restoring prior bytes and prior absence."""
    state = Path((backup / 'state-path.txt').read_text())
    saved = json.loads((backup / 'state-manifest.json').read_text())['files']
    if any(name not in DAEMON_STATE_FILES and not HISTORY_NAME.fullmatch(name) for name in saved):
        raise RuntimeError('Unexpected path in daemon state recovery manifest')
    names = sorted(set(saved) | _daemon_state_names(state))
    tx = FileTransaction(backup / ('state-restore-' + uuid.uuid4().hex))
    # Journal every current output before the first overwrite or removal. These
    # backups retain new samples, including a new day's JSONL and appended rows.
    for name in names:
        tx.snapshot(state / name)
    for name in names:
        entry = saved.get(name, {'present': False})
        if entry['present']:
            tx.write(state / name, (backup / 'state' / name).read_bytes(), entry['mode'])
        else:
            (state / name).unlink(missing_ok=True)


def install(owner, system: bool):
    # Administrator entrypoints take the root lock first, then this shared
    # owner lock. User installers take only the owner lock and never invert
    # that order. Recheck identity/mode after acquiring it, before any apply.
    with owner_install_lock(owner):
        if system:
            validate_root_path(SYSTEM_UNIT)
            validate_system_owner(owner)
        else:
            ensure_no_system_guard()
        return _install_preflighted(owner, system)


def _install_preflighted(owner, system: bool):
    home = Path(owner.pw_dir)
    context = owner_query(owner, lambda: guard_context(home, system))
    state_path = Path(context['state_path'])
    install_dir = home / '.local/lib/wsl-resource-guard'
    unit_path = SYSTEM_UNIT if system else home / '.config/systemd/user' / UNIT
    user_backup = home / '.local/state/wsl-resource-guard/install-backups' / uuid.uuid4().hex
    system_backup = BACKUP_ROOT / uuid.uuid4().hex
    # Read and compile the complete payload before touching a running guard.
    payload = package_payload(SOURCE)
    for name, content in payload.items():
        if name.endswith('.py'):
            compile(content, name, 'exec')
    launcher = (SOURCE / 'bin/wrg').read_bytes()
    config = (SOURCE / 'config/config.toml').read_bytes()
    opencode = render_unit(SOURCE / 'packaging/opencode-web.service', owner).encode()
    unit = render_unit(SOURCE / ('packaging/wsl-resource-guard.service' if system else 'packaging/wsl-resource-guard.user.service'), owner, state_path).encode()
    user_state = unit_state(UNIT, owner)
    system_state = unit_state(UNIT) if system else None
    if system:
        validate_root_path(system_backup.parent)
        system_tx = FileTransaction(system_backup)
        (system_backup / 'unit-states.json').write_text(json.dumps({'system': system_state, 'user': user_state, 'user_backup': str(user_backup)}))
    else:
        system_tx = None
    user_prepared = False
    user_stopped = False
    user_changed = False
    system_stopped = False
    switched = False
    def prepare_owner():
        prepare_state_path(state_path)
        tx = FileTransaction(user_backup)
        (user_backup / 'unit-states.json').write_text(json.dumps({'user': user_state}))
    def apply_owner():
        # Use the existing journal prepared before service transition.
        tx = object.__new__(FileTransaction)
        tx.backup = user_backup
        tx.entries = json.loads((user_backup / 'manifest.json').read_text())
        try:
            for name, content in payload.items():
                tx.write(install_dir / 'wsl_resource_guard' / name, content)
            tx.write(install_dir / 'bin/wrg', launcher, 0o755)
            tx.write(install_dir / 'packaging/opencode-web.service', opencode)
            tx.symlink(home / '.local/bin/wrg', install_dir / 'bin/wrg')
            config_path = home / '.config/wsl-resource-guard/config.toml'
            if not config_path.exists():
                tx.write(config_path, config)
            if not system:
                tx.write(unit_path, unit)
        except BaseException:
            tx.rollback()
            raise
    try:
        owner_call(owner, prepare_owner)
        user_prepared = True
        if owner_query(owner, lambda: guard_context(home, system)) != context:
            raise RuntimeError('Configuration changed during installation preparation')
        # Stop only a guard that is actually running; missing user managers are fine.
        if user_state['active']:
            user_changed = True
            user_stopped = True
            run_as(owner, 'systemctl', '--user', 'stop', UNIT)
        if system and system_state['active']:
            system_stopped = True
            run('systemctl', 'stop', UNIT)
        owner_call(owner, lambda: snapshot_state(user_backup, state_path))
        owner_call(owner, apply_owner)
        if system:
            system_tx.write(unit_path, unit)
            switched = True
            if user_state['enabled'] in ('enabled', 'enabled-runtime'):
                user_changed = True
                run_as(owner, 'systemctl', '--user', 'disable', UNIT)
            switched = True
            run('systemctl', 'daemon-reload')
            run('systemctl', 'enable', UNIT)
            started_at = time.time()
            run('systemctl', 'restart', UNIT)
            run('systemctl', 'is-active', '--quiet', UNIT)
        else:
            switched = True
            run_as(owner, 'systemctl', '--user', 'daemon-reload')
            run_as(owner, 'systemctl', '--user', 'enable', UNIT)
            started_at = time.time()
            run_as(owner, 'systemctl', '--user', 'restart', UNIT)
            run_as(owner, 'systemctl', '--user', 'is-active', '--quiet', UNIT)
        wait_for_guard(owner, system, state_path, started_at)
    except BaseException:
        errors = []
        if switched:
            try:
                if system:
                    run('systemctl', 'stop', UNIT)
                    if system_state['enabled'] not in ('enabled', 'enabled-runtime'):
                        run('systemctl', 'disable', UNIT)
                else:
                    run_as(owner, 'systemctl', '--user', 'stop', UNIT)
                    if user_state['enabled'] not in ('enabled', 'enabled-runtime'):
                        run_as(owner, 'systemctl', '--user', 'disable', UNIT)
            except Exception as exc:
                errors.append(str(exc))
        if system_tx:
            try:
                system_tx.rollback()
            except Exception as exc:
                errors.append(str(exc))
        if user_prepared:
            try:
                owner_call(owner, lambda: FileTransaction.restore(user_backup))
                if switched:
                    owner_call(owner, lambda: restore_state(user_backup))
            except Exception as exc:
                errors.append(str(exc))
        for state, state_owner, changed in [(system_state, None, system and (switched or system_stopped)),
                                            (user_state, owner, user_changed or (switched and not system))]:
            if changed:
                try:
                    restore_unit(UNIT, state, state_owner)
                except Exception as exc:
                    errors.append(str(exc))
        print(f'Installation failed; recovery backup: {user_backup}', flush=True)
        if errors:
            print('Some recovery operations failed: ' + '; '.join(errors), flush=True)
        raise
    print(f'Installed {"system" if system else "user"} guard for {owner.pw_name}.')
    print(f'Owner recovery backup: {user_backup}')
    if system:
        print(f'System recovery backup: {system_backup}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Install the resource guard for a non-root owner')
    parser.add_argument('--owner')
    parser.add_argument('--system', action='store_true')
    args = parser.parse_args(argv)
    try:
        owner = resolve_owner(args.owner)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    if args.system and os.geteuid() != 0:
        parser.error('Use install-root.sh for system installation')
    if not args.system and os.geteuid() == 0:
        parser.error('Run install-user.sh directly as the non-root owner')
    if args.system:
        validate_root_path(Path('/run/wsl-resource-guard-install.lock'))
        with install_lock():
            install(owner, True)
    else:
        install(owner, False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
