"""Bounded, owner-written incident state shared by notifications and the mobile UI."""
from __future__ import annotations
import copy
import errno
import json
import os
from pathlib import Path
import re
import secrets
from datetime import datetime

from . import atomic
from .disks import disk_limits
from .identity import boot_id, describe_target, process_identity
from .safe_read import read_text

MAX_INCIDENTS = 256
# A value hovering at its threshold resolves and re-opens within minutes. A
# recurrence before an observed quiet window continues the same incident;
# time without observations cannot confirm recovery.
RECURRENCE_MERGE_SECONDS = 1800
IDENTIFIER = re.compile(r'^[0-9a-f]{32}$')
HOST_SCOPE = 'host'
# Below this share of a drive's used space the WSL virtual disk is not called its cause.
VHD_SHARE_MINIMUM = 0.3
_LEGACY_DISK_KEY = re.compile(r'^(disk\.[A-Za-z]:):(.*)$')


def atomic_write(path: Path, data: dict):
    atomic.write_json(path, data)


class DamagedStore(ValueError):
    """The file is not a usable version-1 store."""


class UnsupportedStore(ValueError):
    """A later version wrote the file; it must not be rewritten in this format."""


_STATES = ('new', 'active', 'resolving', 'resolved', 'unknown')
MAX_DAMAGE_FILES = 5
_OPTIONAL_TIMES = ('resolved_at', 'normal_since', 'recovery_since', 'recovery_checked_at')


def _usable(row) -> bool:
    """Every field reconcile, delivery and the views read without a default."""
    number = lambda value: type(value) in (int, float)
    return (isinstance(row, dict) and IDENTIFIER.fullmatch(str(row.get('id', ''))) is not None
            and isinstance(row.get('key'), str) and bool(row['key'])
            and row.get('state') in _STATES and isinstance(row.get('severity'), str)
            and type(row.get('revision')) is int and row['revision'] >= 0
            and number(row.get('opened_at')) and number(row.get('observed_at'))
            and isinstance(row.get('reason'), str)
            and all(row.get(field) is None or number(row[field]) for field in _OPTIONAL_TIMES)
            and isinstance(row.get('transitions', []), list) and isinstance(row.get('targets', []), list))


def read_store(path: Path) -> dict:
    """Usable rows of the store; rows that are not are returned under 'damaged_rows'.

    One bad row must not stop every alert. Readers ignore damaged rows; the
    guard, the only writer, sets them aside before it rewrites the file.
    """
    try:
        text = read_text(path, max_bytes=4 * 1024 * 1024)
    except FileNotFoundError:
        return {'version': 1, 'updated_at': 0, 'incidents': []}
    except UnicodeDecodeError:
        raise DamagedStore('Invalid incident store') from None
    except OSError as exc:
        if exc.errno != errno.EFBIG:
            raise                       # unreadable for now is not the same as damaged
        raise DamagedStore('Invalid incident store') from None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise DamagedStore('Invalid incident store') from None
    if (not isinstance(value, dict) or not isinstance(value.get('incidents'), list)
            or len(value['incidents']) > MAX_INCIDENTS):
        raise DamagedStore('Invalid incident store')
    version = value.get('version')
    if version != 1:
        if type(version) is int and version > 1:
            raise UnsupportedStore('Unsupported incident store version')
        raise DamagedStore('Invalid incident store')
    damaged = [row for row in value['incidents'] if not _usable(row)]
    if damaged:
        value['incidents'] = [row for row in value['incidents'] if _usable(row)]
        value['damaged_rows'] = damaged
    return value


