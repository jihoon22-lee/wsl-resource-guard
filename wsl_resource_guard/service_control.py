"""Root-owned service controller. All callers use the same constrained operations.

No command text, filesystem path, environment, or unit contents are accepted
from clients. Registration adopts a currently discovered unit/Compose project.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request

from .owner_worker import LOG_TAILS, MAX_LOG_LINES

REGISTRY = Path('/var/lib/wrg-services/registry.json')
# Root-written, owner-group-readable hand-off to the guard daemon (which runs
# as the owner and cannot read the 0700 registry directory): alert snooze,
# web config change requests, push subscriptions. Never holds app secrets.
SHARED = Path('/var/lib/wrg-shared')
SNOOZE_CHOICES = (0, 60, 240, 480, 1440)
SOCKET = '/run/wrg-services/control.sock'
ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ('enable', 'disable', 'autostart-on', 'autostart-off', 'start', 'stop', 'restart', 'remove')
FOUNDATION = {
    'docker.service': ('Docker', 'Docker 앱 전체의 실행에 영향을 줍니다.'),
    'ssh.service': ('SSH', 'SSH 접속과 연결된 소켓에 영향을 줍니다.'),
    'tailscaled.service': ('Tailscale', '외부 관리 화면과 Tailscale 연결에 영향을 줍니다.'),
    'wsl-resource-guard.service': ('Resource Guard', '자원 감시와 경보 알림이 중단될 수 있습니다.'),
}
# Optional integration; all other application labels come from discovery/registry.
APP_PORTS = {'opencode-web': 4096}
ACTIVATION_SOCKETS = {'ssh.service': 'ssh.socket', 'docker.service': 'docker.socket'}
# Boot recovery can start from two units at once; the later one waits instead of failing.
RESTORE_LOCK_WAIT_SECONDS = 120
EXCLUDED = ('wrg-', 'devbox-wsl-service-recovery', 'systemd-', 'user@', 'getty@', 'serial-getty@', 'dbus', 'init', 'shutdown', 'reboot', 'halt', 'poweroff')


class ControlError(Exception):
    def __init__(self, message: str, *, code: str = ''):
        super().__init__(message)
        self.code = code


def _memory_limit(raw: str) -> int | None:
    """systemd MemoryHigh/MemoryMax in bytes; 'infinity' or junk means no limit."""
    return int(raw) if raw.isdigit() and int(raw) < 2**63 else None


# A registered URL counts as up for any HTTP answer below 500: login pages and
# 401/403 still prove the app is serving. Timeouts and 5xx count as down.
HEALTH_TIMEOUT_SECONDS = 4


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect answer already proves the app serves; never follow it to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


_probe_opener = urllib.request.build_opener(_NoRedirect).open


def probe_url(url: str, opener=_probe_opener) -> dict:
    started = time.monotonic()
    result = {'checked_at': time.time()}
    try:
        with opener(urllib.request.Request(url, method='GET',
                                           headers={'User-Agent': 'wrg-health/1'}),
                    timeout=HEALTH_TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, 'reason', exc)
        result.update(ok=False, status=None, error=f'{type(reason).__name__}: {reason}'[:200])
        return result
    result.update(ok=status < 500, status=status,
                  latency_ms=round((time.monotonic() - started) * 1000))
    if status >= 500:
        result['error'] = f'HTTP {status}'
    return result


def _service_url(origin: str, port: int | None) -> str:
    """Dashboard URL for a known app port, derived from the site origin.

    rsplit on ':' mis-parses an origin that has no port; split the URL properly.
    """
    if not port:
        return ''
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
    except ValueError:
        return ''
    if not host:
        return ''
    if ':' in host:  # IPv6 literal
        host = f'[{host}]'
    return f'{parsed.scheme or "https"}://{host}:{port}'


def unit_detail(state: dict) -> str:
    # An active oneshot's launcher exited; that does not mean its app stopped.
    # Describe systemd activation, without claiming continuous app health.
    if (state.get('ActiveState') == 'active' and state.get('SubState') == 'exited'
            and state.get('Type') == 'oneshot' and state.get('RemainAfterExit') == 'yes'):
        return '시작 완료 · 활성 상태 유지'
    return state.get('SubState', '')


def command(*args: str, timeout: int = 70, check: bool = True) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/root'})
    if check and result.returncode:
        raise ControlError((result.stderr.strip() or result.stdout.strip() or f'{args[0]} failed')[-3000:])
    return result.stdout.strip()


def atomic_json(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rotate_lines(path: Path, max_bytes: int, keep_lines: int) -> None:
    try:
        if path.stat().st_size <= max_bytes:
            return
    except OSError:
        return
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write('\n'.join(tail_lines(path, keep_lines)) + '\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tail_lines(path: Path, count: int, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    try:
        with path.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            offset = max(0, size - max_bytes)
            handle.seek(offset)
            data = handle.read(max_bytes)
        lines = data.decode('utf-8', errors='replace').splitlines()
        if offset and lines:
            lines.pop(0)
        return lines[-count:]
    except FileNotFoundError:
        return []


class Controller:
    def __init__(self, registry: Path = REGISTRY, run=command, shared: Path = SHARED):
        self.registry = registry
        self.run = run
        self.shared = shared
        self._owner_lock = threading.Lock()
        self._owner_cache = None
        self._owner_at = 0.0
        self._monitor_lock = threading.Lock()
        self._monitor_cache: dict = {}
        self._monitor_at = 0.0
        # History/alerts caches use their own lock so reading days of JSONL
        # never queues behind (or stalls) a monitor snapshot.
        self._history_lock = threading.Lock()
        self._history_cache: dict = {}
        self._alerts_cache: dict | None = None
        self._alerts_at = 0.0
        self._disk_history_cache: list = []
        self._disk_history_at = 0.0
        self._disk_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._snapshot_cache: dict | None = None
        self._snapshot_at = 0.0
        # Per-service HTTP health from the background sampler, keyed by id.
        self._health: dict[str, dict] = {}
        self._summary_rows: list | None = None
        self._summary_rows_at = 0.0
        self._docker_df_cache: dict | None = None
        self._docker_df_at = 0.0

    def write_shared(self, name: str, value: object) -> None:
        """0640 root:<owner group> file the guard daemon can read."""
        self.shared.mkdir(parents=True, exist_ok=True)
        owner = pwd.getpwnam(self.load()['owner'])
        fd, temporary = tempfile.mkstemp(prefix='.shared-', dir=self.shared)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(value, handle, ensure_ascii=False)
            os.chmod(temporary, 0o640)
            if os.geteuid() == 0:
                os.chown(temporary, 0, owner.pw_gid)
            os.replace(temporary, self.shared / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def read_shared(self, name: str) -> dict:
        try:
            data = json.loads((self.shared / name).read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def snooze(self, minutes: object, actor: str = '') -> dict:
        """Pause alert *reminders* for a while; transitions still notify."""
        if isinstance(minutes, bool) or minutes not in SNOOZE_CHOICES:
            raise ControlError('일시 정지 시간을 다시 선택하세요.')
        until = time.time() + minutes * 60 if minutes else 0
        self.write_shared('alert-snooze.json', {'until': until, 'actor': actor[:160],
                                                'created_at': time.time()})
        self._monitor_cache = {}
        if not minutes:
            return {'message': '재알림 일시 정지를 해제했습니다.', 'until': 0}
        label = f'{minutes // 60}시간' if minutes >= 60 else f'{minutes}분'
        return {'message': f'{label} 동안 재알림을 보내지 않습니다. 새 경보와 상태 변화는 계속 알립니다.',
                'until': until}

    def request_config_change(self, key: object, value: object, actor: str = '') -> dict:
        """Queue a numeric setting change for the guard daemon to apply."""
        # Serialize the entire check + write across controller instances. Only
        # a matching terminal result releases the slot, even after expiry or
        # a guard restart. The daemon is the sole writer of that result.
        with self.mutation(wait=5):
            pending = self.config_request_status()
            if pending.get('state') == 'pending':
                raise ControlError(f'{pending["key"]} = {pending["value"]} 적용 대기 중입니다. '
                                   '처리 결과를 확인한 뒤 다시 변경하세요.', code='config_pending')
            return self._request_config_change_locked(key, value, actor)

    def _request_config_change_locked(self, key: object, value: object, actor: str) -> dict:
        from .config import (CONFIG_RULES, WEB_EDITABLE_KINDS, check_relations,
                             coerce_config_value)
        if not isinstance(key, str) or key not in CONFIG_RULES \
                or CONFIG_RULES[key][0] not in WEB_EDITABLE_KINDS:
            raise ControlError('웹에서는 범위가 정해진 숫자 설정만 바꿀 수 있습니다.')
        try:
            typed = coerce_config_value(key, value)
            settings = self._owner_settings()
            check_relations(settings, key, typed)
        except ValueError as exc:
            raise ControlError(f'{key}: {exc}') from None
        request = {'id': os.urandom(8).hex(), 'key': key, 'value': typed,
                   'actor': actor[:160], 'requested_at': time.time(),
                   'interval_seconds': settings.interval_seconds}
        self.write_shared('config-request.json', request)
        return {'message': f'{key} = {typed!r} 변경을 요청했습니다. '
                           f'Guard의 다음 측정에서 적용합니다 (수집 주기 {settings.interval_seconds}초).',
                'id': request['id'], 'interval_seconds': settings.interval_seconds}

    def _read_config_request(self) -> dict:
        from .safe_read import read_text
        try:
            request = json.loads(read_text(self.shared / 'config-request.json', max_bytes=16384))
            if (not isinstance(request, dict) or not isinstance(request.get('id'), str)
                    or not request['id'] or len(request['id']) > 64
                    or not isinstance(request.get('key'), str)
                    or type(request.get('value')) not in (int, float)
                    or not math.isfinite(request['value'])
                    or type(request.get('requested_at')) not in (int, float)
                    or not math.isfinite(request['requested_at'])):
                raise ValueError('Malformed config request')
            return request
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, OverflowError):
            raise ControlError('설정 요청 파일을 확인할 수 없습니다. Guard와 서비스 관리자 상태를 확인하세요.',
                               code='config_unavailable') from None

    def config_request_status(self, identifier: object = None) -> dict:
        if identifier is not None and (not isinstance(identifier, str)
                                      or re.fullmatch(r'[0-9a-f]{16}', identifier) is None):
            raise ControlError('설정 요청 ID가 올바르지 않습니다.')
        request = self._read_config_request()
        if not request and identifier is None:
            return {}
        try:
            result = self._owner_read('config-result')
            if not isinstance(result, dict) or (result and (
                    not isinstance(result.get('id'), str) or type(result.get('ok')) is not bool)):
                raise ValueError('Malformed config result')
            context = self._owner_context()
        except (ControlError, ValueError, OSError):
            raise ControlError('설정 처리 결과를 읽지 못했습니다. 적용 여부를 다시 확인하세요.',
                               code='config_unavailable') from None
        target = request.get('id') if identifier is None else identifier
        done = result if result.get('id') == target else None
        if request.get('id') != target:
            if done is None:
                raise ControlError('이 요청의 결과는 더 이상 보관되어 있지 않습니다. 현재 설정값을 확인하세요.',
                                   code='config_not_found')
            request = done  # Keep the previous outcome while the next slot is pending.
        return {'id': target, 'key': request.get('key'), 'value': request.get('value'),
                'requested_at': request.get('requested_at'),
                'interval_seconds': context['settings']['interval_seconds'],
                'daemon_updated_at': context['state'].get('updated_at'),
                'state': 'pending' if done is None else ('applied' if done['ok'] else 'failed'),
                'message': (done or {}).get('message', '')}

    def _push_subscriptions(self) -> list:
        from . import webpush
        subs = webpush.load_json(self.shared / webpush.SUBSCRIPTIONS_FILE, [])
        gone = self._owner_read('gone-endpoints')
        return [s for s in subs if isinstance(s, dict) and s.get('endpoint') not in gone]

    def push_status(self, subscription: object) -> dict:
        """Check this device's exact subscription; never expose other endpoints."""
        from . import webpush
        try:
            sub = webpush.validate_subscription(subscription)
        except ValueError as exc:
            raise ControlError(str(exc)) from None
        with self.mutation(wait=5):
            expired = sub['endpoint'] in self._owner_read('gone-endpoints')
            saved = webpush.load_json(self.shared / webpush.SUBSCRIPTIONS_FILE, [])
            registered = not expired and any(isinstance(row, dict)
                and row.get('endpoint') == sub['endpoint'] and row.get('keys') == sub['keys']
                for row in saved)
            return {'registered': registered, 'expired': expired}

    def push_key(self) -> dict:
        with self.mutation(wait=5):
            return self._push_key_locked()

    def _push_key_locked(self) -> dict:
        """VAPID public key for browser subscription; generated on first use."""
        from . import webpush
        ready, reason = webpush.available()
        if not ready:
            return {'available': False, 'reason': reason, 'subscriptions': 0}
        vapid = self.read_shared(webpush.VAPID_FILE)
        if not vapid.get('public_key'):
            vapid = webpush.generate_vapid()
            vapid['subject'] = self.load().get('origin', '') or 'https://localhost'
            self.write_shared(webpush.VAPID_FILE, vapid)
        return {'available': True, 'public_key': vapid['public_key'],
                'subscriptions': len(self._push_subscriptions())}

    def push_subscribe(self, subscription: object, label: object = '') -> dict:
        with self.mutation(wait=5):
            return self._push_subscribe_locked(subscription, label)

    def _push_subscribe_locked(self, subscription: object, label: object) -> dict:
        from . import webpush
        if not self._push_key_locked()['available']:
            raise ControlError('이 PC에서는 푸시 알림을 사용할 수 없습니다.')
        try:
            sub = webpush.validate_subscription(subscription)
        except ValueError as exc:
            raise ControlError(str(exc)) from None
        sub['label'] = str(label or '')[:80]
        sub['created_at'] = time.time()
        subs = [s for s in self._push_subscriptions() if s.get('endpoint') != sub['endpoint']]
        subs = (subs + [sub])[-webpush.MAX_SUBSCRIPTIONS:]
        self.write_shared(webpush.SUBSCRIPTIONS_FILE, subs)
        return {'message': f'이 기기의 푸시 등록을 완료했습니다. 등록된 기기 {len(subs)}대.'}

    def push_unsubscribe(self, endpoint: object) -> dict:
        from . import webpush
        if not webpush.endpoint_allowed(endpoint):
            raise ControlError('해제할 브라우저 푸시 구독이 올바르지 않습니다.')
        with self.mutation(wait=5):
            return self._push_unsubscribe_locked(endpoint)

    def _push_unsubscribe_locked(self, endpoint: str) -> dict:
        from . import webpush
        subs = [s for s in self._push_subscriptions() if s.get('endpoint') != endpoint]
        self.write_shared(webpush.SUBSCRIPTIONS_FILE, subs)
        return {'message': f'이 기기의 푸시 알림을 해제했습니다. 남은 기기 {len(subs)}대.'}

    def push_test(self) -> dict:
        from . import webpush
        if not self._push_subscriptions():
            raise ControlError('등록된 기기가 없습니다. 먼저 이 기기에서 푸시 알림을 켜세요.')
        sent, attempted, detail = webpush.send_all(
            self.shared, None, None,
            {'title': 'Resource Guard 테스트 알림', 'body': '이 기기로 경보 푸시가 도착합니다.',
             'severity': 'test', 'tag': 'wrg-test', 'url': '/#settings'})
        if not sent:
            raise ControlError(f'푸시를 보내지 못했습니다: {detail}')
        return {'message': f'테스트 푸시를 {sent}/{attempted}대에 보냈습니다.'}

    def snooze_until(self) -> float:
        until = self.read_shared('alert-snooze.json').get('until')
        return until if isinstance(until, (int, float)) and until > time.time() else 0

    def _invalidate_snapshot(self) -> None:
        with self._snapshot_lock:
            self._snapshot_cache = None
            self._snapshot_at = 0.0

    def load(self) -> dict:
        try:
            return json.loads(self.registry.read_text())
        except FileNotFoundError:
            raise ControlError('서비스 관리가 설치되지 않았습니다. install-services.sh를 실행하세요.') from None

    @contextmanager
    def mutation(self, wait: float = 0.0):
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        with (self.registry.parent / 'operation.lock').open('a') as handle:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ControlError('다른 작업이 진행 중입니다. 완료 후 다시 시도하세요.') from None
                    time.sleep(0.2)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                self._invalidate_snapshot()

    def unit_state(self, target: str) -> dict:
        fields = ('Id,LoadState,ActiveState,SubState,Type,RemainAfterExit,UnitFileState,MemoryCurrent,'
                  'MemoryHigh,MemoryMax,Result,TriggeredBy,FragmentPath')
        output = self.run('systemctl', 'show', target, '--property=' + fields)
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def containers(self, target: str | None = None) -> list[dict]:
        if self.unit_state('docker.service').get('ActiveState') != 'active':
            raise ControlError('Docker가 중지되어 있습니다.')
        label = 'com.docker.compose.project' + (f'={target}' if target else '')
        ids = self.run('docker', 'ps', '-aq', '--filter', 'label=' + label).split()
        if not ids:
            return []
        fmt = '\t'.join('{{json ' + field + '}}' for field in
                        ('.Id', '.Name', '.State', '.HostConfig.RestartPolicy', '.Config.Labels'))
        output = self.run('docker', 'inspect', '--format', fmt, *ids)
        result = []
        for line in output.splitlines():
            identifier, name, state, policy, labels = map(json.loads, line.split('\t'))
            if not re.fullmatch('[a-f0-9]{64}', identifier):
                raise ControlError('잘못된 Docker 컨테이너 ID입니다.')
            if str(labels.get('com.docker.compose.oneoff', '')).lower() == 'true':
                continue
            result.append({'id': identifier, 'name': name.lstrip('/'), 'state': state,
                           'policy': policy.get('Name', 'no'), 'labels': labels})
        return result

    def discover(self) -> list[dict]:
        candidates = []
        output = self.run('systemctl', 'list-unit-files', '--type=service', '--no-legend', '--no-pager')
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            target, state = parts[:2]
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:-]*\.service', target)
                    or target.startswith(EXCLUDED) or '@.' in target or state in ('alias', 'masked', 'masked-runtime')):
                continue
            name, notice = FOUNDATION.get(target, (target.removesuffix('.service'), ''))
            candidates.append({'key': 'systemd:' + target, 'id': target.removesuffix('.service'),
                               'kind': 'systemd', 'target': target,
                               'name': 'OpenCode Web' if target == 'opencode-web.service' else name,
                               'category': 'foundation' if target in FOUNDATION else 'app', 'notice': notice,
                               'owned': target == 'opencode-web.service', 'autostart': state.startswith('enabled')})
        if not any(c['target'] == 'opencode-web.service' for c in candidates):
            # A fixed, root-owned template is the only service content installable by the API.
            candidates.append({'key': 'systemd:opencode-web.service', 'id': 'opencode-web',
                               'kind': 'systemd', 'target': 'opencode-web.service', 'name': 'OpenCode Web',
                               'category': 'app', 'notice': '', 'owned': True, 'autostart': False})
        try:
            containers = self.containers()
        except ControlError:
            containers = []  # systemd discovery still works when Docker is stopped.
        projects: dict[str, list] = {}
        for container in containers:
            project = container['labels'].get('com.docker.compose.project', '')
            if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', project):
                projects.setdefault(project, []).append(container)
        for project, group in sorted(projects.items()):
            candidates.append({'key': 'compose:' + project, 'id': 'compose-' + project,
                               'kind': 'compose', 'target': project, 'name': project,
                               'category': 'app', 'notice': '', 'owned': False,
                               'autostart': any(c['policy'] != 'no' for c in group),
                               'directory': group[0]['labels'].get('com.docker.compose.project.working_dir', '')})
        return candidates

    def init(self, owner: str, origin: str) -> None:
        with self.mutation():
            if self.registry.exists():
                return
            wanted = {'systemd:opencode-web.service', *(f'systemd:{u}' for u in FOUNDATION)}
            candidates = [c for c in self.discover() if c['key'] in wanted
                          and (not c['owned'] or self.unit_state(c['target']).get('LoadState') == 'loaded')]
            for candidate in candidates:
                port = APP_PORTS.get(candidate['target'].removesuffix('.service'))
                candidate['url'] = _service_url(origin, port)
            atomic_json(self.registry, {'version': 1, 'owner': owner, 'origin': origin, 'services': candidates})

    def snapshot(self) -> dict:
        # Each row costs a `systemctl show`; reuse a fresh-enough answer (O2).
        with self._snapshot_lock:
            if (self._snapshot_cache is not None
                    and time.monotonic() - self._snapshot_at < 5):
                return self._snapshot_cache
            result = self._collect_snapshot()
            self._snapshot_cache = result
            self._snapshot_at = time.monotonic()
            return result

    def _collect_snapshot(self) -> dict:
        data = self.load()
        docker_error = ''
        try:
            containers = self.containers() if any(s['kind'] == 'compose' for s in data['services']) else []
        except (ControlError, subprocess.TimeoutExpired):
            containers = []
            docker_error = 'Docker 상태를 조회하지 못했습니다. 서비스 제어 로그를 확인하세요.'
        rows = []
        for entry in data['services']:
            row = dict(entry)
            row.update(memory_bytes=None, error='', drift=False)
            try:
                if entry['kind'] == 'systemd':
                    state = self.unit_state(entry['target'])
                    row['state'] = state.get('ActiveState', 'unknown')
                    row['systemd_substate'] = state.get('SubState', '')
                    row['detail'] = unit_detail(state)
                    row['autostart'] = state.get('UnitFileState', '').startswith('enabled')
                    row['autostart_label'] = state.get('UnitFileState') or 'not-found'
                    row['registered'] = state.get('LoadState') != 'not-found'
                    memory = state.get('MemoryCurrent', '')
                    row['memory_bytes'] = int(memory) if memory.isdigit() and int(memory) < 2**63 else None
                    row['memory_high'] = _memory_limit(state.get('MemoryHigh', ''))
                    row['memory_max'] = _memory_limit(state.get('MemoryMax', ''))
                    row['triggers'] = state.get('TriggeredBy', '').split()
                    if state.get('Result', 'success') != 'success':
                        row['error'] = state['Result']
                else:
                    group = [c for c in containers if c['labels'].get('com.docker.compose.project') == entry['target']]
                    running = sum(c['state']['Status'] == 'running' for c in group)
                    # Docker keeps the last health result on exited containers;
                    # only a running container can be unhealthy now.
                    unhealthy = any(c['state']['Status'] == 'running'
                                    and c['state'].get('Health', {}).get('Status') == 'unhealthy'
                                    for c in group)
                    row['state'] = ('unknown' if docker_error else 'missing' if not group else
                                    'degraded' if unhealthy or 0 < running < len(group) else
                                    'active' if running == len(group) else 'inactive')
                    row['detail'] = f'{running}/{len(group)} 컨테이너 실행'
                    row['registered'] = True
                    row['containers'] = [{'name': c['name'], 'state': c['state']['Status'],
                                          'health': c['state'].get('Health', {}).get('Status', ''),
                                          'restart_policy': c['policy']} for c in group]
                    row['drift'] = any((c['policy'] != 'no') != bool(entry['autostart']) for c in group)
                    row['error'] = docker_error or ('Docker 재시작 정책이 설정과 다릅니다. 자동 실행 설정을 다시 적용하세요.' if row['drift'] else '')
                    memory = 0
                    found = False
                    for c in group:
                        for path in (Path('/sys/fs/cgroup/system.slice') / f"docker-{c['id']}.scope/memory.current",
                                     Path('/sys/fs/cgroup/docker') / c['id'] / 'memory.current'):
                            try:
                                memory += int(path.read_text())
                                found = True
                                break
                            except (OSError, ValueError):
                                pass
                    row['memory_bytes'] = memory if found else None
                    row['autostart_label'] = 'enabled' if entry['autostart'] else 'disabled'
            except (ControlError, subprocess.TimeoutExpired):
                row.update(state='unknown', error='서비스 상태를 조회하지 못했습니다. 서비스 제어 로그를 확인하세요.',
                           detail='상태 조회 실패')
            health = self._health.get(entry['id'])
            if health and health.get('url') == entry.get('url'):
                row['health'] = {k: v for k, v in health.items() if k != 'url'}
            rows.append(row)
        return {'services': rows, 'origin': data['origin'], 'updated_at': time.time()}

    def register(self, key: str, name: str | None = None, url: str | None = None) -> dict:
        if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                                 or any(ord(ch) < 32 for ch in name)):
            raise ControlError('표시 이름은 1~80자여야 합니다.')
        if url is not None:
            if not isinstance(url, str) or len(url) > 2048 or any(ch.isspace() or ord(ch) < 32 for ch in url):
                raise ControlError('접속 URL이 올바르지 않습니다.')
            try:
                parsed = urlsplit(url)
            except ValueError as exc:
                raise ControlError('접속 URL이 올바르지 않습니다.') from exc
            if url and (parsed.scheme not in ('https', 'http') or not parsed.hostname
                        or parsed.username or parsed.password):
                raise ControlError('접속 URL은 인증정보 없는 HTTP(S) 주소여야 합니다.')
            try:
                parsed.port
            except ValueError as exc:
                raise ControlError('접속 URL 포트가 올바르지 않습니다.') from exc
        with self.mutation():
            data = self.load()
            if any(s['key'] == key for s in data['services']):
                raise ControlError('이미 등록된 서비스입니다.')
            entry = next((c for c in self.discover() if c['key'] == key), None)
            if entry is None:
                raise ControlError('현재 발견된 서비스만 등록할 수 있습니다. 목록을 새로고침하세요.')
            if any(s['id'] == entry['id'] for s in data['services']):
                raise ControlError('같은 ID의 다른 서비스가 이미 등록되어 있습니다.')
            if entry['owned']:
                target = Path('/etc/systemd/system/opencode-web.service')
                if target.is_symlink():
                    raise ControlError('OpenCode unit의 심볼릭 링크를 먼저 확인하세요.')
                if not target.exists():
                    shutil.copyfile(ROOT / 'packaging/opencode-web.service', target)
                    target.chmod(0o644)
                    self.run('systemctl', 'daemon-reload')
            port = APP_PORTS.get(entry['target'].removesuffix('.service'))
            entry['url'] = _service_url(data['origin'], port)
            if name is not None:
                entry['name'] = name.strip()
            if url is not None:
                entry['url'] = url
            data['services'].append(entry)
            atomic_json(self.registry, data)
            return {'message': '현재 실행·자동 실행 상태를 유지해 등록했습니다.'}

    def _set_autostart(self, data: dict, entry: dict, enabled: bool, containers: list[dict]) -> None:
        if entry['kind'] == 'systemd':
            self.run('systemctl', 'enable' if enabled else 'disable', entry['target'])
            # An independently enabled socket must not reactivate a disabled service.
            activation_socket = ACTIVATION_SOCKETS.get(entry['target'])
            if activation_socket and self.unit_state(activation_socket).get('LoadState') == 'loaded':
                self.run('systemctl', 'enable' if enabled else 'disable', activation_socket)
        else:
            # Persist desired state before applying it, so a failed/partial update remains
            # visible as drift and recovery cannot restart a newly disabled project.
            entry['autostart'] = enabled
            atomic_json(self.registry, data)
            if containers:
                self.run('docker', 'update', '--restart=' + ('unless-stopped' if enabled else 'no'),
                         *(c['id'] for c in containers))
        entry['autostart'] = enabled
        atomic_json(self.registry, data)

    def act(self, identifier: str, action: str, confirmed: bool = False, source: str = 'cli') -> dict:
        if action not in ACTIONS:
            raise ControlError('지원하지 않는 동작입니다.')
        with self.mutation():
            data = self.load()
            entry = next((s for s in data['services'] if s['id'] == identifier), None)
            if entry is None:
                raise ControlError('등록된 서비스를 찾을 수 없습니다.')
            if entry['category'] == 'foundation' and not confirmed:
                raise ControlError(entry['notice'] + ' 확인 후 다시 실행하세요 (--confirm).')
            if source == 'web' and entry['target'] == 'tailscaled.service' and action in (
                    'disable', 'stop', 'restart', 'remove', 'autostart-off'):
                raise ControlError('Tailscale 연결·자동 실행을 끄는 작업은 PC의 wrg services 터미널에서 실행하세요.')
            if action == 'remove' and not confirmed:
                raise ControlError('자동 실행 해제·중지 후 등록을 삭제합니다. 확인이 필요합니다 (--confirm).')
            owned_path = Path('/etc/systemd/system/opencode-web.service')
            if action == 'remove' and entry['owned'] and owned_path.is_symlink():
                raise ControlError('심볼릭 링크 서비스는 직접 확인하세요.')
            try:
                containers = self.containers(entry['target']) if entry['kind'] == 'compose' else []
            except (ControlError, subprocess.TimeoutExpired):
                # A pure autostart toggle only records intent in the registry, so it
                # must not fail just because Docker is stopped; drift reporting will
                # surface the unapplied restart policy until the next snapshot.
                if entry['kind'] != 'compose' or action not in ('autostart-on', 'autostart-off'):
                    raise
                containers = []
            if entry['kind'] == 'compose' and not containers and action in ('start', 'restart', 'enable'):
                raise ControlError('기존 컨테이너가 없습니다. 프로젝트에서 Docker Compose로 먼저 생성하세요.')
            if action in ('enable', 'autostart-on', 'disable', 'autostart-off', 'remove'):
                self._set_autostart(data, entry, action in ('enable', 'autostart-on'), containers)
            operation = {'enable': 'start', 'disable': 'stop', 'remove': 'stop'}.get(action, action)
            if operation in ('start', 'stop', 'restart'):
                if entry['kind'] == 'systemd':
                    activation_socket = ACTIVATION_SOCKETS.get(entry['target'])
                    if activation_socket and operation == 'stop':
                        if self.unit_state(activation_socket).get('LoadState') == 'loaded':
                            self.run('systemctl', 'stop', activation_socket)
                    self.run('systemctl', operation, entry['target'])
                elif containers:
                    # Starting existing containers avoids rebuilding code, rereading .env,
                    # losing deployment overrides, or recreating database volumes.
                    self.run('docker', operation, *(c['id'] for c in containers))
            if action == 'remove':
                if entry['owned'] and owned_path.exists():
                    backup = self.registry.parent / f'opencode-web.service.backup-{time.time_ns()}'
                    shutil.copy2(owned_path, backup)
                    owned_path.unlink()
                    self.run('systemctl', 'daemon-reload')
                data['services'] = [s for s in data['services'] if s['id'] != identifier]
                atomic_json(self.registry, data)
            return {'message': '등록을 삭제했습니다. 프로그램·DB·볼륨·로그는 보존됩니다.' if action == 'remove' else '설정을 적용했습니다.'}

    def logs(self, identifier: str, lines: int = 80) -> dict:
        # Keep direct callers and socket requests on the same bounded contract.
        try:
            lines = int(lines) if type(lines) in (int, str) else 80
        except ValueError:
            lines = 80
        lines = min(max(lines, 1), MAX_LOG_LINES)
        entry = next((s for s in self.load()['services'] if s['id'] == identifier), None)
        if entry is None:
            raise ControlError('등록된 서비스를 찾을 수 없습니다.')
        if entry['kind'] == 'systemd':
            output = self.run('journalctl', '-u', entry['target'], '-n', str(lines), '--no-pager', '-o', 'short-iso')
            if entry['target'] in LOG_TAILS:
                try:
                    auxiliary = self._owner_read('logs', {'target': entry['target'], 'lines': lines})
                    if not isinstance(auxiliary, str):
                        raise ValueError('Invalid auxiliary log response')
                except (OSError, ValueError, ControlError, subprocess.TimeoutExpired) as exc:
                    auxiliary = f'[보조 로그 읽기 실패: {type(exc).__name__}]'
                output += '\n\n' + auxiliary
        else:
            chunks = []
            for container in self.containers(entry['target'])[:12]:
                # Docker logs writes application stderr to stderr; capture both streams.
                if self.run is command:
                    result = subprocess.run(['docker', 'logs', '--tail', str(lines), container['id']],
                                            capture_output=True, text=True, timeout=15)
                    text = result.stdout + result.stderr
                else:
                    text = self.run('docker', 'logs', '--tail', str(lines), container['id'])
                chunks.append(f"[{container['name']}]\n{text[-12000:]}")
            output = '\n'.join(chunks)
        return {'text': output.encode('utf-8')[-100000:].decode('utf-8', errors='ignore')}

    def restore(self, dry_run: bool = False) -> dict:
        """Boot recovery affects only explicitly registered Compose projects."""
        with self.mutation(wait=RESTORE_LOCK_WAIT_SECONDS):
            data = self.load()
            entries = [s for s in data['services'] if s['kind'] == 'compose']
            if not entries or self.unit_state('docker.service').get('ActiveState') != 'active':
                return {'message': 'Docker가 중지되어 있거나 복구할 프로젝트가 없습니다.'}
            failures = []
            planned = []
            for entry in entries:
                try:
                    group = self.containers(entry['target'])
                    if not group:
                        continue
                    policy = 'unless-stopped' if entry['autostart'] else 'no'
                    stopped = [c['id'] for c in group if c['state']['Status'] != 'running']
                    running = [c['id'] for c in group if c['state']['Status'] == 'running']
                    if dry_run:
                        steps = [f"restart={policy} ({len(group)}개 컨테이너)"]
                        if entry['autostart'] and stopped:
                            steps.append(f"start {len(stopped)}개")
                        if not entry['autostart'] and running:
                            steps.append(f"stop {len(running)}개")
                        planned.append(f"{entry['name']}: {', '.join(steps)}")
                        continue
                    self.run('docker', 'update', '--restart=' + policy, *(c['id'] for c in group))
                    if entry['autostart'] and stopped:
                        self.run('docker', 'start', *stopped)
                    elif not entry['autostart'] and running:
                        self.run('docker', 'stop', *running)
                except (ControlError, subprocess.TimeoutExpired) as exc:
                    failures.append(f"{entry['name']}: {exc}")
            if failures:
                raise ControlError('\n'.join(failures))
            if dry_run:
                detail = '\n'.join(planned) if planned else '적용할 변경이 없습니다.'
                return {'message': f'변경 없이 계획만 표시합니다.\n{detail}', 'actions': planned}
            return {'message': '등록된 프로젝트의 자동 실행 설정을 적용했습니다.'}

    def kill_tree(self, pid: int, *, expected_start_ticks: int | None = None, stale_only: bool = False) -> dict:
        """SIGTERM an LLM session or MCP tree root, re-validated at execution time."""
        if pid <= 1:
            raise ControlError('유효하지 않은 PID입니다.')
        from .processes import build_snapshot, descendants_of, is_stale_session, kill_block_reason, signal_verified_process
        owner = pwd.getpwnam(self.load()['owner'])
        settings = self._owner_settings()
        snapshot = build_snapshot(settings.project_roots, uid=owner.pw_uid)
        session = next((item for item in snapshot.sessions if item.root_pid == pid), None)
        group = next((item for item in snapshot.mcp_groups if item.root_pid == pid), None)
        if session is None and group is None:
            raise ControlError(f'PID {pid}는 현재 감지된 LLM 세션·MCP 루트가 아닙니다.')
        root = snapshot.processes.get(pid)
        if expected_start_ticks is not None and (root is None or root.start_ticks != expected_start_ticks):
            raise ControlError('대상 프로세스가 바뀌어 종료하지 않았습니다.')
        if stale_only and (session is None or not is_stale_session(session, settings.stale_session_hours)):
            raise ControlError('현재 오래된 세션이 아니어서 종료하지 않았습니다.')
        reason = kill_block_reason(snapshot.processes.get(pid))
        if reason:
            raise ControlError(reason)
        processes = descendants_of(pid, snapshot.processes)
        if not processes:
            raise ControlError('대상 프로세스를 찾지 못했습니다.')
        signalled = 0
        for process in processes:
            if process.uid != owner.pw_uid:
                continue  # The snapshot is uid-filtered already; belt and suspenders.
            try:
                signalled += int(signal_verified_process(process, owner.pw_uid, signal.SIGTERM))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise ControlError(f'PID {process.pid} 종료를 중단했습니다: {exc}') from None
        if not signalled:
            raise ControlError('신호를 보낼 프로세스가 없습니다. 이미 종료됐을 수 있습니다.')
        name = session.root_name if session else group.root_name
        return {'message': f'{name} 트리({signalled}개 프로세스)에 SIGTERM을 보냈습니다. 자동 재시작은 하지 않습니다.'}

    def kill_stale(self, pids: object) -> dict:
        """SIGTERM the stale sessions the user saw, re-checked now.

        Only PIDs that are both in the request and still stale and killable
        are signalled, so a list that went out of date cannot hit a session
        that became active again.
        """
        if not isinstance(pids, list) or not pids or len(pids) > 50 or not all(
                isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 for pid in pids):
            raise ControlError('종료할 세션 목록이 올바르지 않습니다.')
        from .processes import build_snapshot, is_stale_session, kill_block_reason
        owner = pwd.getpwnam(self.load()['owner'])
        settings = self._owner_settings()
        snapshot = build_snapshot(settings.project_roots, uid=owner.pw_uid)
        eligible = {s.root_pid: snapshot.processes[s.root_pid].start_ticks for s in snapshot.sessions
                    if is_stale_session(s, settings.stale_session_hours)
                    and not kill_block_reason(snapshot.processes.get(s.root_pid))
                    and snapshot.processes[s.root_pid].start_ticks is not None}
        done, skipped = [], []
        for pid in pids:
            if pid not in eligible:
                skipped.append(pid)
                continue
            try:
                self.kill_tree(pid, expected_start_ticks=eligible[pid], stale_only=True)
                done.append(pid)
            except ControlError:
                skipped.append(pid)
        self._monitor_cache = {}
        message = f'오래된 세션 {len(done)}개에 SIGTERM을 보냈습니다.'
        if skipped:
            message += f' {len(skipped)}개는 지금 오래된 세션이 아니거나 종료할 수 없어 건너뛰었습니다.'
        return {'message': message, 'killed': done, 'skipped': skipped}

    def summary(self) -> dict:
        """Small, cheap status for the live stream and the menu badges."""
        state = self._owner_context()['state']
        since = state.get('condition_since') if isinstance(state.get('condition_since'), dict) else {}
        disk_keys = [k for k in since if '.disk.' in k]
        # Each snapshot costs a systemctl call per service; the stream polls
        # every few seconds, so service rows are refreshed at most every 30 s.
        if self._summary_rows is None or time.monotonic() - self._summary_rows_at >= 30:
            self._summary_rows, self._summary_rows_at = self.snapshot()['services'], time.monotonic()
        rows = self._summary_rows
        problems = [r for r in rows if r.get('category') == 'app' and (
            r.get('state') in ('failed', 'degraded', 'unknown', 'missing') or r.get('error')
            or r.get('drift') or (r.get('autostart') and r.get('state') != 'active')
            or (r.get('state') == 'active' and (r.get('health') or {}).get('ok') is False))]
        cached = self._monitor_cache or {}
        return {'severity': state.get('severity', 'unknown'),
                'updated_at': state.get('updated_at'),
                'reasons': state.get('reasons', []),
                'disk_alerts': sorted({k.split('.disk.', 1)[-1] for k in disk_keys}),
                'disk_critical': any(k.startswith('critical.') for k in disk_keys),
                'service_problems': len(problems),
                'service_failed': any(r.get('state') == 'failed' for r in problems),
                'stale_sessions': sum(1 for r in cached.get('sessions', []) if r.get('stale')),
                'snooze_until': self.snooze_until()}

    def record_service_memory(self, retention_days: int = 14) -> None:
        """Append one per-service memory sample to today's jsonl; rotate old files."""
        rows = self.snapshot()['services']
        values = {row['id']: row['memory_bytes'] for row in rows
                  if row.get('memory_bytes') is not None}
        if not values:
            return
        stamp = time.time()
        sampled_at = datetime.fromtimestamp(stamp).astimezone()
        directory = self.registry.parent
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'service-memory-{sampled_at:%Y-%m-%d}.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'a') as handle:
            handle.write(json.dumps({'timestamp': stamp, 'services': values},
                                    ensure_ascii=False, separators=(',', ':')) + '\n')
        cutoff = sampled_at.date() - timedelta(days=retention_days)
        for candidate in directory.glob('service-memory-*.jsonl'):
            try:
                day = datetime.strptime(candidate.stem.removeprefix('service-memory-'),
                                        '%Y-%m-%d').date()
            except ValueError:
                continue
            if day < cutoff:
                candidate.unlink(missing_ok=True)

    def service_memory_history(self, limit: int = 1440) -> list:
        """Chronological per-service memory samples (default: last ~24h at 1/min)."""
        records = []
        for path in sorted(self.registry.parent.glob('service-memory-*.jsonl'), reverse=True):
            for line in reversed(tail_lines(path, limit - len(records))):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                services = record.get('services')
                if isinstance(record.get('timestamp'), (int, float)) and isinstance(services, dict):
                    records.append({'timestamp': record['timestamp'], 'services': services})
            if len(records) >= limit:
                break
        return list(reversed(records))

    def service_memory_leaks(self, window: int = 360) -> list[dict]:
        """Services whose memory grew steadily across the recent ~6h window."""
        records = self.service_memory_history(window)
        if len(records) < 180:
            return []
        span = records[-1]['timestamp'] - records[0]['timestamp']
        if span < 3 * 3600:
            return []
        names = {s['id']: s['name'] for s in self.load()['services']}
        leaks = []
        for sid in sorted({sid for r in records for sid in r['services']}):
            series = [r['services'].get(sid) for r in records]
            series = [v for v in series if isinstance(v, (int, float))]
            if len(series) < 180:
                continue
            first, last = series[0], series[-1]
            steps_up = sum(1 for a, b in zip(series, series[1:]) if b >= a)
            if (last - first >= 512 * 2**20 and last >= first * 1.3
                    and steps_up / (len(series) - 1) >= 0.6):
                leaks.append({'id': sid, 'name': names.get(sid, sid),
                              'from_bytes': first, 'to_bytes': last,
                              'hours': round(span / 3600, 1)})
        return leaks

    def check_health(self, probe=probe_url) -> None:
        """HTTP-probe each registered app URL; results ride on the next snapshot."""
        data = self.load()
        # Only the dashboard's own host is probed: the controller runs as root
        # and must not become a request relay to arbitrary registered hosts.
        own_host = (urlsplit(data.get('origin', '')).hostname or '').lower()
        entries = [s for s in data['services']
                   if s.get('category') == 'app' and s.get('url') and own_host
                   and (urlsplit(s['url']).hostname or '').lower() == own_host]
        fresh = {}
        for entry in entries:
            result = probe(entry['url'])
            result['url'] = entry['url']
            fresh[entry['id']] = result
        self._health = fresh
        self._invalidate_snapshot()

    def docker_df(self) -> dict:
        """Read-only `docker system df`; cached because it walks image layers."""
        if self._docker_df_cache is not None and time.monotonic() - self._docker_df_at < 300:
            return self._docker_df_cache
        if self.unit_state('docker.service').get('ActiveState') != 'active':
            return {'available': False, 'error': 'Docker가 중지되어 있습니다.', 'rows': []}
        output = self.run('docker', 'system', 'df', '--format', '{{json .}}', timeout=60)
        rows = []
        for line in output.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get('Type'), str):
                rows.append({key: str(item.get(key, '')) for key in
                             ('Type', 'TotalCount', 'Active', 'Size', 'Reclaimable')})
        result = {'available': True, 'rows': rows, 'checked_at': time.time()}
        self._docker_df_cache, self._docker_df_at = result, time.monotonic()
        return result

    def service_memory_loop(self, interval: float = 60.0, stop: threading.Event | None = None) -> None:
        """Background sampler so the dashboard can draw per-service memory trends."""
        while stop is None or not stop.is_set():
            try:
                self.record_service_memory(self._owner_settings().retention_days)
            except Exception as exc:
                print(f'service memory sample error={type(exc).__name__}: {exc}', flush=True)
            try:
                self.check_health()
            except Exception as exc:
                print(f'service health check error={type(exc).__name__}: {exc}', flush=True)
            if stop is not None:
                stop.wait(interval)
            else:
                time.sleep(interval)

    @staticmethod
    def _session_row(row, snapshot, settings) -> dict:
        """Attach the server's stale/killable judgment so clients do not re-derive it."""
        from .processes import is_stale_session, kill_block_reason
        data = row.to_dict()
        data['stale'] = is_stale_session(row, settings.stale_session_hours)
        reason = kill_block_reason(snapshot.processes.get(row.root_pid))
        data['killable'] = not reason
        if reason:
            data['kill_block_reason'] = reason
        from .identity import describe_target
        data['identity'] = describe_target(snapshot, row.root_pid)
        return data

    @staticmethod
    def _mcp_row(row, snapshot) -> dict:
        from .processes import kill_block_reason
        data = row.to_dict()
        reason = kill_block_reason(snapshot.processes.get(row.root_pid))
        data['killable'] = not reason
        if reason:
            data['kill_block_reason'] = reason
        from .identity import describe_target
        data['identity'] = describe_target(snapshot, row.root_pid)
        return data

    def monitor(self, force: bool = False) -> dict:
        # Browser polling must never call daemon.sample(), which saves notification state.
        with self._monitor_lock:
            now = time.monotonic()
            if self._monitor_cache and now - self._monitor_at < (5 if force else 30):
                return self._monitor_cache
            from .metrics import read_system_metrics
            from .processes import apply_cpu_rates, build_snapshot
            owner = pwd.getpwnam(self.load()['owner'])
            settings = self._owner_settings()
            state = self._owner_context()['state']
            snapshot = build_snapshot(settings.project_roots, uid=owner.pw_uid)
            apply_cpu_rates(snapshot, state.get('cpu_jiffies', {}), time.time(),
                            os.sysconf('SC_CLK_TCK'))
            metrics = state.get('metrics') or read_system_metrics().to_dict()
            top_other = sorted(
                (p for pid, p in snapshot.processes.items()
                 if pid not in snapshot.root_for_pid),
                key=lambda p: p.rss_kib, reverse=True,
            )[:10]
            thresholds = {key: getattr(settings, key) for key in (
                'warning_available_gib', 'critical_available_gib',
                'warning_psi_some_avg60', 'critical_psi_full_avg60',
                'warning_swap_out_mib_per_minute',
                'warning_disk_free_percent', 'critical_disk_free_percent')}
            result = {'collected_at': time.time(), 'daemon_updated_at': state.get('updated_at'),
                      'metrics': metrics, 'severity': state.get('severity', 'unknown'),
                      'thresholds': thresholds,
                      'reasons': state.get('reasons', []), 'observations': state.get('observations', []),
                      'pending_reasons': state.get('pending_reasons', []),
                      'top': [row.to_dict() for row in snapshot.project_usage],
                      'sessions': [self._session_row(row, snapshot, settings)
                                  for row in snapshot.sessions],
                      'mcp': [self._mcp_row(row, snapshot) for row in snapshot.mcp_groups],
                      'mcp_count': snapshot.mcp_count, 'mcp_process_count': snapshot.mcp_process_count,
                      'mcp_rss_kib': snapshot.mcp_rss_kib, 'stale_session_hours': settings.stale_session_hours,
                      'top_other': [{'pid': p.pid, 'name': p.name, 'rss_kib': p.rss_kib,
                                     'swap_kib': p.swap_kib, 'age_seconds': p.age_seconds}
                                    for p in top_other],
                      'leaks': self.service_memory_leaks(),
                      'snooze_until': self.snooze_until(),
                      'alerts': {'channels': self._owner_context()['channels'],
                                 'channel_errors': (state.get('last_channel_errors')
                                                    if isinstance(state.get('last_channel_errors'), dict) else {}),
                                 'email_heartbeat_enabled': settings.email_heartbeat_enabled,
                                 'last_alert': state.get('last_alert'), 'last_email_status': state.get('last_email_status')}}
            self._monitor_cache, self._monitor_at = result, now
            return result

    def _owner_read(self, operation: str, args: dict | None = None):
        from .owner_worker import call_owner
        owner = pwd.getpwnam(self.load()['owner'])
        try:
            return call_owner(owner, operation, args)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ControlError(f'소유자 데이터 읽기 실패: {type(exc).__name__}') from None

    def _owner_context(self):
        with self._owner_lock:
            if self._owner_cache is None or time.monotonic() - self._owner_at >= 2:
                self._owner_cache = self._owner_read('context')
                self._owner_at = time.monotonic()
            return self._owner_cache

    def _owner_settings(self):
        """Reconstruct settings without reading any owner-controlled path as root."""
        from .config import Settings
        values = dict(self._owner_context()['settings'])
        values['config_path'] = Path(values['config_path'])
        values['secrets_path'] = Path(values['secrets_path'])
        return Settings(**values)

    def _cached_owner_history(self, key, operation, args=None, ttl=60):
        with self._history_lock:
            cached = self._history_cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < ttl:
                return cached[1]
        result = self._owner_read(operation, args)
        with self._history_lock:
            self._history_cache[key] = (time.monotonic(), result)
        return result

    def history(self, range_key: str = '3h') -> list:
        from .history import HISTORY_RANGES
        key = range_key if range_key in HISTORY_RANGES else '3h'
        return self._cached_owner_history(key, 'history', {'range': key})

    def attribution(self, range_key: str = '3h') -> dict:
        from .history import HISTORY_RANGES
        key = range_key if range_key in HISTORY_RANGES else '3h'
        return self._cached_owner_history('attribution:' + key, 'attribution', {'range': key})

    def session_history(self, days: int = 7) -> list:
        return self._cached_owner_history('sessions', 'session-history', ttl=120)

    def alert_episodes(self, gap_seconds: int = 1800) -> dict:
        return self._cached_owner_history('alerts', 'alerts', ttl=300)

    def incidents(self, identifier=None):
        from .incidents import IDENTIFIER
        if identifier is not None and not IDENTIFIER.fullmatch(str(identifier)):
            raise ControlError('경보 식별자가 올바르지 않습니다.')
        store = self._owner_read('incidents', {'id': identifier})
        observed = store.get('updated_at', 0)
        store['stale'] = not observed or not 0 <= time.time() - observed <= max(60, self._owner_settings().interval_seconds * 3)
        decisions = self.read_shared('incident-decisions.json')
        for row in store['incidents']:
            row['decision'] = decisions.get(row['id'], {})
        if identifier is not None:
            row = next((r for r in store['incidents'] if r['id'] == identifier), None)
            if row is None:
                raise ControlError('경보 기록이 없거나 보관 기간이 지났습니다. 다른 대상에 연결하지 않습니다.', code='incident_not_found')
            return {'incident': row, 'stale': store['stale'], 'updated_at': observed,
                    'delivery_unavailable': store.get('delivery_unavailable', False)}
        return store

    def incident_decision(self, identifier, choice, minutes, actor):
        if choice not in ('acknowledge', 'defer', 'resume'):
            raise ControlError('경보 대응을 다시 선택하세요.')
        if choice == 'defer' and (type(minutes) is not int or minutes not in (30, 60, 240, 480)):
            raise ControlError('재확인 시간을 다시 선택하세요.')
        with self.mutation():
            row = self.incidents(identifier)['incident']
            if row['state'] == 'resolved':
                raise ControlError('이미 해소된 경보입니다. 최신 상태를 확인하세요.')
            now = time.time()
            decisions = self.read_shared('incident-decisions.json')
            decisions = {k: v for k, v in decisions.items() if now - v.get('at', 0) < 30 * 86400}
            if identifier not in decisions and len(decisions) >= 256:
                oldest = min(decisions, key=lambda k: decisions[k].get('at', 0))
                decisions.pop(oldest)
            decisions[identifier] = {'choice': choice, 'at': now, 'until': now + minutes * 60 if choice == 'defer' else 0,
                                     'severity': row['severity'], 'revision': row['revision'], 'actor': str(actor)[:160]}
            self.write_shared('incident-decisions.json', decisions)
        return {'message': '이 경보의 대응 상태를 저장했습니다. 새 경보와 위험도 상승은 계속 알립니다.'}

    def _action_snapshot(self, target_id=None):
        from .identity import process_identity, boot_id, describe_target
        from .processes import build_snapshot, descendants_of
        from .incidents import IDENTIFIER
        if target_id is not None and not IDENTIFIER.fullmatch(str(target_id)):
            raise ControlError('대상 식별자가 올바르지 않습니다.')
        owner = pwd.getpwnam(self.load()['owner'])
        snapshot = build_snapshot(self._owner_settings().project_roots, uid=owner.pw_uid)
        pid, metadata = None, {}
        if target_id is not None:
            boot = boot_id()
            roots = {s.root_pid for s in [*snapshot.sessions, *snapshot.mcp_groups]}
            pid = next((p for p in roots if process_identity(snapshot.processes[p], boot) == target_id), None)
            if pid is None:
                raise ControlError('대상이 종료됐거나 신원이 변경됐습니다. 이전 대상을 다시 조치하지 않습니다.', code='target_not_found')
            members = descendants_of(pid, snapshot.processes)
            try:
                metadata = self._owner_read('task-metadata', {'pids': [p.pid for p in members[:512]]})
            except ControlError:
                metadata = {}
        return snapshot, pid, metadata, owner

    def target_detail(self, identifier):
        from .identity import describe_target, process_identity, tool_label
        from .processes import descendants_of
        snapshot, pid, metadata, _owner = self._action_snapshot(identifier)
        result = describe_target(snapshot, pid, metadata)
        members = {p.pid for p in descendants_of(pid, snapshot.processes)}
        groups = [g for g in snapshot.mcp_groups if g.root_pid in members]
        result['tools_total'] = len(groups)
        result['tools'] = [{ 'id': process_identity(snapshot.processes[g.root_pid]), 'root_pid': g.root_pid,
                            'name': tool_label(descendants_of(g.root_pid, snapshot.processes)), 'project': g.project, 'rss_kib': g.rss_kib,
                            'age_seconds': g.age_seconds,
                            'process_count': g.process_count}
                           for g in groups[:64]]
        result['observed_at'] = time.time()
        return result

    def action_operation(self, operation, identifier, actor):
        from .actions import Actions
        from .incidents import IDENTIFIER
        from .processes import process_start_ticks
        if not IDENTIFIER.fullmatch(str(identifier)):
            raise ControlError('조치 식별자가 올바르지 않습니다.')
        actions = Actions(self.registry.parent / 'actions.json')
        if operation == 'status':
            record = actions.load().get(identifier)
            if record and record.get('actor') == actor and record['status'] == 'executing':
                if record.get('execution_owner') == [os.getpid(), process_start_ticks(os.getpid())]:
                    result = actions.public(record)
                    if time.time() - record.get('progress_at', 0) > 30:
                        result.update(status='unknown', message='처리 진행을 확인하지 못했습니다. 중복 실행하지 말고 대상 상태를 확인하세요.')
                    return result
        with self.mutation():
            try:
                if operation == 'preview':
                    snapshot, pid, metadata, _owner = self._action_snapshot(identifier)
                    return actions.preview(snapshot, pid, metadata, actor)
                if operation == 'execute':
                    records = actions.load()
                    record = records.get(identifier)
                    if not record or record.get('actor') != actor:
                        raise ControlError('조치 기록을 찾을 수 없습니다.', code='action_not_found')
                    if record['status'] != 'preview':
                        snapshot, _, _, _ = self._action_snapshot()
                        return actions.status(identifier, snapshot, actor)
                    try:
                        snapshot, _pid, metadata, owner = self._action_snapshot(record['target']['id'])
                    except ControlError:
                        snapshot, _, metadata, owner = self._action_snapshot()
                    return actions.execute(identifier, snapshot, metadata, actor, owner.pw_uid)
                snapshot, _, _, _ = self._action_snapshot()
                return actions.status(identifier, snapshot, actor)
            except ValueError as exc:
                # These modules return controlled diagnostic messages, not command output.
                raise ControlError('조치 확인에 실패했습니다. 대상 정보와 기존 조치 결과를 다시 확인하세요.', code='action_unavailable') from None

    def disks(self, force: bool = False) -> dict:
        if force:
            result = self._owner_read('disks', {'force': True})
            with self._history_lock:
                self._history_cache['disks'] = (time.monotonic(), result)
            return result
        return self._cached_owner_history('disks', 'disks', ttl=60)

    def settings_info(self) -> dict:
        result = self._owner_read('settings')
        result['config_request'] = self.config_request_status()
        return result

    def audit(self, request: dict, uid: int, error: str = '', result: object = None) -> None:
        # Record identifiers and outcome only; never persist logs, commands, or secrets.
        record = {'time': datetime.now(timezone.utc).isoformat(), 'uid': uid,
                  'actor': str(request.get('actor', ''))[:160], 'op': request.get('op'),
                  'id': request.get('id', request.get('key', request.get('pid', ''))), 'action': request.get('action', ''),
                  'ok': not error, 'error': error[:1000]}
        # What a bulk or settings operation actually targeted: snooze length,
        # requested and signalled PIDs, the changed key and value.
        detail = {k: request[k] for k in ('minutes', 'pids', 'value') if k in request}
        if isinstance(result, dict) and isinstance(result.get('killed'), list):
            detail['killed'] = result['killed']
        if request.get('op') in ('action-preview', 'action-execute') and isinstance(result, dict):
            detail.update(operation_id=result.get('id'), status=result.get('status'),
                          signalled=result.get('signalled', []))
        if request.get('op') == 'incident-decision':
            detail['choice'] = request.get('choice')
        if detail:
            record['detail'] = json.loads(json.dumps(detail, default=str))
        path = self.registry.parent / 'audit.jsonl'
        rotate_lines(path, 512 * 1024, 200)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')

    def dispatch(self, request: dict, uid: int, web_uid: int) -> object:
        if not isinstance(request, dict):
            raise ControlError('잘못된 요청입니다.')
        op = request.get('op')
        if op == 'incidents':
            return self.incidents(request.get('id'))
        if op == 'target-detail':
            return self.target_detail(request.get('id'))
        if op in ('action-preview', 'action-execute', 'action-status'):
            result = self.action_operation(op.removeprefix('action-'), request.get('id'), str(request.get('actor', '')))
            if op != 'action-status':
                error = '종료 요청 미실행 또는 일부 처리 실패' if result.get('status') in ('rejected', 'partial', 'unknown', 'expired') else ''
                self.audit(request, uid, error=error, result=result)
            return result
        if op == 'incident-decision':
            result = self.incident_decision(request.get('id'), request.get('choice'), request.get('minutes'), request.get('actor', ''))
            self.audit(request, uid, result=result)
            return result
        if op == 'list':
            return self.snapshot()
        if op == 'discover':
            registered = {s['key'] for s in self.load()['services']}
            return [c for c in self.discover() if c['key'] not in registered]
        if op == 'monitor':
            return self.monitor(request.get('force') is True)
        if op == 'history':
            return self.history(str(request.get('range', '3h')))
        if op == 'alerts':
            return self.alert_episodes()
        if op == 'attribution':
            return self.attribution(str(request.get('range', '3h')))
        if op == 'session-history':
            return self.session_history()
        if op == 'disks':
            return self.disks(request.get('force') is True)
        if op == 'service-memory':
            from .history import downsample_service_memory
            return downsample_service_memory(self.service_memory_history())
        if op == 'settings':
            return self.settings_info()
        if op == 'config-request':
            return self.config_request_status(request.get('id'))
        if op == 'docker-df':
            return self.docker_df()
        if op == 'summary':
            return self.summary()
        if op == 'push-key':
            return self.push_key()
        if op == 'push-status':
            return self.push_status(request.get('subscription'))
        if op == 'audit':
            records = []
            for line in tail_lines(self.registry.parent / 'audit.jsonl', 50):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return records
        if op == 'logs':
            return self.logs(str(request.get('id', '')), request.get('lines', 80))
        try:
            if op == 'register':
                result = self.register(str(request.get('key', '')), request.get('name'), request.get('url'))
            elif op == 'action':
                result = self.act(str(request.get('id', '')), str(request.get('action', '')),
                                  request.get('confirmed') is True, 'web' if uid == web_uid else 'cli')
            elif op == 'kill':
                if uid == web_uid:
                    raise ControlError('종료 영향 미리보기를 다시 열어 확인하세요.', code='preview_required')
                if request.get('confirmed') is not True:
                    raise ControlError('세션·MCP 종료에는 확인이 필요합니다.')
                try:
                    pid = int(request.get('pid', 0))
                except (TypeError, ValueError):
                    pid = 0
                result = self.kill_tree(pid)
            elif op == 'snooze':
                result = self.snooze(request.get('minutes'), str(request.get('actor', '')))
            elif op == 'push-subscribe':
                result = self.push_subscribe(request.get('subscription'), request.get('label'))
            elif op == 'push-unsubscribe':
                result = self.push_unsubscribe(request.get('endpoint'))
            elif op == 'push-test':
                result = self.push_test()
            elif op == 'config-change':
                result = self.request_config_change(request.get('key'), request.get('value'),
                                                    str(request.get('actor', '')))
            elif op == 'kill-stale':
                if uid == web_uid:
                    raise ControlError('대상을 개별 확인한 뒤 종료하세요.', code='preview_required')
                if request.get('confirmed') is not True:
                    raise ControlError('세션 종료에는 확인이 필요합니다.')
                result = self.kill_stale(request.get('pids'))
            elif op == 'restore':
                if uid == web_uid:
                    raise ControlError('복구는 PC의 wrg services 터미널에서 실행하세요.')
                if not request.get('dry_run') and request.get('confirmed') is not True:
                    raise ControlError('컨테이너를 시작·중지합니다. --dry-run으로 확인 뒤 --confirm을 붙여 실행하세요.')
                result = self.restore(request.get('dry_run') is True)
            else:
                raise ControlError('지원하지 않는 요청입니다.')
        except Exception as exc:
            self.audit(request, uid, str(exc))
            raise
        self.audit(request, uid, result=result)
        return result


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, controller: Controller, owner_uid: int, web_uid: int):
        self.controller = controller
        self.allowed_uids = {0, owner_uid, web_uid}
        self.web_uid = web_uid
        super().__init__(path, ControlHandler)


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        try:
            if uid not in self.server.allowed_uids:
                raise ControlError('이 사용자는 서비스 관리 권한이 없습니다.')
            raw = self.rfile.readline(16385)
            if len(raw) > 16384 or not raw.endswith(b'\n'):
                raise ControlError('요청 크기 제한을 초과했습니다.')
            result = self.server.controller.dispatch(json.loads(raw), uid, self.server.web_uid)
            payload = {'ok': True, 'data': result}
        except (ControlError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            payload = {'ok': False, 'error': str(exc)}
            from .services import ERROR_STATUSES
            code = getattr(exc, 'code', '')
            if code in ERROR_STATUSES:
                payload['code'] = code
        except Exception:
            import traceback
            traceback.print_exc()
            payload = {'ok': False, 'error': '서비스 관리 내부 오류입니다. 관리자 서비스 로그를 확인하세요.'}
        try:
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode() + b'\n')
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('serve', 'restore', 'init'))
    parser.add_argument('--owner')
    parser.add_argument('--origin')
    args = parser.parse_args()
    controller = Controller()
    if args.mode == 'init':
        if not args.origin or not args.owner:
            parser.error('--owner and --origin are required for init')
        controller.init(args.owner, args.origin)
    elif args.mode == 'restore':
        try:
            print(controller.restore()['message'])
        except ControlError as exc:
            print(f'복구 실패: {exc}', file=sys.stderr)
            return 1
    else:
        owner = pwd.getpwnam(controller.load()['owner'])
        web = pwd.getpwnam('wrg-web')
        path = Path(SOCKET)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        with ControlServer(SOCKET, controller, owner.pw_uid, web.pw_uid) as server:
            os.chown(path, owner.pw_uid, web.pw_gid)
            os.chmod(path, 0o660)
            sampler_stop = threading.Event()
            threading.Thread(target=controller.service_memory_loop,
                             kwargs={'stop': sampler_stop}, daemon=True).start()
            try:
                server.serve_forever()
            finally:
                sampler_stop.set()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
