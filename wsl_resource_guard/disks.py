"""Cheap filesystem snapshots: no directory walks or disk cleanup operations."""
from __future__ import annotations

import copy
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import threading
import time
from .safe_read import read_text

_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_LOCK = threading.Lock()


def mount_table(path: Path = Path('/proc/self/mountinfo')) -> dict[str, tuple[str, str]]:
    def decode(value: str) -> str:
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    mounts = {}
    for line in path.read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields, fs = left.split(), right.split()
        mounts[decode(fields[4])] = (fs[0], decode(fs[1]))
    return mounts


def disk_severity(disk: dict, warning: float = 20, critical: float = 10) -> str:
    total, available = disk.get('total_bytes'), disk.get('available_bytes')
    if not isinstance(total, (int, float)) or not isinstance(available, (int, float)) or total <= 0:
        return 'unknown'
    # Compare unrounded values. Exactly 20% is normal; exactly 10% is warning.
    if available * 100 < total * critical:
        return 'critical'
    if available * 100 < total * warning:
        return 'warning'
    return 'normal'


def collect_disks(drives: list[str], vhd_path: str = '', previous: list[dict] | None = None) -> list[dict]:
    previous_by_id = {d['id']: d for d in (previous or []) if isinstance(d, dict) and 'id' in d}
    now = time.time()
    try:
        mounts = mount_table()
        mount_error = ''
    except (OSError, ValueError, IndexError):
        mounts, mount_error = {}, '마운트 정보를 읽지 못했습니다.'
    targets = [(str(letter).upper() + ':', 'windows', '/mnt/' + str(letter).lower()) for letter in drives]
    targets.append(('wsl', 'wsl', '/'))
    rows = []
    for identifier, kind, path in targets:
        row = {'id': identifier, 'name': 'WSL · Ubuntu' if kind == 'wsl' else identifier,
               'kind': kind, 'mount': path, 'checked_at': now, 'observed_at': None,
               'status': 'ok', 'error': '', 'total_bytes': None, 'used_bytes': None,
               'available_bytes': None, 'reserved_bytes': None,
               'available_percent': None, 'used_percent': None}
        try:
            if kind == 'windows':
                if not re.fullmatch('[A-Z]:', identifier):
                    raise ValueError('잘못된 드라이브 설정입니다.')
                if mount_error:
                    raise OSError(mount_error)
                fs_type, source = mounts.get(path, ('', ''))
                if fs_type not in ('9p', 'drvfs') or not source.upper().startswith(identifier):
                    row['status'] = 'unmounted'
                    raise OSError('드라이브가 연결 또는 마운트되어 있지 않습니다.')
            stats = os.statvfs(path)
            total = stats.f_blocks * stats.f_frsize
            free = stats.f_bfree * stats.f_frsize
            available = stats.f_bavail * stats.f_frsize
            if total <= 0 or not 0 <= available <= free <= total:
                raise ValueError('올바른 용량 정보를 받지 못했습니다.')
            used = total - free
            row.update(observed_at=now, total_bytes=total, used_bytes=used, available_bytes=available,
                       reserved_bytes=free-available, available_percent=available/total*100,
                       used_percent=used/total*100)
        except (OSError, ValueError) as exc:
            row['status'] = 'unmounted' if row['status'] == 'unmounted' else 'error'
            row['error'] = str(exc)
            old = previous_by_id.get(identifier, {})
            # Keep last good measurements for alarm continuity, explicitly marked stale.
            for key in ('observed_at', 'total_bytes', 'used_bytes', 'available_bytes',
                        'reserved_bytes', 'available_percent', 'used_percent'):
                row[key] = old.get(key)
        if kind == 'wsl':
            row['vhd'] = {'path': vhd_path, 'file_bytes': None, 'host_drive': None,
                          'status': 'not-configured', 'error': ''}
            if vhd_path:
                match = re.match(r'^/mnt/([a-zA-Z])/', vhd_path)
                row['vhd']['host_drive'] = match[1].upper() + ':' if match else None
                try:
                    if match and '/mnt/' + match[1].lower() not in mounts:
                        raise OSError('VHDX 저장 드라이브가 마운트되어 있지 않습니다.')
                    row['vhd']['file_bytes'] = Path(vhd_path).stat().st_size
                    row['vhd']['status'] = 'ok'
                except OSError as exc:
                    row['vhd'].update(status='error', error=str(exc))
        rows.append(row)
    return rows