def set_damage_aside(path: Path, now: float, *, rows: list | None = None) -> Path:
    """Keep damaged content next to the store instead of erasing it.

    `rows` writes just those rows; without it the unreadable file itself is
    moved. Only the newest few such files are kept.
    """
    target = path.with_name(f'incidents.damaged-{int(now)}-{secrets.token_hex(3)}.json')
    if rows is None:
        os.replace(path, target)
    else:
        atomic.write_json(target, {'set_aside_at': now, 'rows': rows})
    def age(item: Path) -> float:
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0
    # By modification time: after a clock rollback the newest file has the smallest name.
    kept = sorted((item for item in path.parent.glob('incidents.damaged-*.json') if item != target), key=age)
    for old in kept[:-(MAX_DAMAGE_FILES - 1)]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def _key(condition: dict, snapshot, boot: str) -> str:
    key = condition['key'].split('.', 1)[-1]
    if key.startswith('session.'):
        root = snapshot.processes.get(int(key.split('.')[-1]))
        return 'session:' + (process_identity(root, boot) if root else 'unknown:' + key)
    if key.startswith(('disk.', 'host_')):
        # Windows keeps its state while WSL restarts; a boot-scoped key would
        # reopen the same condition and announce a false recovery.
        return key + ':' + HOST_SCOPE
    return key + ':' + boot


def _drop_recovery(row: dict) -> None:
    """Withdraw a recovery that observations of the current drive state never confirmed."""
    for field in ('recovery_pending', 'recovery_since', 'recovery_checked_at', 'recovery_paused'):
        row.pop(field, None)
    row['recovery_unconfirmed'] = True


def _migrate_disk_keys(rows: list[dict], boot: str, now: float) -> None:
    """Move boot-scoped disk keys written by older versions to the host scope.

    Each drive keeps exactly one open row: an already host-scoped row first,
    then this boot's row, then an older boot's. Further open rows are the same
    condition counted twice and are merged into that row. A resolved row never
    announces a recovery when it comes from another boot or when the drive
    still has an open row.
    """
    legacy = []
    for row in rows:
        match = _LEGACY_DISK_KEY.match(str(row.get('key', '')))
        if match and match[2] != HOST_SCOPE:
            legacy.append((row, match[1] + ':' + HOST_SCOPE, bool(boot) and match[2] == boot))
    if not legacy:
        return
    keys = {key for _, key, _ in legacy}
    holders = {row['key']: row for row in rows
               if row.get('key') in keys and row.get('state') != 'resolved'}
    merged = []
    for row, key, current in sorted(legacy, key=lambda item: not item[2]):
        row['key'] = key
        if row.get('state') == 'resolved':
            if not current:
                _drop_recovery(row)
        elif key in holders:
            holder = holders[key]
            opened = [value for value in (holder.get('opened_at'), row.get('opened_at'))
                      if type(value) in (int, float)]
            if opened:
                holder['opened_at'] = min(opened)
            merged.append(id(row))
        else:
            holders[key] = row
    for row in rows:
        if row.get('state') == 'resolved' and row.get('recovery_pending') and row.get('key') in holders:
            _drop_recovery(row)
    if merged:
        rows[:] = [row for row in rows if id(row) not in merged]


def _targets(condition: dict, snapshot) -> list[dict]:
    key = condition['key']
    if '.session.' in key:
        pids = [int(key.split('.')[-1])]
    elif key.endswith('mcp_total'):
        pids = [s.root_pid for s in sorted(snapshot.sessions, key=lambda s: s.mcp_rss_kib, reverse=True) if s.mcp_rss_kib][:8]
    elif '.host_' in key:
        pids = []  # Windows is short of memory; no WSL session is offered as the thing to end.
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
        return metrics.psi_some_avg60 is None or metrics.psi_full_avg60 is None
    if key.startswith('host_'):
        host = metrics.host if isinstance(getattr(metrics, 'host', None), dict) else {}
        if host.get('status') != 'ok' or type(host.get('observed_at')) not in (int, float):
            return True
        # A successful query can still lack this value (one WMI class failed).
        memory = _host_memory(metrics)
        known = lambda name: _plain_number(memory.get(name))
        if key.startswith('host_ram'):
            return not known('available_bytes')
        return not (known('commit_used_bytes') and known('commit_limit_bytes') and memory['commit_limit_bytes'] > 0)
    if key.startswith('disk.'):
        drive = key.split(':', 1)[0].split('.', 1)[1]
        # Windows drive IDs contain ':', so match the letter as well.
        disk = next((d for d in metrics.disks if str(d.get('id', '')).rstrip(':') == drive.rstrip(':')), None)
        return disk is None or disk.get('status') != 'ok'
    return False


