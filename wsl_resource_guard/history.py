"""Resource-history reads, downsampling, and alert-episode extraction.

The daemon appends one JSON object per line to history-YYYY-MM-DD.jsonl files
under the state directory. Readers never write; the daemon is the only writer.
"""
from __future__ import annotations

from datetime import datetime
import json
import errno
import heapq
import math
import stat
from collections.abc import Iterable
from pathlib import Path
import re
import threading
import time

from .metrics import psi_status
from .safe_read import iter_text_lines

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


MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 100_000
READ_SECONDS = 10


def _encoded_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def _check_result(value):
    if _encoded_size(value) > MAX_RESULT_BYTES:
        raise OSError(errno.EFBIG, 'History result exceeds output limit')
    return value


class _Budget:
    def __init__(self):
        self.sizes = {}
        self.total = 0

    def keep(self, key, value):
        size = _encoded_size(value)
        self.total += size - self.sizes.get(key, 0)
        self.sizes[key] = size
        if self.total > MAX_RESULT_BYTES or len(self.sizes) > MAX_RECORDS:
            raise OSError(errno.EFBIG, 'History aggregate exceeds output limit')


def iter_history(state_path: Path, since: float, fields: tuple | None = HISTORY_FIELDS):
    """Stream finite timestamped rows in daily-file order under one deadline."""
    deadline = time.monotonic() + READ_SECONDS
    for path in _history_files(state_path, since):
        for record in _iter_records(path, deadline=deadline):
            if record['timestamp'] >= since:
                yield {k: record.get(k) for k in fields} if fields is not None else record


def read_history(state_path: Path, since: float, fields: tuple | None = HISTORY_FIELDS,
                 *, limit: int | None = None, severity: str | None = None,
                 max_records: int = MAX_RECORDS, max_bytes: int = MAX_RESULT_BYTES) -> list[dict]:
    """Oldest-first rows, optionally retaining only the latest bounded N rows.

    Exceeding a full raw response's budget is an explicit failure. Aggregators
    use iter_history instead, so their input is not limited by raw output size.
    """
    if limit is not None and not 1 <= limit <= max_records:
        raise ValueError('History limit is outside the supported range')
    rows = []
    size = 2
    for sequence, record in enumerate(iter_history(state_path, since, fields)):
        if severity is not None and record.get('severity') != severity:
            continue
        entry = (record['timestamp'], sequence, record, _encoded_size(record) + 2)
        if limit is not None and len(rows) == limit:
            if entry[:2] <= rows[0][:2]:
                continue
            removed = heapq.heapreplace(rows, entry)
            size -= removed[3]
        else:
            heapq.heappush(rows, entry)
        size += entry[3]
        if len(rows) > max_records or size > max_bytes:
            raise OSError(errno.EFBIG, 'History result exceeds output limit')
    return [entry[2] for entry in sorted(rows)]


def downsample(records: Iterable[dict], bucket_seconds: int) -> list[dict]:
    """Aggregate records into time buckets preserving worst-case values."""
    if bucket_seconds <= 0:
        return list(records)
    buckets: dict[int, dict] = {}
    budget = _Budget()
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
            budget.keep(key, {k: v for k, v in acc.items() if k != '_psi_statuses'})
            continue
        latest = ts >= acc['timestamp']
        acc['timestamp'] = max(acc['timestamp'], ts)
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
            if value is not None and latest:
                merged[field] = value
        budget.keep(key, {k: v for k, v in acc.items() if k != '_psi_statuses'})
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


def alert_episodes(records: Iterable[dict], gap_seconds: int = 1800,
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
    budget = _Budget()
    current: dict | None = None
    previous_ts: float | None = None
    for record in records:
        ts = record.get('timestamp')
        if not isinstance(ts, (int, float)):
            continue
        severity = record.get('severity') or 'unknown'
        if (current is not None and previous_ts is not None
                and (ts < previous_ts or ts - previous_ts > gap_seconds)):
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
        budget.keep(len(episodes), current)
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
    return _check_result(episodes)


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


# Per-file aggregate caches include inode/ctime: replacement and permission
# changes must not reuse an older parsed result, even with the same mtime/size.
_file_cache: dict[tuple, object] = {}
_file_cache_lock = threading.Lock()


def _cached(path: Path, kind: str, build):
    try:
        info = path.stat()
    except FileNotFoundError:
        raise OSError(errno.ENOENT, 'History file disappeared during read') from None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, 'Only regular history files may be read')
    key = (str(path), kind)
    stamp = (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size)
    with _file_cache_lock:
        hit = _file_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    value = _check_result(build(path))
    with _file_cache_lock:
        # A long-lived reader must not retain unlimited date/query combinations.
        while len(_file_cache) >= 16:
            _file_cache.pop(next(iter(_file_cache)))
        _file_cache[key] = (stamp, value)
        # Forget files that rotated away.
        for stale in [k for k in _file_cache if not Path(k[0]).exists()]:
            _file_cache.pop(stale, None)
    return value


