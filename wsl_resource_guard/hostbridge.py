"""Every Windows program is started from this module.

The host query is a fixed read-only script; the toast is the only other caller
and supplies escaped text. Calls are bounded in time and run one at a time. The
Windows mount is a 9p share: stat and exec on it can block before a subprocess
timeout even starts, so callers use ``bounded_probe`` around the whole call.
A failed or missing observation keeps the last values and says so in ``status``;
it is never turned into zeros.
"""
from __future__ import annotations

import base64
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import time

from .disks import ProbeBusy, ProbeTimeout, bounded_probe, mount_table

POWERSHELL_RELATIVE = 'Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
HOST_SCHEMA = 1
HOST_TIMEOUT_SECONDS = 20
SCRIPT_TIMEOUT_SECONDS = 15
MAX_OUTPUT_BYTES = 256 * 1024
MAX_ROWS = 32
INTEROP_RETRY_SECONDS = 0.5
MAX_RETRY_SECONDS = 900
_DRIVE_SOURCE = re.compile(r'^([A-Za-z]):\\?$')
_DATA_KEYS = ('memory', 'vmmem', 'volumes', 'distros', 'wslconfig', 'processes', 'windows', 'distro', 'vhd_path')
_STATUS_TEXT = {
    'unavailable': 'Windows interop 또는 PowerShell을 사용할 수 없습니다.',
    'timeout': f'Windows가 {HOST_TIMEOUT_SECONDS}초 안에 응답하지 않았습니다.',
    'busy': '이전 Windows 조회가 아직 끝나지 않았습니다.',
    'error': 'Windows 상태를 읽지 못했습니다.',
}


class HostUnavailable(OSError):
    """No Windows mount with PowerShell is reachable from this distro."""


class HostError(ValueError):
    """The script ran but its result is unusable; the message is a fixed, safe text."""