def _unmonitored_disk(row: dict, metrics, settings) -> bool:
    """True when disks were collected and this incident's drive is not among them."""
    key = row['key']
    if not key.startswith('disk.') or not metrics.disks:
        return False
    if getattr(settings, 'load_warnings', None):
        # The file was not read cleanly, so the drive list may be a fallback
        # rather than the owner's choice. Leave the incident as it is.
        return False
    drive = key.split(':', 1)[0].split('.', 1)[1].rstrip(':')
    return not any(str(d.get('id', '')).rstrip(':') == drive for d in metrics.disks if isinstance(d, dict))


def _disk_row(condition: dict, metrics) -> dict | None:
    drive = condition['key'].split('.disk.', 1)[-1]
    return next((d for d in metrics.disks if isinstance(d, dict) and d.get('id') == drive), None)


def _vhd_share(disk: dict | None, metrics) -> dict | None:
    """What part of this drive the WSL virtual disk holds, and how much of it WSL no longer uses."""
    wsl = next((d for d in metrics.disks if isinstance(d, dict) and d.get('kind') == 'wsl'), None)
    vhd = (wsl or {}).get('vhd') if isinstance((wsl or {}).get('vhd'), dict) else {}
    size, drive_used = vhd.get('file_bytes'), (disk or {}).get('used_bytes')
    number = lambda value: type(value) in (int, float) and value > 0
    if not disk or vhd.get('host_drive') != disk.get('id') or not number(size) or not number(drive_used):
        return None
    # Say it only when the numbers are current and consistent, and the virtual
    # disk really is a large part of what fills the drive.
    if disk.get('status') != 'ok' or wsl.get('status') != 'ok' or vhd.get('status') != 'ok':
        return None
    if size > drive_used or size < drive_used * VHD_SHARE_MINIMUM:
        return None
    unused = size - wsl['used_bytes'] if number(wsl.get('used_bytes')) else 0
    # Same rule as the disks view: only a large, clear gap is worth a clean-up.
    reclaim = round(unused / 2**30, 1) if unused > 5 * 2**30 and unused > size * 0.2 else None
    return {'file_gib': round(size / 2**30, 1), 'share_percent': round(size / drive_used * 100),
            'reclaim_gib': reclaim}


def _disk_guidance(vhd: dict | None) -> str:
    if not vhd:
        return '디스크 여유 공간이 기준 미만입니다. 저장 공간 화면에서 사용량과 추세를 확인하세요.'
    text = f"이 드라이브 사용량의 {vhd['share_percent']}%는 WSL 가상 디스크({vhd['file_gib']:.0f} GiB)입니다."
    if vhd['reclaim_gib']:
        return (text + f" 그중 약 {vhd['reclaim_gib']:.0f} GiB는 WSL 안에서 더 이상 쓰지 않는 공간이라 정리하면 돌려받을 수 있습니다."
                " 저장 공간 화면의 'VHDX 정리 방법'을 확인하세요.")
    return text + ' WSL 안의 사용량을 줄이면 이 드라이브의 여유가 늘어납니다.'


def _plain_number(value) -> bool:
    return type(value) in (int, float) and value == value and 0 <= value < 2**63


def _host_limits_off(key: str, settings, severity: str = '') -> bool:
    """True when nothing judges this host incident any more.

    A warning-level incident ends with its warning limit; a critical one only
    when the warning limit, which would continue it, is off as well.
    """
    if key.startswith('host_ram'):
        warning, critical = settings.warning_host_available_gib, settings.critical_host_available_gib
    elif key.startswith('host_commit'):
        warning, critical = settings.warning_host_commit_percent, settings.critical_host_commit_percent
    else:
        return False
    return warning <= 0 and (critical <= 0 or severity != 'critical')


def _observation_clock(row: dict, metrics, now: float) -> float:
    """Time base for quiet periods: for host values, when Windows was last read.

    Re-reading one good value for half an hour is one observation, not thirty
    minutes of quiet.
    """
    if row['key'].startswith('host_'):
        observed = (metrics.host if isinstance(getattr(metrics, 'host', None), dict) else {}).get('observed_at')
        if _plain_number(observed) and observed <= now:
            return float(observed)
    return now


