"""Resource-history reads, downsampling, and alert-episode extraction.

The daemon appends one JSON object per line to history-YYYY-MM-DD.jsonl files
under the state directory. Readers never write; the daemon is the only writer.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import threading
import time

from .metrics import psi_status
from .safe_read import read_text

# range key -> (lookback seconds, bucket seconds). A 0 bucket means raw samples.
HISTORY_RANGES = {
    '3h': (3 * 3600, 0),
    '24h': (24 * 3600, 300),
    '7d': (7 * 86400, 1800),
    '14d': (14 * 86400, 3600),
}
# The fields downsample() emits, so raw (3h) and bucketed (24h/7d) answers
# share one schema; the dashboard history view needs nothing more.
HISTORY_FIELDS = ('timestamp', 'severity', 'reasons', 'observations', 'metrics')
SEVERITY_RANK = {'normal': 0, 'warning': 1, 'critical': 2}

# Downsampling keeps worst-case values so spikes survive aggregation: lowest
# available memory, highest swap footprint and pressure, the worst severity.
_MIN_KEYS = ('mem_available_kib', 'swap_free_kib')
_MAX_KEYS = ('swap_total_kib', 'psi_some_avg60', 'psi_full_avg60',
             'swap_in_mib_per_minute', 'swap_out_mib_per_minute')
# vmmem is an external observation: keep the newest pair, never a maximum.
_LATEST_KEYS = ('vmmem_bytes', 'vmmem_observed_at')


def _file_date(path: Path, prefix: str) -> object:
    try:
        return datetime.strptime(path.stem.removeprefix(prefix), '%Y-%m-%d').date()
    except ValueError:
        return None


def read_history(state_path: Path, since: float,
                 fields: tuple = HISTORY_FIELDS) -> list[dict]:
    """Oldest-first history records with timestamp >= since."""
    records: list[dict] = []
    since_day = datetime.fromtimestamp(since).date() if since else None
    for path in sorted(state_path.glob('history-*.jsonl')):
        day = _file_date(path, 'history-')
        if day is None or (since_day is not None and day < since_day):
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
            if not isinstance(record, dict):
                continue
            ts = record.get('timestamp')
            if isinstance(ts, (int, float)) and ts >= since:
                records.append({k: record.get(k) for k in fields})
    records.sort(key=lambda r: r['timestamp'])
    return records


def downsample(records: list[dict], bucket_seconds: int) -> list[dict]:
    """Aggregate records into time buckets preserving worst-case values."""
    if bucket_seconds <= 0:
        return list(records)
    buckets: dict[int, dict] = {}
    for record in records:
        ts = record.get('timestamp')
        metrics = record.get('metrics')
        if not isinstance(ts, (int, float)) or not isinstance(metrics, dict):
            continue
        key = int(ts // bucket_seconds)
        acc = buckets.get(key)
        if acc is None:
            acc = {'timestamp': ts, 'severity': record.get('severity'),
                   'reasons': list(record.get('reasons') or []),
                   'observations': list(record.get('observations') or []),
                   'metrics': {**metrics, 'psi_status': psi_status(metrics)},
                   '_psi_statuses': {psi_status(metrics)}}
            buckets[key] = acc
            continue
        acc['timestamp'] = ts
        if SEVERITY_RANK.get(record.get('severity'), 1) > SEVERITY_RANK.get(acc['severity'], -1):
            acc['severity'] = record.get('severity')
            acc['reasons'] = list(record.get('reasons') or [])
            acc['observations'] = list(record.get('observations') or [])
        acc['_psi_statuses'].add(psi_status(metrics))
        merged = acc['metrics']
        for field in _MIN_KEYS:
            value = metrics.get(field)
            if value is not None and (merged.get(field) is None or value < merged[field]):
                merged[field] = value
        for field in _MAX_KEYS:
            value = metrics.get(field)
            if value is not None and (merged.get(field) is None or value > merged[field]):
                merged[field] = value
        for field in _LATEST_KEYS:
            value = metrics.get(field)
            if value is not None:
                merged[field] = value
    for acc in buckets.values():
        statuses = acc.pop('_psi_statuses')
        has_value = any(acc['metrics'].get(k) is not None
                        for k in ('psi_some_avg60', 'psi_full_avg60'))
        if len(statuses) == 1:
            status = next(iter(statuses))
        elif has_value:
            status = 'partial'
        else:
            status = 'error' if 'error' in statuses else 'missing'
        acc['metrics']['psi_status'] = status
    return [buckets[key] for key in sorted(buckets)]


def downsample_service_memory(records: list[dict], bucket_seconds: int = 300) -> list[dict]:
    """Per-bucket maximum per service; absent services stay absent (gaps)."""
    buckets: dict[int, dict] = {}
    for record in records:
        ts = record.get('timestamp')
        services = record.get('services')
        if not isinstance(ts, (int, float)) or not isinstance(services, dict):
            continue
        acc = buckets.setdefault(int(ts // bucket_seconds),
                                 {'timestamp': ts, 'services': {}})
        acc['timestamp'] = ts
        for name, value in services.items():
            if isinstance(value, (int, float)):
                acc['services'][name] = max(acc['services'].get(name, 0), value)
    return [buckets[key] for key in sorted(buckets)]


_PARENS = re.compile(r'\([^)]*\)')
# Numbers with an optional unit; bare counts are dropped too so slightly
# different samples collapse into one cause label.
_NUMBER = re.compile(
    r'\s*\d+(?:[.,]\d+)?\s*(?:MiB/min|MiB/분|GiB|MiB|KiB|TiB|%|건|개|초|분|시간|일)?')
_DANGLING_COMMA = re.compile(r'\s+,')
_TRAILING_JOSA = re.compile(
    r'\s+(?:에서|으로|로|에게|한테|부터|까지|처럼|보다|이|가|을|를|은|는|의|에|와|과|도|만)$')
_TRAILING_SEP = re.compile(r'[\s,;:.·\-—/]+$')


def _drop_number(match: re.Match) -> str:
    """Erase a number token. A glued-on particle (GiB에서) must stay attached
    to the preceding word, so no replacement space is emitted then."""
    end = match.end()
    if end < len(match.string) and '가' <= match.string[end] <= '힣':
        return ''
    return ' '


def reason_label(text: object) -> str:
    """Normalize an alert reason into a stable cause label.

    Drops parenthesized details and number+unit tokens so 'E: 디스크 여유
    18.7% (…)' and '… 18.8% …' both become 'E: 디스크 여유'.
    """
    label = _PARENS.sub(' ', str(text))
    label = _NUMBER.sub(_drop_number, label)
    label = _DANGLING_COMMA.sub(',', label)
    label = ' '.join(label.split())
    label = _TRAILING_JOSA.sub('', label)
    label = _TRAILING_SEP.sub('', label)
    return label or str(text).strip()


def alert_episodes(records: list[dict], gap_seconds: int = 1800,
                   now: float | None = None) -> list[dict]:
    """Group consecutive non-normal samples into alert episodes.

    An episode closes when severity returns to 'normal' or the gap between
    samples exceeds gap_seconds — a daemon outage must not silently extend an
    episode. For the same reason a still-open trailing episode is reported
    ongoing=False once its end is older than now - gap_seconds; `now` defaults
    to the current time. Episodes are {start, end, ongoing, duration_seconds,
    worst_severity, reasons_top5, reason_summary}. reasons_top5 lists the
    normalized reason_label() values by frequency; reason_summary carries
    {label, samples, last} per label, where samples counts raw reason
    occurrences and last is the most recent raw text.
    """
    episodes: list[dict] = []
    current: dict | None = None
    previous_ts: float | None = None
    for record in records:
        ts = record.get('timestamp')
        if not isinstance(ts, (int, float)):
            continue
        severity = record.get('severity') or 'unknown'
        if (current is not None and previous_ts is not None
                and ts - previous_ts > gap_seconds):
            current['end'] = previous_ts
            current['ongoing'] = False
            episodes.append(current)
            current = None
        if severity == 'normal':
            if current is not None:
                # The episode ends at the last non-normal sample, not at the
                # recovery timestamp.
                current['end'] = previous_ts
                current['ongoing'] = False
                episodes.append(current)
                current = None
            previous_ts = ts
            continue
        previous_ts = ts
        if current is None:
            current = {'start': ts, 'end': ts, 'ongoing': True,
                       'worst_severity': severity, '_labels': {}, '_last': {}}
        current['end'] = ts
        if SEVERITY_RANK.get(severity, 1) > SEVERITY_RANK.get(current['worst_severity'], 0):
            current['worst_severity'] = severity
        for reason in record.get('reasons') or []:
            label = reason_label(reason)
            current['_labels'][label] = current['_labels'].get(label, 0) + 1
            current['_last'][label] = reason
    if current is not None:
        # The trailing episode is ongoing only while fresh samples arrive;
        # otherwise the daemon has been off longer than the gap allows.
        if now is None:
            now = time.time()
        if current['end'] < now - gap_seconds:
            current['ongoing'] = False
        episodes.append(current)
    for episode in episodes:
        episode['duration_seconds'] = max(0, episode['end'] - episode['start'])
        labels = episode.pop('_labels')
        last = episode.pop('_last')
        summary = [{'label': label, 'samples': count, 'last': last[label]}
                   for label, count in sorted(
                       labels.items(), key=lambda item: (-item[1], item[0]))]
        episode['reason_summary'] = summary
        episode['reasons_top5'] = [entry['label'] for entry in summary[:5]]
    return episodes


def reason_summary_7d(episodes: list[dict], now: float | None = None) -> list[dict]:
    """7-day cause rollup for the alerts view.

    Episodes that started within the last 7 days count once toward every label
    in their reason_summary; each counted label also gains the episode's full
    duration. Sorted by episode count, then total seconds, then label.
    """
    cutoff = (now if now is not None else time.time()) - 7 * 86400
    counts: dict[str, int] = {}
    seconds: dict[str, float] = {}
    for episode in episodes:
        start = episode.get('start')
        if not isinstance(start, (int, float)) or start < cutoff:
            continue
        duration = episode.get('duration_seconds') or 0
        for entry in episode.get('reason_summary') or []:
            label = entry.get('label')
            if not label:
                continue
            counts[label] = counts.get(label, 0) + 1
            seconds[label] = seconds.get(label, 0) + duration
    return [{'label': label, 'episodes': count, 'total_seconds': seconds[label]}
            for label, count in sorted(
                counts.items(), key=lambda item: (-item[1], -seconds[item[0]], item[0]))]


# --- Per-file caches. Past days' files never change again, so their parsed
# aggregates are kept by (path, mtime, size); only today's file is re-read.
_file_cache: dict[tuple, object] = {}
_file_cache_lock = threading.Lock()


def _cached(path: Path, kind: str, build):
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), kind)
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _file_cache_lock:
        hit = _file_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    value = build(path)
    with _file_cache_lock:
        _file_cache[key] = (stamp, value)
        # Forget files that rotated away.
        for stale in [k for k in _file_cache if not Path(k[0]).exists()]:
            _file_cache.pop(stale, None)
    return value


def _history_files(state_path: Path, since: float) -> list[Path]:
    since_day = datetime.fromtimestamp(since).date() if since else None
    return [path for path in sorted(state_path.glob('history-*.jsonl'))
            if (day := _file_date(path, 'history-')) is not None
            and (since_day is None or day >= since_day)]


def _iter_records(path: Path):
    try:
        lines = read_text(path, max_bytes=16 * 1024 * 1024, errors='replace').splitlines()
    except OSError:
        return
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get('timestamp'), (int, float)):
            yield record


def read_history_downsampled(state_path: Path, since: float, bucket_seconds: int) -> list[dict]:
    """downsample(read_history(...)) with each day's buckets cached.

    Buckets are epoch-aligned and divide an hour, and local midnight falls on
    an hour boundary, so a bucket never spans two daily files.
    """
    result: list[dict] = []
    for path in _history_files(state_path, since):
        rows = _cached(path, f'down:{bucket_seconds}', lambda p: downsample(
            [{k: r.get(k) for k in HISTORY_FIELDS} for r in _iter_records(p)], bucket_seconds))
        result.extend(r for r in rows or [] if r['timestamp'] >= since)
    result.sort(key=lambda r: r['timestamp'])
    return result


def _scan_sessions(path: Path) -> dict:
    """One pass over a day: per-sample project RSS and per-session extremes."""
    points: list[tuple[float, dict]] = []
    sessions: dict[str, dict] = {}
    for record in _iter_records(path):
        ts = record['timestamp']
        by_project: dict[str, int] = {}
        for session in record.get('sessions') or []:
            if not isinstance(session, dict):
                continue
            projects = session.get('projects') or [session]
            for part in projects:
                if isinstance(part, dict) and isinstance(part.get('rss_kib'), (int, float)):
                    name = str(part.get('project') or '(unknown)')
                    by_project[name] = by_project.get(name, 0) + int(part['rss_kib'])
            age = session.get('age_seconds')
            pid = session.get('root_pid')
            if not isinstance(age, (int, float)) or not isinstance(pid, int):
                continue
            started = round((ts - age) / 60) * 60
            key = f"{session.get('provider')}:{pid}:{started:.0f}"
            entry = sessions.get(key)
            rss = int(session.get('rss_kib') or 0)
            if entry is None:
                entry = sessions[key] = {
                    'provider': session.get('provider'), 'root_pid': pid,
                    'root_name': session.get('root_name'), 'project': session.get('project'),
                    'started_at': ts - age, 'first_seen': ts, 'last_seen': ts,
                    'peak_rss_kib': rss, 'peak_mcp_count': int(session.get('mcp_count') or 0),
                    'persistent': bool(session.get('persistent'))}
            entry['last_seen'] = ts
            entry['peak_rss_kib'] = max(entry['peak_rss_kib'], rss)
            entry['peak_mcp_count'] = max(entry['peak_mcp_count'], int(session.get('mcp_count') or 0))
        points.append((ts, by_project))
    return {'points': points, 'sessions': sessions}


def memory_attribution(state_path: Path, since: float, bucket_seconds: int,
                       top: int = 7) -> dict:
    """Stacked LLM-session memory by project over time.

    Each bucket holds the mean RSS per project across its samples (a project
    absent from a sample counts as 0 there). The `top` projects by peak keep
    their own series; the rest fold into '기타'.
    """
    samples: list[tuple[float, dict]] = []
    for path in _history_files(state_path, since):
        scan = _cached(path, 'sessions', _scan_sessions)
        samples.extend(p for p in (scan or {}).get('points', []) if p[0] >= since)
    samples.sort(key=lambda p: p[0])
    peaks: dict[str, int] = {}
    for _, projects in samples:
        for name, kib in projects.items():
            peaks[name] = max(peaks.get(name, 0), kib)
    keep = [name for name, _ in sorted(peaks.items(), key=lambda kv: -kv[1])[:top]]
    other = len(peaks) > len(keep)
    keys = keep + (['기타'] if other else [])
    step = bucket_seconds or 60
    buckets: dict[int, dict] = {}
    for ts, projects in samples:
        acc = buckets.setdefault(int(ts // step), {'timestamp': ts, 'n': 0, 'sum': {}})
        acc['timestamp'] = ts
        acc['n'] += 1
        for name, kib in projects.items():
            label = name if name in keep else '기타'
            acc['sum'][label] = acc['sum'].get(label, 0) + kib
    rows = [{'timestamp': acc['timestamp'],
             'projects': {k: round(v / acc['n']) for k, v in acc['sum'].items()}}
            for _, acc in sorted(buckets.items())]
    return {'keys': keys, 'rows': rows}


def session_history(state_path: Path, since: float, now: float | None = None,
                    active_gap: float = 300) -> list[dict]:
    """Sessions seen in history, newest end first, with peak memory.

    A session is identified by provider, root PID, and start minute (PIDs are
    reused). ended=True once it has not been sampled for active_gap seconds.
    """
    merged: dict[str, dict] = {}
    for path in _history_files(state_path, since):
        scan = _cached(path, 'sessions', _scan_sessions)
        for key, entry in ((scan or {}).get('sessions') or {}).items():
            if entry['last_seen'] < since:
                continue
            seen = merged.get(key)
            if seen is None:
                merged[key] = dict(entry)
                continue
            seen['first_seen'] = min(seen['first_seen'], entry['first_seen'])
            seen['last_seen'] = max(seen['last_seen'], entry['last_seen'])
            seen['peak_rss_kib'] = max(seen['peak_rss_kib'], entry['peak_rss_kib'])
            seen['peak_mcp_count'] = max(seen['peak_mcp_count'], entry['peak_mcp_count'])
    current = now if now is not None else time.time()
    rows = []
    for entry in merged.values():
        entry['ended'] = current - entry['last_seen'] > active_gap
        entry['duration_seconds'] = max(0, entry['last_seen'] - entry['started_at'])
        rows.append(entry)
    rows.sort(key=lambda e: -e['last_seen'])
    return rows
