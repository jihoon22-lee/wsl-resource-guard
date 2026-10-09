"""Bounded, owner-written incident state shared by notifications and the mobile UI."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from datetime import datetime

from .identity import boot_id, describe_target, process_identity
from .safe_read import read_text

MAX_INCIDENTS = 256
IDENTIFIER = re.compile(r'^[0-9a-f]{32}$')


def atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_store(path: Path) -> dict:
    try:
        value = json.loads(read_text(path, max_bytes=4 * 1024 * 1024))
    except FileNotFoundError:
        return {'version': 1, 'updated_at': 0, 'incidents': []}
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('incidents'), list) or len(value['incidents']) > MAX_INCIDENTS):
        raise ValueError('Invalid incident store')
    for row in value['incidents']:
        if not isinstance(row, dict) or not IDENTIFIER.fullmatch(str(row.get('id', ''))):
            raise ValueError('Invalid incident identity')
    return value


def _key(condition: dict, snapshot, boot: str) -> str:
    key = condition['key'].split('.', 1)[-1]
    if key.startswith('session.'):
        root = snapshot.processes.get(int(key.split('.')[-1]))
        return 'session:' + (process_identity(root, boot) if root else 'unknown:' + key)
    return key + ':' + boot


def _targets(condition: dict, snapshot) -> list[dict]:
    key = condition['key']
    if '.session.' in key:
        pids = [int(key.split('.')[-1])]
    elif key.endswith('mcp_total'):
        pids = [s.root_pid for s in sorted(snapshot.sessions, key=lambda s: s.mcp_rss_kib, reverse=True) if s.mcp_rss_kib][:8]
    elif any(word in key for word in ('available', 'ram', 'swap', 'psi', 'oom')):
        pids = [s.root_pid for s in snapshot.sessions[:5]]
    else:
        pids = []
    rows = []
    for pid in pids:
        try:
            data = describe_target(snapshot, pid)
        except ValueError:
            continue
        item = {k: data[k] for k in ('id', 'root_pid', 'provider', 'kind', 'name', 'project',
                                     'rss_kib', 'process_count', 'killable', 'kill_block_reason')}
        item['projects'] = [{'project': p['project'][:160]} for p in data['projects'][:8]]
        item['projects_complete'] = len(data['projects']) <= 8 and data['projects_complete']
        rows.append(item)
    return rows


def _unobserved(row: dict, metrics) -> bool:
    key = row['key']
    if 'psi' in key:
        return metrics.psi_some_avg60 is None
    if key.startswith('disk.'):
        drive = key.split(':', 1)[0].split('.', 1)[1]
        # Windows drive IDs contain ':', so match the letter as well.
        disk = next((d for d in metrics.disks if str(d.get('id', '')).rstrip(':') == drive.rstrip(':')), None)
        return disk is None or disk.get('status') != 'ok'
    return False


def _measurement(condition, snapshot, metrics, settings):
    key = condition['key']
    if key.endswith('mcp_total'):
        return {'value': round(snapshot.mcp_rss_kib / 1048576, 2), 'threshold': settings.mcp_warning_rss_gib, 'unit': 'GiB', 'label': 'MCP RSS 합계'}
    if '.session.' in key:
        session = next((s for s in snapshot.sessions if s.root_pid == int(key.split('.')[-1])), None)
        return {'value': round(session.rss_kib / 1048576, 2) if session else None, 'threshold': settings.session_warning_rss_gib, 'unit': 'GiB', 'label': '실행 트리 RSS'}
    if key.endswith('available_ram'):
        limit = settings.critical_available_gib if condition['severity'] == 'critical' else settings.warning_available_gib
        return {'value': round(metrics.mem_available_gib, 2), 'threshold': limit, 'unit': 'GiB', 'label': '가용 RAM · 기준 미만'}
    return {}


def reconcile(previous: dict, conditions: list[dict], snapshot, metrics, settings, *, boot=None) -> dict:
    now = metrics.timestamp
    boot = boot_id() if boot is None else boot
    rows = copy.deepcopy(previous.get('incidents', []))
    active = {r['key']: r for r in rows if r.get('state') != 'resolved'}
    seen = set()
    pressure = (metrics.mem_available_gib < settings.warning_available_gib
                or (metrics.psi_some_avg60 is not None and metrics.psi_some_avg60 >= settings.warning_psi_some_avg60)
                or any(c['key'].endswith('oom_kill') for c in conditions))
    for condition in conditions:
        key = _key(condition, snapshot, boot)
        seen.add(key)
        row = active.get(key)
        if row is None:
            row = {'id': secrets.token_hex(16), 'key': key, 'opened_at': now, 'revision': 0,
                   'state': 'new', 'severity': '', 'transitions': [], 'initial_reason': condition['reason']}
            rows.append(row)
        targets = _targets(condition, snapshot)
        changed = (row['state'] not in ('active', 'resolving') or row['severity'] != condition['severity']
                   or (row.get('targets') is not None and {t['id'] for t in row['targets']} != {t['id'] for t in targets}))
        if changed:
            row['revision'] += 1
            row['transitions'] = (row.get('transitions', []) + [{'at': now, 'state': 'active',
                                    'severity': condition['severity']}])[-16:]
        measurement = _measurement(condition, snapshot, metrics, settings)
        if measurement and 'initial_value' not in row:
            row['initial_value'] = measurement.get('value')
        if measurement.get('value') is not None and row.get('initial_value') is not None:
            measurement['change_since_open'] = round(measurement['value'] - row['initial_value'], 2)
        benign_memory = (not pressure and metrics.psi_some_avg60 is not None
                         and (condition['key'].endswith('mcp_total') or '.session.' in condition['key']))
        row.update(state='active', severity=condition['severity'], reason=condition['reason'],
                   observed_at=now, normal_since=None, targets=targets,
                   measurement=measurement,
                   priority='pressure' if pressure or condition['severity'] == 'critical' else 'review',
                   evidence={'available_gib': round(metrics.mem_available_gib, 2),
                             'psi_some_avg60': metrics.psi_some_avg60,
                             'swap_out_mib_per_minute': metrics.swap_out_mib_per_minute},
                   guidance=('현재 측정에서는 가용 RAM과 PSI가 압박 기준 안에 있습니다. 작업을 사용 중이면 유지하며 추이를 확인하세요.' if benign_memory else
                             '자원 압박이 관측됐습니다. 저장할 작업과 주요 점유 대상을 확인하세요.' if pressure else
                             '자원 기준을 넘었습니다. 점유량만으로 작업 중단이나 누수를 단정하지 않습니다.'))
    for row in rows:
        if row['key'] in seen or row['state'] == 'resolved':
            continue
        gap = now < row.get('observed_at', now) or now - row.get('observed_at', now) > max(60, 3 * settings.interval_seconds)
        if gap or _unobserved(row, metrics):
            row.update(state='unknown', normal_since=None, observed_at=now)
            continue
        since = row.get('normal_since')
        since = now if since is None or since > now else since
        row.update(state='resolving', normal_since=since, observed_at=now)
        if now - since >= settings.recovery_sustain_seconds:
            row.update(state='resolved', resolved_at=now, revision=row['revision'] + 1)
            row['transitions'] = (row.get('transitions', []) + [{'at': now, 'state': 'resolved'}])[-16:]
    cutoff = now - settings.retention_days * 86400
    rows = [r for r in rows if r['state'] != 'resolved' or r.get('resolved_at', now) >= cutoff]
    rows.sort(key=lambda r: (r['state'] != 'resolved', r['opened_at']), reverse=True)
    total = len(rows)
    rows = rows[:MAX_INCIDENTS]
    while rows and len(json.dumps(rows, ensure_ascii=False).encode()) > 3 * 1024 * 1024:
        rows.pop()
    return {'version': 1, 'updated_at': now, 'incidents': rows,
            'truncated': len(rows) < total, 'retention_days': settings.retention_days,
            'clock_adjusted': now < previous.get('updated_at', 0)}


def incident_message(row: dict, origin: str) -> tuple[str, str, str]:
    import html
    from urllib.parse import urlsplit
    parsed = urlsplit(origin)
    valid_origin = (parsed.scheme == 'https' and parsed.hostname and not parsed.username
                    and not parsed.password and not parsed.query and not parsed.fragment
                    and parsed.path in ('', '/'))
    url = origin.rstrip('/') + '/#incident?id=' + row['id'] if valid_origin else ''
    label = '회복 확인' if row['state'] == 'resolved' else '자원 압박' if row.get('priority') == 'pressure' else '점유 상태 확인'
    names = list(dict.fromkeys(t.get('provider', '') for t in row.get('targets', [])))
    title = f"{label} · {' / '.join(names) or 'WSL'}"
    shared = any(t.get('kind') == 'shared_runtime' for t in row.get('targets', []))
    body = row['reason'] + ('\n여러 작업이 연결된 공용 실행기가 포함됩니다.' if shared else '')
    if row['state'] == 'resolved':
        body = '이 경보의 조건이 회복 기준 시간 동안 해소됐습니다.\n이전 원인: ' + body
    else:
        body += '\n' + row.get('guidance', '')
    body += '\n관측 시각: ' + datetime.fromtimestamp(row['observed_at']).astimezone().isoformat(timespec='seconds')
    body += '\nTailscale 연결 후 원인과 영향을 확인하세요.'
    plain = body + ('\n' + url if url else '\n웹 주소 미설정: Resource Guard 웹의 경보 화면에서 확인하세요.')
    link = f'<p><a href="{html.escape(url, quote=True)}">원인과 영향 확인</a></p>' if url else ''
    rich = '<html><body><h2>' + html.escape(title) + '</h2><p>' + html.escape(body).replace('\n', '<br>') + '</p>' + link + '</body></html>'
    return title, plain, rich