def _host_memory(metrics) -> dict:
    host = metrics.host if isinstance(getattr(metrics, 'host', None), dict) else {}
    return host.get('memory') if isinstance(host.get('memory'), dict) else {}


def _host_guidance(metrics) -> str:
    host = metrics.host if isinstance(getattr(metrics, 'host', None), dict) else {}
    vm = host.get('vmmem') if isinstance(host.get('vmmem'), dict) else {}
    text = 'Windows 호스트의 메모리가 부족합니다. WSL 안의 가용 RAM에 여유가 있어도 호스트가 먼저 느려지거나 프로그램이 종료될 수 있습니다.'
    if _plain_number(vm.get('working_set_bytes')):
        used = max(0, metrics.mem_total_kib - metrics.mem_available_kib) / 1048576
        text += (f" WSL VM이 Windows에서 {vm['working_set_bytes'] / 2**30:.0f} GiB를 차지하고 있고, "
                 f'WSL 안에서 실제로 쓰는 양은 {used:.0f} GiB입니다.')
    return text + ' 개요의 Windows 호스트 항목에서 메모리를 많이 쓰는 Windows 프로그램과 WSL VM 점유를 함께 확인하세요.'


def _host_evidence(metrics) -> dict:
    """Windows values behind a host incident; absent ones are left out, not zeroed."""
    host = metrics.host if isinstance(getattr(metrics, 'host', None), dict) else {}
    memory = _host_memory(metrics)
    vm = host.get('vmmem') if isinstance(host.get('vmmem'), dict) else {}
    out = {}
    if _plain_number(memory.get('available_bytes')):
        out['available_gib'] = round(memory['available_bytes'] / 2**30, 2)
    if (_plain_number(memory.get('commit_used_bytes')) and _plain_number(memory.get('commit_limit_bytes'))
            and memory['commit_limit_bytes'] > 0):
        out['commit_percent'] = round(memory['commit_used_bytes'] / memory['commit_limit_bytes'] * 100, 1)
    if _plain_number(vm.get('working_set_bytes')):
        out['vmmem_gib'] = round(vm['working_set_bytes'] / 2**30, 2)
    if _plain_number(host.get('observed_at')):
        out['observed_at'] = host['observed_at']
    out['current'] = host.get('status') == 'ok'
    return out


def _measurement(condition, snapshot, metrics, settings):
    key = condition['key']
    critical = condition['severity'] == 'critical'
    memory = _host_memory(metrics)
    host = metrics.host if isinstance(getattr(metrics, 'host', None), dict) else {}
    not_current = '' if host.get('status') == 'ok' else ' · 마지막 확인값'
    if key.endswith('host_ram') and _plain_number(memory.get('available_bytes')):
        return {'value': round(memory['available_bytes'] / 2**30, 2), 'unit': 'GiB',
                'label': 'Windows 가용 RAM · 기준 이하' + not_current,
                'threshold': settings.critical_host_available_gib if critical else settings.warning_host_available_gib}
    if (key.endswith('host_commit') and _plain_number(memory.get('commit_used_bytes'))
            and _plain_number(memory.get('commit_limit_bytes')) and memory['commit_limit_bytes'] > 0):
        return {'value': round(memory['commit_used_bytes'] / memory['commit_limit_bytes'] * 100, 1), 'unit': '%',
                'label': 'Windows 커밋 사용률 · 기준 이상' + not_current,
                'threshold': settings.critical_host_commit_percent if critical else settings.warning_host_commit_percent}
    if '.disk.' in key:
        disk = _disk_row(condition, metrics)
        percent = (disk or {}).get('available_percent')
        if type(percent) not in (int, float):
            return {}
        warning, critical, warning_gib, critical_gib = disk_limits(settings, disk['id'])
        level = condition['severity'] == 'critical'
        floor = critical_gib if level else warning_gib
        label = '디스크 여유 · 기준 미만'
        if floor > 0:
            label += f' (여유 {floor:g} GiB 미만일 때만 경보)'
        if disk.get('status') != 'ok':
            label += ' · 마지막 확인값'
        return {'value': round(percent, 1), 'threshold': float(critical if level else warning), 'unit': '%', 'label': label}
    if key.endswith('mcp_total'):
        return {'value': round(snapshot.mcp_rss_kib / 1048576, 2), 'threshold': settings.mcp_warning_rss_gib, 'unit': 'GiB', 'label': 'MCP RSS 합계'}
    if '.session.' in key:
        session = next((s for s in snapshot.sessions if s.root_pid == int(key.split('.')[-1])), None)
        return {'value': round(session.rss_kib / 1048576, 2) if session else None, 'threshold': settings.session_warning_rss_gib, 'unit': 'GiB', 'label': '실행 트리 RSS'}
    if key.endswith('available_ram'):
        limit = settings.critical_available_gib if condition['severity'] == 'critical' else settings.warning_available_gib
        return {'value': round(metrics.mem_available_gib, 2), 'threshold': limit, 'unit': 'GiB', 'label': '가용 RAM · 기준 미만'}
    return {}