# Read-only and fixed: no value from configuration or a request is interpolated.
HOST_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# A failed part yields nothing at all; a caught error must never become 0.
function Safe([scriptblock]$b) { try { & $b } catch { } }
function Num($v) { if ($null -eq $v) { $null } else { [uint64]$v } }
$encoding = [Console]::OutputEncoding
try {
  [Console]::OutputEncoding = [Text.Encoding]::UTF8
  $os = Safe { Get-CimInstance Win32_OperatingSystem }
  $perf = Safe { Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory }
  $pf = @(Safe { Get-CimInstance Win32_PageFileUsage })
  $procs = @(Safe { Get-Process })
  $env:WSL_UTF8 = '1'
  $wsl = Safe { $ErrorActionPreference = 'Continue'; (& wsl.exe -l -v 2>$null) -join "`n" }
  $lxss = @(Safe { Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' | ForEach-Object { $p = Get-ItemProperty $_.PSPath; @{ name = [string]$p.DistributionName; base = [string]$p.BasePath } } })
  $cfgPath = Join-Path $env:USERPROFILE '.wslconfig'
  $cfg = Safe { if (Test-Path -LiteralPath $cfgPath) { (Get-Content -LiteralPath $cfgPath -TotalCount 200) -join "`n" } else { '' } }
  $json = @{
    schema = 1
    os = if ($os) { @{ total_kb = Num $os.TotalVisibleMemorySize; free_kb = Num $os.FreePhysicalMemory; build = [string]$os.BuildNumber; boot = $os.LastBootUpTime.ToUniversalTime().ToString('o') } } else { $null }
    memory = if ($perf) { @{ available = Num $perf.AvailableBytes; committed = Num $perf.CommittedBytes; commit_limit = Num $perf.CommitLimit } } else { $null }
    pagefile = @($pf | Where-Object { $_ } | ForEach-Object { @{ allocated_mb = Num $_.AllocatedBaseSize; used_mb = Num $_.CurrentUsage; peak_mb = Num $_.PeakUsage } })
    vmmem = @($procs | Where-Object { $_.ProcessName -like 'vmmem*' } | ForEach-Object { @{ name = $_.ProcessName; ws = Num $_.WorkingSet64; private = Num $_.PrivateMemorySize64 } })
    top = @($procs | Sort-Object WorkingSet64 -Descending | Select-Object -First 8 | ForEach-Object { @{ name = $_.ProcessName; ws = Num $_.WorkingSet64 } })
    volumes = @(Safe { [IO.DriveInfo]::GetDrives() | Where-Object { $_.DriveType -eq 'Fixed' -and $_.IsReady } | ForEach-Object { @{ id = $_.Name.Substring(0, 2); total = Num $_.TotalSize; free = Num $_.AvailableFreeSpace; fs = [string]$_.DriveFormat } } })
    wsl_list = $wsl
    lxss = $lxss
    wslconfig = $cfg
  } | ConvertTo-Json -Depth 4 -Compress
  $bytes = [Text.Encoding]::UTF8.GetBytes($json)
  $stdout = [Console]::OpenStandardOutput()
  $stdout.Write($bytes, 0, $bytes.Length)
} finally {
  [Console]::OutputEncoding = $encoding
}
'''


def windows_mounts() -> dict[str, str]:
    """Drive letter -> WSL mount point for mounted Windows volumes."""
    result = {}
    for mount, (fs_type, source) in mount_table().items():
        match = _DRIVE_SOURCE.match(source)
        if fs_type in ('9p', 'drvfs') and match:
            result.setdefault(match[1].upper(), mount)
    return result


def powershell_path() -> Path | None:
    """PowerShell on the mounted system drive. Unbounded: call inside a bounded probe.

    Only C: is trusted to hold it; another mounted drive that happens to carry
    the same path is never executed.
    """
    try:
        mount = windows_mounts().get('C')
    except (OSError, ValueError, IndexError):
        return None
    candidate = Path(mount) / POWERSHELL_RELATIVE if mount else None
    return candidate if candidate is not None and candidate.exists() else None


def run_powershell(script: str, *, timeout: float = SCRIPT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run one fixed script. Unbounded on the mount: wrap in bounded_probe."""
    executable = powershell_path()
    if executable is None:
        raise HostUnavailable('powershell.exe is unavailable')
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return subprocess.run(
        [str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        capture_output=True, timeout=timeout, check=False)


def _count(value, *, scale: int = 1) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value != value:
        return None
    return int(value) * scale if value < 2 ** 63 else None


def _text(value, limit: int = 120) -> str:
    return ''.join(char for char in str(value or '') if char.isprintable())[:limit]


def _rows(value) -> list[dict]:
    if isinstance(value, dict):  # ConvertTo-Json unwraps a single-element array.
        value = [value]
    return [row for row in value if isinstance(row, dict)][:MAX_ROWS] if isinstance(value, list) else []


def parse_wslconfig(text) -> dict:
    """Known keys only; comments and anything else in the file are dropped.

    `present` is None when the file could not be read at all: unknown, not absent.
    """
    result = {'present': bool(text.strip()) if isinstance(text, str) else None,
              'memory': None, 'swap': None, 'processors': None,
              'auto_memory_reclaim': None, 'sparse_vhd': None}
    wanted = {('wsl2', 'memory'): 'memory', ('wsl2', 'swap'): 'swap', ('wsl2', 'processors'): 'processors',
              ('experimental', 'automemoryreclaim'): 'auto_memory_reclaim',
              ('wsl2', 'automemoryreclaim'): 'auto_memory_reclaim',
              ('experimental', 'sparsevhd'): 'sparse_vhd', ('wsl2', 'sparsevhd'): 'sparse_vhd'}
    section = ''
    for raw in (text if isinstance(text, str) else '').splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip().lower()
            continue
        key, separator, value = line.partition('=')
        target = wanted.get((section, key.strip().lower()))
        if separator and target and re.fullmatch(r'[A-Za-z0-9.]{1,24}', value.strip()):
            result[target] = value.strip()
    return result


def parse_distros(listing, lxss) -> list[dict]:
    """`wsl.exe -l -v` rows joined with each distro's VHDX directory."""
    bases = {}
    for row in _rows(lxss):
        name, base = _text(row.get('name')), _text(row.get('base'), 300)
        if name:
            bases[name] = base[4:] if base.startswith('\\\\?\\') else base
    rows = []
    # An older wsl.exe ignores WSL_UTF8 and leaves NULs between characters.
    for line in (listing if isinstance(listing, str) else '').replace('\x00', '').splitlines()[1:]:
        default = line.lstrip().startswith('*')
        parts = line.replace('*', ' ', 1).split()
        if len(parts) < 3 or not parts[-1].isdigit():
            continue
        name = _text(' '.join(parts[:-2]))
        rows.append({'name': name, 'state': _text(parts[-2], 24), 'version': int(parts[-1]),
                     'default': default, 'vhd_dir': bases.get(name, '')})
    return rows[:MAX_ROWS]


def normalize(raw: dict) -> dict:
    """Typed, bounded data from the script's JSON. Missing parts stay None."""
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        raise ValueError('Unexpected host snapshot')
    system = raw.get('os') if isinstance(raw.get('os'), dict) else {}
    perf = raw.get('memory') if isinstance(raw.get('memory'), dict) else {}
    pagefiles = _rows(raw.get('pagefile'))
    paging = {key: (sum(values) if None not in values and values else None)
              for key, values in ((name, [_count(row.get(name), scale=2 ** 20) for row in pagefiles])
                                  for name in ('allocated_mb', 'used_mb', 'peak_mb'))}
    vm = [(_count(row.get('ws')), _count(row.get('private'))) for row in _rows(raw.get('vmmem'))]
    vm = [pair for pair in vm if pair[0] is not None]
    try:
        boot_at = datetime.fromisoformat(str(system.get('boot', '')).replace('Z', '+00:00')[:19] + '+00:00').timestamp()
    except ValueError:
        boot_at = None
    available = _count(perf.get('available'))
    return {
        'memory': {
            'total_bytes': _count(system.get('total_kb'), scale=1024),
            'available_bytes': available if available is not None else _count(system.get('free_kb'), scale=1024),
            'commit_used_bytes': _count(perf.get('committed')),
            'commit_limit_bytes': _count(perf.get('commit_limit')),
            'pagefile_allocated_bytes': paging['allocated_mb'],
            'pagefile_used_bytes': paging['used_mb'],
            'pagefile_peak_bytes': paging['peak_mb'],
        },
        'vmmem': ({'working_set_bytes': sum(pair[0] for pair in vm),
                   'private_bytes': (sum(pair[1] for pair in vm) if all(pair[1] is not None for pair in vm) else None)}
                  if vm else None),
        'volumes': [{'id': _text(row.get('id'), 2).upper(), 'total_bytes': _count(row.get('total')),
                     'free_bytes': _count(row.get('free')), 'filesystem': _text(row.get('fs'), 16)}
                    for row in _rows(raw.get('volumes'))
                    if re.fullmatch(r'[A-Za-z]:', str(row.get('id', ''))) and _count(row.get('total'))],
        'distros': parse_distros(raw.get('wsl_list'), raw.get('lxss')),
        'wslconfig': parse_wslconfig(raw.get('wslconfig')),
        'processes': [{'name': _text(row.get('name'), 64), 'working_set_bytes': _count(row.get('ws'))}
                      for row in _rows(raw.get('top'))[:8] if _count(row.get('ws')) is not None],
        'windows': {'build': _text(system.get('build'), 16), 'boot_at': boot_at},
    }


def _interop_start_failed(completed: subprocess.CompletedProcess) -> bool:
    # /init could not launch the Windows binary at all; seen intermittently.
    return bool(completed.returncode and not completed.stdout
                and completed.stderr.rstrip().endswith(b'Invalid argument'))


def _collect() -> dict:
    completed = run_powershell(HOST_SCRIPT)
    if _interop_start_failed(completed):
        time.sleep(INTEROP_RETRY_SECONDS)
        completed = run_powershell(HOST_SCRIPT)
        if _interop_start_failed(completed):
            raise HostError('interop could not start PowerShell')
    if completed.returncode:
        raise HostError(f'PowerShell exit {completed.returncode}')
    if len(completed.stdout) > MAX_OUTPUT_BYTES:
        raise HostError('output too large')
    try:
        data = normalize(json.loads(completed.stdout.decode('utf-8-sig')))
    except (ValueError, TypeError, KeyError):
        raise HostError('unexpected output') from None
    mounts = windows_mounts()
    for volume in data['volumes']:
        volume['mounted'] = volume['id'][0] in mounts
    data['distro'] = current_distro_name()
    data['vhd_path'] = _existing_vhd_path(data, mounts)
    return data


def _existing_vhd_path(data: dict, mounts: dict[str, str]) -> str:
    """WSL path of this distro's ext4.vhdx if it is really there, else ''. Unbounded."""
    row = next((d for d in data['distros'] if d['name'] == data['distro']), None) if data.get('distro') else None
    match = re.match(r'^([A-Za-z]):\\(.*)$', (row or {}).get('vhd_dir') or '')
    mount = mounts.get(match[1].upper()) if match else None
    if not mount:
        return ''
    parts = [part for part in match[2].split('\\') if part]
    if any(part in ('.', '..') or '\x00' in part for part in parts):
        return ''
    candidate = Path(mount, *parts, 'ext4.vhdx')
    try:
        return str(candidate) if candidate.is_file() else ''
    except (OSError, ValueError):
        return ''


def retry_interval(previous: dict, interval: float) -> float:
    """Back off after consecutive failures so a hung host is not probed every minute."""
    failures = previous.get('failures')
    if previous.get('status') == 'ok' or type(failures) is not int or failures <= 0:
        return interval
    return min(interval * 2 ** min(failures, 8), MAX_RETRY_SECONDS)


def read_host_snapshot(previous: dict | None = None, interval: float = 60, force: bool = False,
                       now: float | None = None) -> dict:
    """The host as Windows reports it, refreshed at most once per interval.

    Any attempt, successful or not, waits a full interval before the next one,
    so a broken interop path costs one PowerShell start per interval.
    """
    now = time.time() if now is None else now
    previous = previous if isinstance(previous, dict) and previous.get('schema') == HOST_SCHEMA else {}
    attempted = previous.get('attempted_at')
    if (not force and type(attempted) in (int, float) and 0 < attempted <= now
            and now - attempted < retry_interval(previous, interval)):
        return previous
    snapshot = {'schema': HOST_SCHEMA, 'status': 'ok', 'error': '', 'attempted_at': now, 'failures': 0,
                'observed_at': previous.get('observed_at')}
    try:
        snapshot.update(bounded_probe('host-snapshot', _collect, timeout=HOST_TIMEOUT_SECONDS))
        snapshot['observed_at'] = now
        return snapshot
    except HostUnavailable:
        status, cause = 'unavailable', ''
    except ProbeBusy:
        status, cause = 'busy', ''
    except (ProbeTimeout, subprocess.TimeoutExpired):
        status, cause = 'timeout', ''
    except HostError as exc:
        status, cause = 'error', str(exc)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        status, cause = 'error', type(exc).__name__
    failures = previous.get('failures')
    # The cause is a fixed text or an exception type name, never command output.
    snapshot.update(status=status, error=_STATUS_TEXT[status] + (f' ({cause})' if cause else ''),
                    failures=(failures if type(failures) is int and failures >= 0 else 0) + 1)
    # The last observation stays visible, marked as not current.
    for key in _DATA_KEYS:
        if key in previous:
            snapshot[key] = previous[key]
    return snapshot


def current_distro_name() -> str:
    """Name Windows uses for this distro, from the UNC form of '/'. Unbounded."""
    try:
        completed = subprocess.run(['wslpath', '-w', '/'], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ''
    match = re.match(r'^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\?$', completed.stdout.strip())
    return _text(match[1]) if match and completed.returncode == 0 else ''


def detected_vhd_path(host: dict | None) -> str:
    """The VHDX path found at collection time; '' when none was confirmed."""
    path = host.get('vhd_path') if isinstance(host, dict) else ''
    return path if isinstance(path, str) and path.startswith('/') and '\x00' not in path and len(path) < 1024 else ''