def read_disks(drives: list[str], vhd_path: str = '', previous: list[dict] | None = None,
               interval: float = 60, force: bool = False) -> list[dict]:
    key = (tuple(drives), vhd_path)
    with _LOCK:
        cached = _CACHE.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < (5 if force else interval):
            return copy.deepcopy(cached[1])
        rows = collect_disks(drives, vhd_path, cached[1] if cached else previous)
        _CACHE[key] = (now, rows)
        return copy.deepcopy(rows)


def disk_summary_lines(disks: list[dict]) -> list[str]:
    lines = []
    for disk in disks:
        if disk.get('total_bytes') is None:
            lines.append(f"{disk['name']} · {disk.get('error') or '조회 불가'}")
            continue
        used = disk['used_bytes'] / 2**30
        total = disk['total_bytes'] / 2**30
        free = disk['available_bytes'] / 2**30
        stale = ' · 마지막 확인값 / 현재 조회 불가' if disk.get('status') != 'ok' else ''
        lines.append(f"{disk['name']} · {used:.1f}/{total:.1f} GiB 사용 · 여유 {free:.1f} GiB ({disk['available_percent']:.1f}%){stale}")
    return lines


def _last_lines(path: Path, count: int, max_bytes: int = 65536) -> list[str]:
    try:
        data = read_text(path, max_bytes=max_bytes, errors='replace', tail=True)
    except OSError:
        return []
    lines = data.splitlines()
    return lines[-count:]


def daily_disk_records(state_path: Path, days: int) -> list[dict]:
    """Last usable record of each retained disk-history file, oldest first."""
    records = []
    for path in sorted(state_path.glob('disk-history-*.jsonl'))[-max(days, 1):]:
        for line in reversed(_last_lines(path, 2)):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and isinstance(record.get('disks'), list):
                record['date'] = path.stem.removeprefix('disk-history-')
                records.append(record)
                break
    return records