def reconcile(previous: dict, conditions: list[dict], snapshot, metrics, settings, *, boot=None,
              pending: list[dict] | None = None) -> dict:
    """`pending` holds re-exceeded conditions whose sustain period is not yet met.

    They keep an existing incident open, matching the guard which also holds its
    severity, but never open one or change its revision.
    """
    now = metrics.timestamp
    boot = boot_id() if boot is None else boot
    rows = copy.deepcopy(previous.get('incidents', []))
    _migrate_disk_keys(rows, boot, now)
    active = {r['key']: r for r in rows if r.get('state') != 'resolved'}
    # Until recovery is confirmed by observations, a recurrence continues the
    # same incident even when a collection gap outlasted the wall-clock window.
    recent = {}
    for r in sorted(rows, key=lambda r: r.get('resolved_at', 0)):
        if r.get('state') == 'resolved' and r.get('recovery_pending'):
            recent[r['key']] = r
    pending_keys = {_key(c, snapshot, boot) for c in pending or []}
    seen = set()
    pressure = (metrics.mem_available_gib < settings.warning_available_gib
                or (metrics.psi_some_avg60 is not None and metrics.psi_some_avg60 >= settings.warning_psi_some_avg60)
                or any(c['key'].endswith('oom_kill') for c in conditions))
    for condition in conditions:
        key = _key(condition, snapshot, boot)
        seen.add(key)
        row = active.get(key)
        recurred = False
        if row is None and key in recent:
            # Same cause again before the recovery was announced: continue the
            # incident. Its revision (and any defer bound to it) is unchanged.
            row = active[key] = recent.pop(key)
            recurred = True
            row.pop('resolved_at', None)
            row.pop('recovery_pending', None)
            for field in ('recovery_since', 'recovery_checked_at', 'recovery_paused'):
                row.pop(field, None)
            row['recurrences'] = row.get('recurrences', 0) + 1
            row['transitions'] = (row.get('transitions', []) + [{'at': now, 'state': 'active', 'recurred': True,
                                                                  'severity': condition['severity']}])[-16:]
        if row is None:
            row = {'id': secrets.token_hex(16), 'key': key, 'opened_at': now, 'revision': 0,
                   'state': 'new', 'severity': '', 'transitions': [], 'initial_reason': condition['reason']}
            rows.append(row)
        targets = _targets(condition, snapshot)
        # The top-session target list is refreshed in place; only a new or
        # escalated incident is a new revision that may re-alert or end a defer.
        severity_changed = row['severity'] != condition['severity']
        ranks = {'warning': 1, 'critical': 2}
        new_revision = (row['revision'] == 0
                        or ranks.get(condition['severity'], 0) > ranks.get(row['severity'], 0))
        if new_revision:
            row['revision'] += 1
        if not recurred and (new_revision or severity_changed or row['state'] == 'unknown'):
            row['transitions'] = (row.get('transitions', []) + [{'at': now, 'state': 'active',
                                    'severity': condition['severity']}])[-16:]
        measurement = _measurement(condition, snapshot, metrics, settings)
        # Only a row opened now has a first observation; an older incident that
        # gains a measurement later must not present today's value as its start.
        if measurement and row['state'] == 'new':
            row['initial_value'] = measurement.get('value')
        if measurement.get('value') is not None and row.get('initial_value') is not None:
            measurement['change_since_open'] = round(measurement['value'] - row['initial_value'], 2)
        benign_memory = (not pressure and metrics.psi_some_avg60 is not None
                         and (condition['key'].endswith('mcp_total') or '.session.' in condition['key']))
        on_disk = '.disk.' in condition['key']
        on_host = '.host_' in condition['key']
        vhd = _vhd_share(_disk_row(condition, metrics), metrics) if on_disk else None
        row.update(state='active', severity=condition['severity'], reason=condition['reason'],
                   observed_at=now, normal_since=None, targets=targets,
                   measurement=measurement,
                   priority='pressure' if pressure or on_host or condition['severity'] == 'critical' else 'review',
                   evidence={'available_gib': round(metrics.mem_available_gib, 2),
                             'psi_some_avg60': metrics.psi_some_avg60,
                             'swap_out_mib_per_minute': metrics.swap_out_mib_per_minute,
                             **({'vhd': vhd} if vhd else {}),
                             **({'host': _host_evidence(metrics)} if on_host else {})},
                   stale=_unobserved({'key': key}, metrics),
                   guidance=(_disk_guidance(vhd) if on_disk else _host_guidance(metrics) if on_host else '현재 측정에서는 가용 RAM과 PSI가 압박 기준 안에 있습니다. 작업을 사용 중이면 유지하며 추이를 확인하세요.' if benign_memory else
                             '자원 압박이 관측됐습니다. 저장할 작업과 주요 점유 대상을 확인하세요.' if pressure else
                             '자원 기준을 넘었습니다. 점유량만으로 작업 중단이나 누수를 단정하지 않습니다.'))
    for condition in pending or []:
        key = _key(condition, snapshot, boot)
        row = active.get(key)
        if key in seen or row is None or row['state'] not in ('active', 'resolving'):
            continue
        seen.add(key)
        row.update(state='active', normal_since=None, observed_at=now, stale=_unobserved(row, metrics))
    for row in rows:
        unjudged = (_unmonitored_disk(row, metrics, settings)
                    or _host_limits_off(row['key'], settings, row.get('severity', '')))
        if row['state'] == 'resolved' and row.get('recovery_pending'):
            checked = row.get('recovery_checked_at')
            continuous = (type(checked) in (int, float)
                          and 0 <= now - checked <= max(1, 2 * settings.interval_seconds))
            row['recovery_checked_at'] = now
            if unjudged:
                _drop_recovery(row)  # Nothing will ever observe the quiet window.
                row['transitions'] = (row.get('transitions', []) + [
                    {'at': now, 'state': 'resolved', 'unmonitored': True}])[-16:]
                continue
            if _unobserved(row, metrics) or row['key'] in pending_keys:
                row.update(recovery_since=None, recovery_paused=True)
                continue
            clock = _observation_clock(row, metrics, now)
            since = row.get('recovery_since')
            if not continuous or type(since) not in (int, float) or not 0 <= since <= clock:
                since = clock  # Includes old pending records and clock rollback.
            row.update(recovery_since=since, recovery_paused=False)
            if clock - since >= RECURRENCE_MERGE_SECONDS:
                row.pop('recovery_pending', None)
                row.update(revision=row['revision'] + 1, recovery_confirmed_at=now)
            continue
        if row['key'] in seen or row['state'] == 'resolved':
            continue
        if unjudged:
            # Removed from disk_drives, or its limit switched off: nothing
            # judges it any more, so it can neither stay open forever nor be
            # announced as recovered.
            row.update(state='resolved', resolved_at=now, recovery_unconfirmed=True, normal_since=None)
            row['transitions'] = (row.get('transitions', []) + [
                {'at': now, 'state': 'resolved', 'unmonitored': True}])[-16:]
            continue
        gap = now < row.get('observed_at', now) or now - row.get('observed_at', now) > max(60, 3 * settings.interval_seconds)
        if gap or _unobserved(row, metrics):
            row.update(state='unknown', normal_since=None, observed_at=now)
            continue
        clock = _observation_clock(row, metrics, now)
        since = row.get('normal_since')
        since = clock if type(since) not in (int, float) or since > clock else since
        row.update(state='resolving', normal_since=since, observed_at=now)
        if clock - since >= settings.recovery_sustain_seconds:
            row.update(state='resolved', resolved_at=now, recovery_pending=True,
                       recovery_since=clock, recovery_checked_at=now, recovery_paused=False)
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


