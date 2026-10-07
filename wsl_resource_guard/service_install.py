"""Explicit, recoverable administrator installation of optional web services."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid

from .build_info import write_stamp
from .installer import (FileTransaction, install_lock, owner_call, owner_install_lock, resolve_owner, restore_unit,
                        run, run_as, unit_state, validate_root_path)
from .service_control import Controller, REGISTRY, SHARED

SOURCE = Path(__file__).resolve().parents[1]
DEST = Path('/opt/wsl-resource-guard')
WEB_CONFIG = Path('/etc/wsl-resource-guard/web.json')
UNIT_DIR = Path('/etc/systemd/system')
BACKUP_ROOT = Path('/var/backups/wrg-services')
UNITS = ('wrg-service-control.service', 'wrg-services-restore.service', 'wrg-web.service')


def package_payload(source: Path, include_web=False) -> dict[str, bytes]:
    files = {p.name: p.read_bytes() for p in sorted((source / 'wsl_resource_guard').glob('*.py'))}
    if include_web:
        files.update({f'web/{p.name}': p.read_bytes() for p in (source / 'wsl_resource_guard/web').iterdir() if p.is_file()})
    with tempfile.TemporaryDirectory() as temporary:
        write_stamp(Path(temporary), source)
        files['BUILD.json'] = (Path(temporary) / 'BUILD.json').read_bytes()
    return files


def sync_user_library(source: Path, user_lib: Path, uid: int, gid: int, backup: Path | None = None) -> list[str]:
    """Load source bytes first, then write exclusively as the destination owner UID."""
    owner = pwd.getpwuid(uid)
    if owner.pw_gid != gid:
        raise ValueError('Owner group mismatch')
    payload = package_payload(source)
    backup = backup or Path(owner.pw_dir) / '.local/state/wsl-resource-guard/install-backups' / uuid.uuid4().hex
    def copy():
        tx = FileTransaction(backup)
        from .guard_install import snapshot_state
        snapshot_state(backup)
        try:
            for name, content in payload.items():
                tx.write(user_lib / name, content)
        except BaseException:
            tx.rollback()
            raise
    owner_call(owner, copy)
    return sorted(name for name in payload if name.endswith('.py'))


def prepare_shared_dir(path: Path, owner_gid: int) -> None:
    validate_root_path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, 0, owner_gid)
    path.chmod(0o750)


def disable_legacy_unit(run_func=run) -> None:
    if (UNIT_DIR / 'devbox-wsl-service-recovery.service').exists():
        run_func('systemctl', 'disable', 'devbox-wsl-service-recovery.service')


def preflight_identity(owner: str, origin: str, login: str,
                       registry: Path = REGISTRY, web_config: Path = WEB_CONFIG) -> None:
    if registry.exists():
        existing = json.loads(registry.read_text())
        if existing.get('owner') != owner or existing.get('origin') != origin:
            raise RuntimeError('Existing registry owner/origin differs; explicit migration is required')
    if web_config.exists():
        configured = json.loads(web_config.read_text())
        if configured.get('origin') != origin or configured.get('allowed_logins') != [login]:
            raise RuntimeError('Existing web access configuration differs; explicit migration is required')
        if not isinstance(configured.get('secret_key'), str) or not configured['secret_key']:
            raise RuntimeError('Existing web configuration has no secret_key')


def prepare_venv(dest: Path, requirements: Path, web, environment: Path | None = None) -> Path:
    """Create at its permanent path: venv script shebangs cannot survive a rename."""
    environment = environment or dest / '.venvs' / uuid.uuid4().hex
    environment.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    run('/usr/bin/python3', '-m', 'venv', str(environment))
    python = str(environment / 'bin/python')
    run(python, '-m', 'pip', 'install', '--disable-pip-version-check', '--require-hashes', '-r', str(requirements))
    run(python, '-m', 'pip', 'check')
    run_as(web, python, '-c', 'import flask, gunicorn', cwd='/')
    run_as(web, str(environment / 'bin/gunicorn'), '--version', cwd='/')
    return environment


def tailscale_preflight():
    status = json.loads(run('tailscale', 'status', '--json', capture=True))
    node = status['Self']
    dns = node['DNSName'].rstrip('.')
    if not re.fullmatch(r'[a-zA-Z0-9.-]+\.ts\.net', dns):
        raise RuntimeError('Cannot determine the Tailscale DNS name')
    login = status['User'][str(node['UserID'])]['LoginName']
    if login == 'tagged-devices':
        raise RuntimeError('Tagged devices require an explicitly configured login; automatic installation is unsupported')
    serve = json.loads(run('tailscale', 'serve', 'status', '--json', capture=True))
    handler = serve.get('Web', {}).get(f'{dns}:9443', {}).get('Handlers', {}).get('/')
    own_proxies = {'http://127.0.0.1:8765', 'unix:/run/wrg-web/http.sock', 'http+unix:///run/wrg-web/http.sock'}
    if '9443' in serve.get('TCP', {}) and (handler or {}).get('Proxy') not in own_proxies:
        raise RuntimeError('Tailscale port 9443 is already used by another service')
    if serve.get('AllowFunnel', {}).get(f'{dns}:9443'):
        raise RuntimeError('Disable public Funnel on port 9443 before installation')
    return f'https://{dns}:9443', login, serve, handler


def wait_for_health():
    for _ in range(40):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(.25)
    raise RuntimeError('Dashboard health check failed')


def install(owner):
    # All identity checks precede writes, chmod/chown, account creation and pip.
    paths = [DEST, REGISTRY, SHARED, WEB_CONFIG, BACKUP_ROOT,
             *(UNIT_DIR / name for name in UNITS)]
    for path in paths:
        validate_root_path(path)
    origin, login, serve, old_handler = tailscale_preflight()
    preflight_identity(owner.pw_name, origin, login, REGISTRY, WEB_CONFIG)
    from .guard_install import validate_system_owner
    validate_system_owner(owner)
    if not (UNIT_DIR / 'wrg-web.service').exists():
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 8765))
    with owner_install_lock(owner):
        return _install_preflighted(owner, origin, login, serve, old_handler)


def _install_preflighted(owner, origin, login, serve, old_handler):
    payload = package_payload(SOURCE, include_web=True)
    for name in payload:
        validate_root_path(DEST / 'wsl_resource_guard' / name)
    for name in ('requirements-web.txt', 'packaging/opencode-web.service'):
        validate_root_path(DEST / name)
    validate_root_path(DEST / '.venvs')
    states = {name: unit_state(name) for name in UNITS}
    backup = BACKUP_ROOT / uuid.uuid4().hex
    tx = FileTransaction(backup)
    (backup / 'unit-states.json').write_text(json.dumps(states))
    (backup / 'tailscale-serve.json').write_text(json.dumps(serve))
    if REGISTRY.exists():
        (backup / 'registry.json').write_bytes(REGISTRY.read_bytes())
    shared_before = SHARED.stat() if SHARED.exists() else None
    user_lib = Path(owner.pw_dir) / '.local/lib/wsl-resource-guard/wsl_resource_guard'
    user_backup = Path(owner.pw_dir) / '.local/state/wsl-resource-guard/install-backups' / uuid.uuid4().hex
    user_synced = False
    switched = False
    serve_attempted = False
    try:
        DEST.mkdir(parents=True, exist_ok=True, mode=0o755)
        try:
            web = pwd.getpwnam('wrg-web')
        except KeyError:
            run('useradd', '--system', '--user-group', '--home-dir', '/nonexistent', '--no-create-home', '--shell', '/usr/sbin/nologin', 'wrg-web')
            web = pwd.getpwnam('wrg-web')
        environment = DEST / '.venvs' / uuid.uuid4().hex
        (backup / 'environment-path.txt').write_text(str(environment))
        prepare_venv(DEST, SOURCE / 'requirements-web.txt', web, environment)
        # No running service is changed before environment validation succeeds.
        for name, content in payload.items():
            tx.write(DEST / 'wsl_resource_guard' / name, content)
        tx.write(DEST / 'requirements-web.txt', (SOURCE / 'requirements-web.txt').read_bytes())
        from .guard_install import render_unit
        tx.write(DEST / 'packaging/opencode-web.service', render_unit(SOURCE / 'packaging/opencode-web.service', owner).encode())
        REGISTRY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        prepare_shared_dir(SHARED, owner.pw_gid)
        if not REGISTRY.exists():
            tx.snapshot(REGISTRY)
            Controller().init(owner.pw_name, origin)
        if not WEB_CONFIG.exists():
            tx.write(WEB_CONFIG, json.dumps({'origin': origin, 'allowed_logins': [login], 'secret_key': secrets.token_urlsafe(48)}).encode(), 0o640, web.pw_gid)
        # Prove both installed code and configuration are readable by the web UID.
        run_as(web, str(environment / 'bin/python'), '-c',
               'import wsl_resource_guard.webapp; from pathlib import Path; Path("/etc/wsl-resource-guard/web.json").read_bytes()', cwd=str(DEST))
        for name in UNITS:
            unit = (SOURCE / 'packaging' / name).read_text().replace('@WEB_VENV@', str(environment))
            tx.write(UNIT_DIR / name, unit.encode())
        if user_lib.is_dir():
            sync_user_library(SOURCE, user_lib, owner.pw_uid, owner.pw_gid, user_backup)
            user_synced = True
        switched = True
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', *UNITS)
        run('systemctl', 'restart', 'wrg-service-control.service')
        run('systemctl', 'restart', 'wrg-web.service')
        wait_for_health()
        serve_attempted = True
        run('tailscale', 'serve', '--bg', '--https=9443', 'unix:/run/wrg-web/http.sock')
    except BaseException:
        errors = []
        if serve_attempted:
            try:
                proxy = (old_handler or {}).get('Proxy')
                run('tailscale', 'serve', '--bg', '--https=9443', proxy or 'off')
            except Exception as exc:
                errors.append(str(exc))
        if switched:
            for name in ('wrg-web.service', 'wrg-service-control.service'):
                try:
                    run('systemctl', 'stop', name)
                except Exception as exc:
                    errors.append(str(exc))
            # Remove installer-created enablement links before restoring old units.
            for name, state in states.items():
                if state['enabled'] not in ('enabled', 'enabled-runtime'):
                    try:
                        run('systemctl', 'disable', name)
                    except Exception as exc:
                        errors.append(str(exc))
        try:
            tx.rollback()
        except Exception as exc:
            errors.append(str(exc))
        if shared_before:
            os.chown(SHARED, shared_before.st_uid, shared_before.st_gid)
            SHARED.chmod(shared_before.st_mode & 0o7777)
        if user_synced:
            try:
                owner_call(owner, lambda: FileTransaction.restore(user_backup))
            except Exception as exc:
                errors.append(str(exc))
        if switched:
            for name, state in states.items():
                try:
                    restore_unit(name, state)
                except Exception as exc:
                    errors.append(str(exc))
        print(f'Installation failed; recovery backup retained at {backup}', flush=True)
        if errors:
            print('Some recovery operations failed: ' + '; '.join(errors), flush=True)
        raise
    print(f'Installed. Dashboard: {origin}\nRecovery backup: {backup}')
    if user_synced:
        print(f'Owner recovery backup: {user_backup}')
    print('Existing application services and their autostart settings were preserved.')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Install optional service management and private Tailscale dashboard')
    parser.add_argument('--owner', help='Non-root owner; defaults to invoking user or SUDO_USER')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('Run install-services.sh; it invokes sudo for system installation')
    try:
        owner = resolve_owner(args.owner)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    validate_root_path(Path('/run/wsl-resource-guard-install.lock'))
    with install_lock():
        previous_umask = os.umask(0o022)
        try:
            install(owner)
        finally:
            os.umask(previous_umask)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
