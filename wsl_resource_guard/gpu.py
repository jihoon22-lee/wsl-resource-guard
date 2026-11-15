"""GPU memory as nvidia-smi reports it inside WSL. Display only."""
from __future__ import annotations

import os
import shutil
import subprocess
import time

from .disks import ProbeTimeout, bounded_probe

# WSL exposes the host driver's tools here; the directory is a host-backed mount.
WSL_NVIDIA_SMI = '/usr/lib/wsl/lib/nvidia-smi'
GPU_TIMEOUT_SECONDS = 8
QUERY = ['--query-gpu=name,memory.used,memory.total,utilization.gpu', '--format=csv,noheader,nounits']


def nvidia_smi_path() -> str | None:
    if os.access(WSL_NVIDIA_SMI, os.X_OK):
        return WSL_NVIDIA_SMI
    return shutil.which('nvidia-smi', path='/usr/bin:/usr/local/bin')


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None   # "[N/A]" and the like: not reported
    return value if value == value and value >= 0 else None


def _collect() -> list[dict] | None:
    tool = nvidia_smi_path()
    if tool is None:
        return None
    completed = subprocess.run([tool, *QUERY], capture_output=True, text=True, timeout=5, check=False)
    if completed.returncode:
        raise ValueError('nvidia-smi failed')
    devices = []
    for line in completed.stdout.splitlines()[:16]:
        parts = [part.strip() for part in line.split(',')]
        if len(parts) != 4 or not parts[0]:
            raise ValueError('unexpected nvidia-smi output')
        used, total, load = (_number(part) for part in parts[1:])
        devices.append({'name': ''.join(c for c in parts[0] if c.isprintable())[:80],
                        'memory_used_bytes': int(used * 2**20) if used is not None else None,
                        'memory_total_bytes': int(total * 2**20) if total is not None else None,
                        'utilization_percent': load})
    return devices


def read_gpu(previous: dict | None = None, interval: float = 60, now: float | None = None) -> dict:
    """GPU devices, refreshed at most once per interval; a failure keeps the last values."""
    now = time.time() if now is None else now
    previous = previous if isinstance(previous, dict) and isinstance(previous.get('devices'), list) else {}
    attempted = previous.get('attempted_at')
    if type(attempted) in (int, float) and 0 < attempted <= now and now - attempted < interval:
        return previous
    observed = previous.get('observed_at')
    snapshot = {'status': 'ok', 'attempted_at': now, 'devices': previous.get('devices', []),
                'observed_at': observed if type(observed) in (int, float) else None}
    try:
        devices = bounded_probe('gpu', _collect, timeout=GPU_TIMEOUT_SECONDS)
    except (ProbeTimeout, OSError, ValueError, subprocess.SubprocessError):
        snapshot['status'] = 'error'
        return snapshot
    if devices is None:
        return {'status': 'missing', 'attempted_at': now, 'observed_at': None, 'devices': []}
    snapshot.update(devices=devices, observed_at=now)
    return snapshot