def _history_files(state_path: Path, since: float) -> list[Path]:
    since_day = datetime.fromtimestamp(since).date() if since else None
    try:
        paths = sorted(path for path in state_path.iterdir()
                       if path.name.startswith('history-') and path.suffix == '.jsonl')
    except FileNotFoundError:
        return []
    return [path for path in paths
            if (day := _file_date(path, 'history-')) is not None
            and (since_day is None or day >= since_day)]


def _iter_records(path: Path, *, deadline: float | None = None):
    deadline = deadline if deadline is not None else time.monotonic() + READ_SECONDS
    for line in iter_text_lines(path, max_line_bytes=1024 * 1024, deadline=deadline):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(record, dict):
            ts = record.get('timestamp')
            if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts):
                yield record


def read_history_downsampled(state_path: Path, since: float, bucket_seconds: int) -> list[dict]:
    """Aggregate streaming daily inputs, caching only bounded bucket results."""
    if bucket_seconds <= 0:
        return read_history(state_path, since)
    result: list[dict] = []
    budget = _Budget()
    deadline = time.monotonic() + READ_SECONDS
    for path in _history_files(state_path, since):
        rows = _cached(path, f'down:{bucket_seconds}:{since}', lambda p: downsample(
            ({k: r.get(k) for k in HISTORY_FIELDS} for r in _iter_records(p, deadline=deadline)
             if r['timestamp'] >= since), bucket_seconds))
        for row in rows or []:
            budget.keep(len(result), row)
            result.append(row)
    result.sort(key=lambda r: r['timestamp'])
    # Recombine a bucket that crosses local midnight or a time-zone change.
    return _check_result(downsample(result, bucket_seconds))


def _scan_sessions(path: Path, deadline: float) -> dict:
    """Retain session extremes, never the full daily sample list."""
    budget = _Budget()
    sessions: dict[str, dict] = {}
    for record in _iter_records(path, deadline=deadline):
        ts = record['timestamp']
        for session in record.get('sessions') or []:
            if not isinstance(session, dict):
                continue
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
            entry['last_seen'] = max(entry['last_seen'], ts)
            entry['first_seen'] = min(entry['first_seen'], ts)
            entry['peak_rss_kib'] = max(entry['peak_rss_kib'], rss)
            entry['peak_mcp_count'] = max(entry['peak_mcp_count'], int(session.get('mcp_count') or 0))
            budget.keep(key, entry)
    return {'sessions': sessions}


def memory_attribution(state_path: Path, since: float, bucket_seconds: int,
                       top: int = 7) -> dict:
    """Stacked LLM-session memory by project over time.

    Each bucket holds the mean RSS per project across its samples (a project
    absent from a sample counts as 0 there). The `top` projects by peak keep
    their own series; the rest fold into '기타'.
    """
    peaks: dict[str, int] = {}
    buckets: dict[int, dict] = {}
    budget = _Budget()
    step = bucket_seconds or 60
    for record in iter_history(state_path, since, fields=None):
        ts = record['timestamp']
        projects = {}
        for session in record.get('sessions') or []:
            if not isinstance(session, dict):
                continue
            for part in session.get('projects') or [session]:
                if isinstance(part, dict) and isinstance(part.get('rss_kib'), (int, float)):
                    name = str(part.get('project') or '(unknown)')
                    projects[name] = projects.get(name, 0) + int(part['rss_kib'])
        key = int(ts // step)
        acc = buckets.setdefault(key, {'timestamp': ts, 'n': 0, 'sum': {}})
        acc['timestamp'] = max(acc['timestamp'], ts)
        acc['n'] += 1
        for name, kib in projects.items():
            peaks[name] = max(peaks.get(name, 0), kib)
            acc['sum'][name] = acc['sum'].get(name, 0) + kib
        budget.keep(key, acc)
    keep = [name for name, _ in sorted(peaks.items(), key=lambda kv: -kv[1])[:top]]
    keys = keep + (['기타'] if len(peaks) > len(keep) else [])
    rows = []
    for _, acc in sorted(buckets.items()):
        sums = {}
        for name, kib in acc['sum'].items():
            label = name if name in keep else '기타'
            sums[label] = sums.get(label, 0) + kib
        rows.append({'timestamp': acc['timestamp'],
                     'projects': {k: round(v / acc['n']) for k, v in sums.items()}})
    return _check_result({'keys': keys, 'rows': rows})


def session_history(state_path: Path, since: float, now: float | None = None,
                    active_gap: float = 300) -> list[dict]:
    """Sessions seen in history, newest end first, with peak memory.

    A session is identified by provider, root PID, and start minute (PIDs are
    reused). ended=True once it has not been sampled for active_gap seconds.
    """
    merged: dict[str, dict] = {}
    budget = _Budget()
    deadline = time.monotonic() + READ_SECONDS
    for path in _history_files(state_path, since):
        scan = _cached(path, 'sessions', lambda p: _scan_sessions(p, deadline))
        for key, entry in ((scan or {}).get('sessions') or {}).items():
            if entry['last_seen'] < since:
                continue
            seen = merged.get(key)
            if seen is None:
                merged[key] = dict(entry)
                budget.keep(key, entry)
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
    return _check_result(rows)