def recent_disk_records(state_path: Path, hours: float = 6) -> list[dict]:
    """Hourly disk records from the last `hours`, oldest first."""
    cutoff = time.time() - hours * 3600
    cutoff_day = datetime.fromtimestamp(cutoff).date()
    records = []
    for path in sorted(state_path.glob('disk-history-*.jsonl')):
        try:
            day = datetime.strptime(path.stem.removeprefix('disk-history-'),
                                    '%Y-%m-%d').date()
        except ValueError:
            continue
        if day < cutoff_day:
            continue
        try:
            lines = read_text(path, max_bytes=16 * 1024 * 1024, errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (isinstance(record, dict) and isinstance(record.get('timestamp'), (int, float))
                    and record['timestamp'] >= cutoff
                    and isinstance(record.get('disks'), list)):
                records.append(record)
    records.sort(key=lambda r: r['timestamp'])
    return records


# Daily trends look back this many day-to-day steps.
TREND_DAYS = 7
# Below this burn rate, or beyond this horizon, a forecast is noise
# ("경고까지 약 454931일"), so none is reported.
MIN_FORECAST_GIB_PER_DAY = 0.05
MAX_FORECAST_DAYS = 365


def median_rate(points: list[tuple[float, float]], unit_seconds: float,
                min_gap: float = 0.0) -> float | None:
    """Median consumption per `unit_seconds` across consecutive samples.

    points are (timestamp, available_bytes) in time order; a positive result
    means free space is shrinking. Samples closer than min_gap to the last
    kept sample are skipped so a fresh reading taken minutes after the last
    hourly record cannot produce an extreme per-hour step.
    """
    kept: list[tuple[float, float]] = []
    for ts, value in points:
        if kept and ts - kept[-1][0] < max(min_gap, 1e-9):
            continue
        kept.append((ts, value))
    steps = sorted((a[1] - b[1]) / ((b[0] - a[0]) / unit_seconds)
                   for a, b in zip(kept, kept[1:]))
    if not steps:
        return None
    middle = len(steps) // 2
    return steps[middle] if len(steps) % 2 else (steps[middle - 1] + steps[middle]) / 2


def disk_insights(disks: list[dict], history: list[dict],
                  warning_pct: float, critical_pct: float,
                  recent: list[dict] | None = None) -> dict[str, dict]:
    """Per-drive trend and reclaim hints from one record per day.

    history records carry a 'date' (YYYY-MM-DD) and a 'disks' list, e.g. from
    daily_disk_records(). Rates are the median of consecutive per-day changes
    over the last week: steady consumption shows in every step, while a single
    large copy is one step and cannot pose as a lasting burn rate.

    `recent` holds hourly records (recent_disk_records output, optionally with
    the current measurement appended) for a short-term burn forecast: it is
    reported only when consumption is >= 0.25 GiB/h over >= 2 h of coverage,
    and only for monitored Windows drives.
    """
    hourly: dict[str, list[tuple[float, float]]] = {}
    for record in recent or []:
        ts = record.get('timestamp')
        for row in record.get('disks', []):
            available = row.get('available_bytes')
            if (isinstance(ts, (int, float)) and isinstance(row.get('id'), str)
                    and isinstance(available, (int, float))):
                hourly.setdefault(row['id'], []).append((ts, available))
    daily: dict[str, list[tuple[str, float]]] = {}
    for record in history:
        day = record.get('date')
        for row in record.get('disks', []):
            available = row.get('available_bytes')
            if day and isinstance(row.get('id'), str) and isinstance(available, (int, float)):
                daily.setdefault(row['id'], []).append((day, available))
    insights: dict[str, dict] = {}
    for disk in disks:
        info: dict[str, float] = {}
        series = sorted(daily.get(disk['id'], []))
        if len(series) >= 2:
            info['daily_delta_gib'] = (series[-1][1] - series[-2][1]) / 2**30
            try:
                points = [(date.fromisoformat(day).toordinal() * 86400.0, value)
                          for day, value in series[-(TREND_DAYS + 1):]]
            except ValueError:
                points = []
            rate = median_rate(points, 86400)
            if rate is not None:
                info['gib_per_day'] = rate / 2**30
                total = disk.get('total_bytes')
                # Forecasts only make sense for alert-eligible Windows drives (F10).
                if (disk.get('kind') == 'windows' and rate >= MIN_FORECAST_GIB_PER_DAY * 2**30
                        and isinstance(total, (int, float)) and total > 0):
                    available = series[-1][1]
                    for label, pct in (('days_to_warning', warning_pct),
                                       ('days_to_critical', critical_pct)):
                        floor = total * pct / 100
                        days = (available - floor) / rate
                        if available > floor and days <= MAX_FORECAST_DAYS:
                            info[label] = days
        series_h = sorted(hourly.get(disk['id'], []))
        if disk.get('kind') == 'windows' and len(series_h) >= 2:
            span_seconds = series_h[-1][0] - series_h[0][0]
            rate = median_rate(series_h, 3600, min_gap=1800) if span_seconds >= 7200 else None
            if rate is not None:
                total = disk.get('total_bytes')
                if rate >= 0.25 * 2**30 and isinstance(total, (int, float)) and total > 0:
                    info['recent_gib_per_hour'] = rate / 2**30
                    available = series_h[-1][1]
                    for label, pct in (('hours_to_warning', warning_pct),
                                       ('hours_to_critical', critical_pct)):
                        floor = total * pct / 100
                        if available > floor:
                            info[label] = (available - floor) / rate
        vhd = disk.get('vhd') or {}
        file_bytes, used_bytes = vhd.get('file_bytes'), disk.get('used_bytes')
        if isinstance(file_bytes, (int, float)) and isinstance(used_bytes, (int, float)):
            reclaim = file_bytes - used_bytes
            if reclaim > 5 * 2**30 and reclaim > file_bytes * 0.2:
                info['vhd_reclaim_gib'] = reclaim / 2**30
        if info:
            insights[disk['id']] = info
    return insights
