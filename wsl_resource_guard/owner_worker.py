"""Fixed read operations executed after dropping all root credentials.

This module is launched from the controller's trusted installation, never from
an owner's CLI copy. Inputs name operations and bounded values, never paths.
"""
from __future__ import annotations
from dataclasses import asdict
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
import time

from .safe_read import read_text

MAX_OUTPUT = 16 * 1024 * 1024
TIMEOUT = 20
LOG_TAILS = {'opencode-web.service': [('[OpenCode stdout]', '.local/state/opencode-web.log')]}


def weekly_status(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    if isinstance(value.get('slot'), str):
        result['slot'] = value['slot'][:100]
    if isinstance(value.get('sent_at'), (int, float)) and not isinstance(value.get('sent_at'), bool):
        result['sent_at'] = value['sent_at']
    if isinstance(value.get('results'), dict):
        result['results'] = {channel: detail[:2000] for channel, detail in value['results'].items()
                             if channel in ('gmail', 'discord', 'webhook') and isinstance(detail, str)}
    return result


class OwnerData:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else Path.home()
        self._history_lock = threading.Lock()
        self._history_cache = {}
        self._alerts_cache = None
        self._alerts_at = 0
        self._disk_lock = threading.Lock()
        self._disk_history_at = 0
        self._disk_history_cache = []

    def context(self):
        from .daemon import load_state
        from .notifications import Notifier
        settings = self._owner_settings()
        values = asdict(settings)
        values['config_path'] = str(settings.config_path)
        values['secrets_path'] = str(settings.secrets_path)
        return {'settings': values, 'state': load_state(settings.state_path / 'state.json'),
                'channels': Notifier(settings).channel_status()}

    def _owner_home(self) -> Path:
        return self.home

    def _owner_settings(self):
        from .config import load_daemon_settings
        settings = load_daemon_settings(self.home / '.config/wsl-resource-guard/config.toml')
        def absolute(raw):
            path = Path(raw)
            if raw == '~':
                return str(self.home)
            if raw.startswith('~/'):
                path = self.home / raw[2:]
            elif not path.is_absolute():
                path = self.home / path
            return str(path.absolute())
        settings.state_dir = absolute(settings.state_dir)
        settings.project_roots = [absolute(root) for root in settings.project_roots]
        if settings.wsl_vhd_path:
            settings.wsl_vhd_path = absolute(settings.wsl_vhd_path)
        settings.secrets_path = self.home / '.config/wsl-resource-guard/secrets.json'
        return settings

    def _state_path(self) -> Path:
        return self._owner_settings().state_path

    def history(self, range_key: str = '3h') -> list:
        from .history import HISTORY_RANGES, read_history, read_history_downsampled
        if range_key not in HISTORY_RANGES:
            range_key = '3h'
        seconds, bucket = HISTORY_RANGES[range_key]
        with self._history_lock:
            cached = self._history_cache.get(range_key)
            # An empty history is a valid answer and is cached too.
            if cached is not None and time.monotonic() - cached[0] < 60:
                return cached[1]
        # Reading and parsing JSONL happens outside the lock (O5); only cache
        # access is synchronized. Each range keeps its own entry, so clients
        # viewing different ranges do not evict each other.
        since = time.time() - seconds
        if bucket:
            result = read_history_downsampled(self._state_path(), since, bucket)
        else:
            result = read_history(self._state_path(), since)[-1800:]
        with self._history_lock:
            self._history_cache[range_key] = (time.monotonic(), result)
        return result

    def attribution(self, range_key: str = '3h') -> dict:
        from .history import HISTORY_RANGES, memory_attribution
        if range_key not in HISTORY_RANGES:
            range_key = '3h'
        seconds, bucket = HISTORY_RANGES[range_key]
        key = 'attribution:' + range_key
        with self._history_lock:
            cached = self._history_cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < 60:
                return cached[1]
        result = memory_attribution(self._state_path(), time.time() - seconds, bucket)
        with self._history_lock:
            self._history_cache[key] = (time.monotonic(), result)
        return result

    def session_history(self, days: int = 7) -> list:
        from .history import session_history
        with self._history_lock:
            cached = self._history_cache.get('sessions')
            if cached is not None and time.monotonic() - cached[0] < 120:
                return cached[1]
        result = session_history(self._state_path(), time.time() - days * 86400)[:300]
        with self._history_lock:
            self._history_cache['sessions'] = (time.monotonic(), result)
        return result

    def alert_episodes(self, gap_seconds: int = 1800) -> dict:
        """Episodes plus a 7-day per-cause rollup for the alerts view (C4)."""
        with self._history_lock:
            if self._alerts_cache is not None and time.monotonic() - self._alerts_at < 300:
                return self._alerts_cache
        from .history import alert_episodes, read_history, reason_summary_7d
        settings = self._owner_settings()
        since = time.time() - settings.retention_days * 86400
        records = read_history(settings.state_path, since,
                               fields=('timestamp', 'severity', 'reasons'))
        episodes = alert_episodes(records, gap_seconds)
        result = {'episodes': episodes,
                  'reasons_7d': reason_summary_7d(episodes)}
        with self._history_lock:
            self._alerts_cache = result
            self._alerts_at = time.monotonic()
        return result

    def disks(self, force: bool = False) -> dict:
        from .daemon import load_state
        from .disks import (daily_disk_records, disk_insights, read_disks,
                            disk_severity, recent_disk_records)
        settings = self._owner_settings()
        state_path = settings.state_path
        state = load_state(state_path / 'state.json')
        previous = (state.get('metrics') or {}).get('disks', [])
        rows = read_disks(settings.disk_drives, settings.wsl_vhd_path, previous,
                          interval=settings.disk_refresh_seconds, force=force)
        for row in rows:
            row['severity'] = (disk_severity(row, settings.warning_disk_free_percent, settings.critical_disk_free_percent)
                               if row['kind'] == 'windows' else 'not-monitored')
        with self._disk_lock:
            now = time.monotonic()
            if not self._disk_history_at or now - self._disk_history_at >= (5 if force else 60):
                history = daily_disk_records(state_path, settings.retention_days)
                self._disk_history_cache, self._disk_history_at = history, now
            recent = recent_disk_records(state_path) + [
                {'timestamp': time.time(), 'disks': rows}]
            insights = disk_insights(rows, self._disk_history_cache,
                                     settings.warning_disk_free_percent,
                                     settings.critical_disk_free_percent,
                                     recent=recent)
            for row in rows:
                if row['id'] in insights:
                    row['insight'] = insights[row['id']]
            return {'disks': rows, 'history': self._disk_history_cache, 'updated_at': time.time(),
                    'sample_interval_seconds': settings.disk_refresh_seconds,
                    'thresholds': {'warning_free_percent': settings.warning_disk_free_percent,
                                   'critical_free_percent': settings.critical_disk_free_percent}}

    def settings_info(self) -> dict:
        """Read-only configuration and diagnostics for the dashboard (N4).

        Never include secret values — only enabled/configured flags.
        """
        from .build_info import WEB_PACKAGE, compare_stamps, read_stamp
        from .config import CONFIG_GROUPS, CONFIG_RULES, Settings
        from .daemon import load_state
        from .notifications import Notifier
        home = self._owner_home()
        settings = self._owner_settings()
        state_path = settings.state_path
        state = load_state(state_path / 'state.json')
        cli_stamp = read_stamp(home / '.local/lib/wsl-resource-guard/wsl_resource_guard')
        web_stamp = read_stamp(WEB_PACKAGE)
        level, detail = compare_stamps(cli_stamp, web_stamp, WEB_PACKAGE.is_dir())
        numeric = ('int', 'float')
        defaults = Settings()
        group_of = {key: title for title, keys in CONFIG_GROUPS for key in keys}
        from .config import WEB_EDITABLE_KINDS
        rules = [
            {'key': key, 'value': getattr(settings, key, None), 'kind': kind,
             'editable': kind in WEB_EDITABLE_KINDS,
             # Ranges apply to numeric rules only; a 0 minimum is meaningful.
             'min': low if kind in numeric else None,
             'max': high if kind in numeric else None,
             'description': description,
             # Group title orders the panels; default marks changed values.
             'group': group_of.get(key, '기타'),
             'default': getattr(defaults, key, None)}
            for key, (kind, low, high, description) in CONFIG_RULES.items()
        ]
        return {
            'config_path': str(settings.config_path),
            'load_warnings': list(settings.load_warnings),
            'groups': [title for title, _ in CONFIG_GROUPS],
            'rules': rules,
            'disk_drives': list(settings.disk_drives),
            'wsl_vhd_path': settings.wsl_vhd_path,
            'project_roots': list(settings.project_roots),
            'channels': Notifier(settings).channel_status(),
            'channel_errors': (state.get('last_channel_errors')
                               if isinstance(state.get('last_channel_errors'), dict) else {}),
            'config_request': {},  # Root controller merges the trusted request status.
            'weekly_report': weekly_status(load_state(state_path / 'weekly-report.json')),
            'code_copies': {'level': level, 'detail': detail,
                            'cli_commit': (cli_stamp or {}).get('commit'),
                            'web_commit': (web_stamp or {}).get('commit'),
                            'cli_dirty': bool((cli_stamp or {}).get('dirty')),
                            'web_dirty': bool((web_stamp or {}).get('dirty'))},
        }


def dispatch(operation: str, args: dict, reader=None):
    allowed = {'context', 'history', 'attribution', 'session-history', 'alerts',
               'disks', 'settings', 'config-result', 'gone-endpoints', 'logs'}
    if operation not in allowed or not isinstance(args, dict):
        raise ValueError('Unsupported owner read operation')
    reader = reader or OwnerData()
    if operation == 'context':
        return reader.context()
    if operation in ('history', 'attribution'):
        from .history import HISTORY_RANGES
        key = args.get('range', '3h')
        if key not in HISTORY_RANGES:
            raise ValueError('Invalid history range')
        return getattr(reader, operation)(key)
    if operation == 'session-history':
        return reader.session_history()
    if operation == 'alerts':
        return reader.alert_episodes()
    if operation == 'disks':
        return reader.disks(force=args.get('force') is True)
    if operation == 'settings':
        return reader.settings_info()
    if operation == 'config-result':
        from .daemon import load_state
        return load_state(reader._state_path() / 'config-request-result.json')
    if operation == 'gone-endpoints':
        from .webpush import gone_endpoints
        return sorted(gone_endpoints(reader._state_path()))
    target, lines = args.get('target'), args.get('lines', 80)
    if target not in LOG_TAILS or type(lines) is not int or not 1 <= lines <= 500:
        raise ValueError('Invalid log target')
    chunks = []
    for label, relative in LOG_TAILS[target]:
        try:
            text = read_text(reader.home / relative, max_bytes=64000, errors='replace', tail=True)
        except OSError:
            text = ''
        chunks.append(label + '\n' + '\n'.join(text.splitlines()[-lines:]))
    return '\n\n'.join(chunks)


def _trusted(path: Path):
    """Root may execute only code and interpreter below root-owned directories."""
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError('Owner worker installation is not root-owned and immutable')


def call_owner(owner, operation: str, args: dict | None = None):
    """Spawn with credentials dropped *before* Python starts or files are opened."""
    root = Path(__file__).resolve().parents[1]
    executable = Path('/usr/bin/python3').resolve()
    credentials = {}
    if os.geteuid() == 0:
        if owner.pw_uid == 0:
            raise PermissionError('Resource Guard owner must be unprivileged')
        _trusted(root)
        package = Path(__file__).resolve().parent
        _trusted(package)
        for module in package.rglob('*'):
            _trusted(module.resolve())
        _trusted(executable)
        credentials = {'user': owner.pw_uid, 'group': owner.pw_gid, 'extra_groups': ()}
    elif os.geteuid() != owner.pw_uid:
        raise PermissionError('Owner credentials unavailable')
    script = 'import sys;sys.path.insert(0, ' + repr(str(root)) + ');from wsl_resource_guard.owner_worker import main;main()'
    payload = json.dumps({'op': operation, 'args': args or {}}).encode()
    if len(payload) > 4096:
        raise ValueError('Owner read request exceeds limit')
    with subprocess.Popen([str(executable), '-I', '-c', script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd='/', env={'HOME': owner.pw_dir, 'USER': owner.pw_name, 'LOGNAME': owner.pw_name,
                          'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
            shell=False, close_fds=True, **credentials) as child:
        try:
            child.stdin.write(payload)
            child.stdin.close()
            data = bytearray()
            deadline = time.monotonic() + TIMEOUT
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError('Owner read timed out')
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_OUTPUT:
                        raise ValueError('Owner read output exceeds limit')
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
            if child.returncode:
                raise OSError('Owner read failed')
            return json.loads(data)
        except BaseException:
            child.kill()
            child.wait()
            raise


def main():
    import resource
    import sys
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    request = json.loads(sys.stdin.buffer.read(4097))
    result = dispatch(request['op'], request.get('args', {}))
    output = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
    if len(output) > MAX_OUTPUT:
        raise ValueError('Owner read output exceeds limit')
    sys.stdout.buffer.write(output)
