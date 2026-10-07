from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import math
import subprocess
import time


KIB_PER_GIB = 1024 * 1024
POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


@dataclass(slots=True)
class SystemMetrics:
    timestamp: float
    mem_total_kib: int
    mem_available_kib: int
    swap_total_kib: int
    swap_free_kib: int
    psi_some_avg60: float | None
    psi_full_avg60: float | None
    pswpout_pages: int
    oom_kills: int
    swap_out_mib_per_minute: float = 0.0
    pswpin_pages: int = 0
    swap_in_mib_per_minute: float = 0.0
    disks: list[dict] = field(default_factory=list)
    vmmem_bytes: int | None = None
    vmmem_observed_at: float = 0.0
    vmmem_attempted_at: float = 0.0
    psi_status: str = "legacy"

    @property
    def mem_available_gib(self) -> float:
        return self.mem_available_kib / KIB_PER_GIB

    @property
    def swap_used_kib(self) -> int:
        return max(0, self.swap_total_kib - self.swap_free_kib)

    @property
    def swap_used_gib(self) -> float:
        return self.swap_used_kib / KIB_PER_GIB

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _read_key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                values[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                continue
    return values


def psi_status(metrics: dict) -> str:
    """Old numeric records remain readable but do not prove collector health."""
    status = metrics.get("psi_status")
    if status in ("normal", "missing", "error", "partial", "legacy"):
        return status
    return "legacy"


def psi_text(metrics: SystemMetrics) -> str:
    values = (metrics.psi_some_avg60, metrics.psi_full_avg60)
    formatted = " / ".join(f"{v:.2f}%" if v is not None else "관측 불가" for v in values)
    label = {"missing": "미제공", "error": "수집 오류", "legacy": "과거 수집 상태 미확인",
             "partial": "일부 관측"}.get(metrics.psi_status)
    return f"{formatted} ({label})" if label else formatted


def _read_memory_psi(path: Path = Path("/proc/pressure/memory")) -> tuple[float | None, float | None, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None, None, "missing"
    except (OSError, UnicodeError):
        return None, None, "error"
    values = {}
    try:
        for line in lines:
            parts = line.split()
            if parts and parts[0] in ("some", "full"):
                fields = dict(item.split("=", 1) for item in parts[1:] if "=" in item)
                value = float(fields["avg60"])
                if not math.isfinite(value) or not 0 <= value <= 100 or parts[0] in values:
                    raise ValueError("invalid PSI avg60")
                values[parts[0]] = value
        return values["some"], values["full"], "normal"
    except (KeyError, ValueError):
        return None, None, "error"


def read_system_metrics(previous: SystemMetrics | None = None) -> SystemMetrics:
    now = time.time()
    mem = _read_key_values(Path("/proc/meminfo"))
    vm = _read_key_values(Path("/proc/vmstat"))
    psi_some, psi_full, status = _read_memory_psi()
    current = SystemMetrics(
        timestamp=now,
        mem_total_kib=mem.get("MemTotal", 0),
        mem_available_kib=mem.get("MemAvailable", mem.get("MemFree", 0)),
        swap_total_kib=mem.get("SwapTotal", 0),
        swap_free_kib=mem.get("SwapFree", 0),
        psi_some_avg60=psi_some,
        psi_full_avg60=psi_full,
        psi_status=status,
        pswpout_pages=vm.get("pswpout", 0),
        oom_kills=vm.get("oom_kill", 0),
        pswpin_pages=vm.get("pswpin", 0),
    )
    if previous and now > previous.timestamp:
        elapsed = now - previous.timestamp
        page_size = 4096
        if current.pswpout_pages >= previous.pswpout_pages:
            pages = current.pswpout_pages - previous.pswpout_pages
            current.swap_out_mib_per_minute = pages * page_size / (1024 * 1024) * (60 / elapsed)
        if current.pswpin_pages >= previous.pswpin_pages:
            pages = current.pswpin_pages - previous.pswpin_pages
            current.swap_in_mib_per_minute = pages * page_size / (1024 * 1024) * (60 / elapsed)
    return current


def read_host_memory(
    previous_bytes: int | None = None,
    previous_at: float = 0.0,
    interval: float = 300,
    force: bool = False,
    previous_attempt: float = 0.0,
) -> tuple[int | None, float, float]:
    """vmmem* working set as seen by Windows: (bytes, observed_at, attempted_at).

    Keeps the last value on failure, and waits a full interval after any attempt,
    so a broken interop path costs one PowerShell start per interval, not per sample.
    """
    now = time.time()
    keep = (
        int(previous_bytes)
        if isinstance(previous_bytes, (int, float)) and not isinstance(previous_bytes, bool)
        else None
    )
    # Attempts are authoritative after a clock rollback; observed_at can still
    # describe a stale value from the old clock. Older states have no attempt.
    last_try = previous_attempt if 0 < previous_attempt <= now else previous_at
    if not force and 0 < last_try <= now and now - last_try < interval:
        return keep, previous_at, previous_attempt
    if not POWERSHELL.exists():
        return keep, previous_at, now
    try:
        completed = subprocess.run(
            [
                str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command",
                "(Get-Process vmmem* -ErrorAction SilentlyContinue | "
                "Measure-Object WorkingSet64 -Sum).Sum",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        text = completed.stdout.strip()
        if completed.returncode == 0 and text:
            return int(float(text)), now, now
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return keep, previous_at, now