def _time_text(stamp) -> str:
    """Local ISO time, or '' for a value that is not a usable timestamp."""
    if not _plain_number(stamp):
        return ''
    try:
        return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec='seconds')
    except (OverflowError, OSError, ValueError):
        return ''


def incident_message(row: dict, origin: str) -> tuple[str, str, str]:
    import html
    from urllib.parse import urlsplit
    parsed = urlsplit(origin)
    valid_origin = (parsed.scheme == 'https' and parsed.hostname and not parsed.username
                    and not parsed.password and not parsed.query and not parsed.fragment
                    and parsed.path in ('', '/'))
    url = origin.rstrip('/') + '/#incident?id=' + row['id'] if valid_origin else ''
    # Closed without an observed recovery: never worded as recovered.
    unconfirmed = row['state'] == 'resolved' and bool(row.get('recovery_unconfirmed'))
    closed = '감시 종료' if unconfirmed else '회복 확인' if row['state'] == 'resolved' else ''
    label = closed or ('자원 압박' if row.get('priority') == 'pressure' else '점유 상태 확인')
    names = list(dict.fromkeys(t.get('provider', '') for t in row.get('targets', [])))
    if str(row.get('key', '')).startswith('host_'):
        label = closed or 'Windows 메모리 부족'
        names = ['호스트']
    if str(row.get('key', '')).startswith('disk.'):
        # A drive is not an agent session: say what it is and which one.
        label = closed or '디스크 여유 부족'
        names = [row['key'].split(':', 1)[0].split('.', 1)[1] + ':']
    title = f"{label} · {' / '.join(names) or 'WSL'}"
    shared = any(t.get('kind') == 'shared_runtime' for t in row.get('targets', []))
    body = row['reason'] + ('\n여러 작업이 연결된 공용 실행기가 포함됩니다.' if shared else '')
    if unconfirmed:
        body = '회복을 관측으로 확인하지 못한 채 이 경보의 감시를 끝냈습니다.\n이전 원인: ' + body
    elif row['state'] == 'resolved':
        body = '이 경보의 조건이 회복 기준 시간 동안 해소됐습니다.\n이전 원인: ' + body
    else:
        body += '\n' + row.get('guidance', '')
    observed = row.get('recovery_confirmed_at', row['observed_at']) if row['state'] == 'resolved' else row['observed_at']
    body += '\n관측 시각: ' + (_time_text(observed) or '확인 불가')
    host_evidence = (row.get('evidence') or {}).get('host') if isinstance(row.get('evidence'), dict) else None
    if row['state'] != 'resolved' and isinstance(host_evidence, dict) and not host_evidence.get('current'):
        read_at = _time_text(host_evidence.get('observed_at'))
        if read_at:
            body += '\nWindows 값 확인 시각: ' + read_at + ' (이후 Windows를 조회하지 못했습니다)'
    body += '\nTailscale 연결 후 원인과 영향을 확인하세요.'
    plain = body + ('\n' + url if url else '\n웹 주소 미설정: Resource Guard 웹의 경보 화면에서 확인하세요.')
    link = f'<p><a href="{html.escape(url, quote=True)}">원인과 영향 확인</a></p>' if url else ''
    rich = '<html><body><h2>' + html.escape(title) + '</h2><p>' + html.escape(body).replace('\n', '<br>') + '</p>' + link + '</body></html>'
    return title, plain, rich
