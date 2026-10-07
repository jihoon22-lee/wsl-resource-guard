from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import fcntl
from datetime import datetime, timedelta
import html
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from .safe_read import read_text
from .config import (CONFIG_RULES, WEB_EDITABLE_KINDS, Settings, check_relations,
                     coerce_config_value, write_config_value)
from .disks import disk_severity, read_disks
from .metrics import KIB_PER_GIB, SystemMetrics, read_host_memory, read_system_metrics
from .notifications import SKIPPED_DETAILS, Notifier, NotificationResult
from .processes import ProcessSnapshot, SessionUsage, apply_cpu_rates, build_snapshot
from .reporting import build_html_report, build_text_report, collect_resource_rows


# Written by the root service controller, readable by the owner's group.
SHARED_DIR = Path("/var/lib/wrg-shared")


def read_snooze_until(shared_dir: Path = SHARED_DIR, now: float | None = None) -> float:
    """Epoch until which alert reminders are paused from the dashboard, or 0."""
    try:
        data = json.loads((shared_dir / "alert-snooze.json").read_text(encoding="utf-8"))
        until = float(data.get("until") or 0)
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return 0.0
    current = time.time() if now is None else now
    # A pause is capped at one day even if the file says otherwise.
    return until if current < until <= current + 86400 + 60 else 0.0


@dataclass(slots=True)
class Evaluation:
    severity: str
    reasons: list[str]
    offenders: list[SessionUsage]
    observations: list[str] = field(default_factory=list)
    pending_reasons: list[str] = field(default_factory=list)
    condition_since: dict[str, float] = field(default_factory=dict, repr=False)
    psi_alert: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class AlertCondition:
    key: str
    severity: str
    reason: str
    sustain_seconds: int = 0


def oom_kill_victims(since: float) -> list[str]:
    """Names of processes the kernel OOM killer reaped since `since`."""
    try:
        completed = subprocess.run(
            [
                "journalctl", "-k", "-q", "--no-pager", "-o", "cat",
                "--since", f"@{max(0, int(since) - 5)}",
                "-g", "Killed process",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    victims: list[str] = []
    for line in completed.stdout.splitlines():
        match = re.search(r"Killed process \d+ \(([^)]+)\)", line)
        if match and match.group(1) not in victims:
            victims.append(match.group(1))
    return victims


def evaluate(
    metrics: SystemMetrics,
    snapshot: ProcessSnapshot,
    settings: Settings,
    previous: SystemMetrics | None = None,
    condition_since: dict[str, float] | None = None,
    oom_victims: list[str] | None = None,
) -> Evaluation:
    conditions: list[AlertCondition] = []
    observations: list[str] = []

    for disk in metrics.disks:
        if disk.get("kind") != "windows":
            continue
        if disk.get("status") != "ok":
            observations.append(f"{disk['id']} 디스크 조회 불가: {disk.get('error', '')}")
        severity = disk_severity(disk, settings.warning_disk_free_percent, settings.critical_disk_free_percent)
        if severity in ("warning", "critical"):
            stale = " (마지막 확인값 · 현재 조회 불가)" if disk.get("status") != "ok" else ""
            conditions.append(AlertCondition(
                f"{severity}.disk.{disk['id']}", severity,
                f"{disk['id']} 디스크 여유 {disk['available_percent']:.1f}% "
                f"({disk['available_bytes'] / 2**30:.1f} GiB / {disk['total_bytes'] / 2**30:.1f} GiB){stale}",
            ))

    if metrics.mem_available_gib <= settings.critical_available_gib:
        conditions.append(
            AlertCondition(
                "critical.available_ram",
                "critical",
                f"가용 RAM {metrics.mem_available_gib:.1f} GiB",
            )
        )
    elif metrics.mem_available_gib <= settings.warning_available_gib:
        conditions.append(
            AlertCondition(
                "warning.available_ram",
                "warning",
                f"가용 RAM {metrics.mem_available_gib:.1f} GiB",
                settings.warning_available_sustain_seconds,
            )
        )

    if metrics.psi_some_avg60 is None or metrics.psi_full_avg60 is None:
        label = "미제공" if metrics.psi_status == "missing" else "수집 오류"
        observations.append(f"PSI 관측 불가 ({label}) · 자원 경보와 별도 수집 상태")
    elif metrics.psi_status == "legacy":
        observations.append("PSI 과거 수집 상태 미확인")
    critical_pressure = False
    if metrics.psi_full_avg60 is not None and metrics.psi_full_avg60 >= settings.critical_psi_full_avg60:
        critical_pressure = True
        conditions.append(
            AlertCondition(
                "critical.psi_full",
                "critical",
                f"메모리 완전 정체 PSI {metrics.psi_full_avg60:.2f}%",
                settings.critical_psi_sustain_seconds,
            )
        )
    elif (
        metrics.psi_full_avg60 is not None
        and metrics.psi_full_avg60 >= settings.critical_psi_full_low_ram_avg60
        and metrics.mem_available_gib <= settings.critical_psi_full_low_ram_available_gib
    ):
        critical_pressure = True
        conditions.append(
            AlertCondition(
                "critical.psi_full_low_ram",
                "critical",
                f"가용 RAM {metrics.mem_available_gib:.1f} GiB에서 "
                f"완전 정체 PSI {metrics.psi_full_avg60:.2f}%",
                settings.critical_psi_sustain_seconds,
            )
        )
    if not critical_pressure and metrics.psi_some_avg60 is not None and metrics.psi_some_avg60 >= settings.warning_psi_some_avg60:
        conditions.append(
            AlertCondition(
                "warning.psi_some",
                "warning",
                f"메모리 압력 PSI {metrics.psi_some_avg60:.2f}%",
                settings.warning_psi_sustain_seconds,
            )
        )

    if previous and metrics.oom_kills > previous.oom_kills:
        detail = f" (희생 프로세스: {', '.join(oom_victims[:3])})" if oom_victims else ""
        conditions.append(
            AlertCondition(
                "critical.oom_kill",
                "critical",
                f"OOM kill {metrics.oom_kills - previous.oom_kills}건 발생{detail}",
            )
        )
    if (
        metrics.swap_out_mib_per_minute >= settings.warning_swap_out_mib_per_minute
        and metrics.mem_available_gib <= settings.warning_swap_out_max_available_gib
    ):
        conditions.append(
            AlertCondition(
                "warning.swap_out",
                "warning",
                f"가용 RAM {metrics.mem_available_gib:.1f} GiB, "
                f"swap-out {metrics.swap_out_mib_per_minute:.0f} MiB/min",
                settings.warning_swap_out_sustain_seconds,
            )
        )

    session_observe_limit_kib = int(settings.session_observe_rss_gib * KIB_PER_GIB)
    session_limit_kib = int(settings.session_warning_rss_gib * KIB_PER_GIB)
    offenders = [session for session in snapshot.sessions if session.rss_kib >= session_observe_limit_kib]
    if offenders:
        top = offenders[0]
        observations.append(
            f"대형 세션 {top.provider} PID {top.root_pid}, "
            f"RSS {top.rss_kib / KIB_PER_GIB:.2f} GiB"
        )
    for session in snapshot.sessions:
        if session.rss_kib < session_limit_kib:
            continue
        conditions.append(
            AlertCondition(
                f"warning.session.{session.root_pid}",
                "warning",
                f"{session.provider} PID {session.root_pid} 트리 "
                f"{session.rss_kib / KIB_PER_GIB:.2f} GiB",
                settings.process_warning_sustain_seconds,
            )
        )

    if snapshot.mcp_rss_kib >= settings.mcp_observe_rss_gib * KIB_PER_GIB:
        observations.append(
            f"MCP 트리 {snapshot.mcp_count}개(프로세스 {snapshot.mcp_process_count}개), "
            f"RSS {snapshot.mcp_rss_kib / KIB_PER_GIB:.2f} GiB"
        )
    if snapshot.mcp_rss_kib >= settings.mcp_warning_rss_gib * KIB_PER_GIB:
        conditions.append(
            AlertCondition(
                "warning.mcp_total",
                "warning",
                f"MCP 트리 {snapshot.mcp_count}개(프로세스 {snapshot.mcp_process_count}개), "
                f"RSS {snapshot.mcp_rss_kib / KIB_PER_GIB:.2f} GiB",
                settings.process_warning_sustain_seconds,
            )
        )

    now = metrics.timestamp
    previous_since = condition_since or {}
    current_since: dict[str, float] = {}
    critical: list[str] = []
    warning: list[str] = []
    pending: list[str] = []
    psi_alert: dict = {}
    for condition in conditions:
        try:
            started_at = float(previous_since.get(condition.key, now))
        except (TypeError, ValueError):
            started_at = now
        if started_at > now or started_at < 0:
            started_at = now
        current_since[condition.key] = started_at
        elapsed = max(0.0, now - started_at)
        if elapsed >= condition.sustain_seconds:
            target = critical if condition.severity == "critical" else warning
            target.append(condition.reason)
            if ".psi_" in condition.key:
                psi_alert = {"severity": condition.severity, "reasons": [condition.reason]}
            continue
        remaining = max(1, math.ceil(condition.sustain_seconds - elapsed))
        pending.append(f"{condition.reason} ({condition.severity}까지 {remaining}초)")

    if critical:
        severity = "critical"
        reasons = critical + warning
    elif warning:
        severity = "warning"
        reasons = warning
    else:
        severity = "normal"
        reasons = []
    return Evaluation(
        severity,
        reasons,
        offenders,
        observations=observations,
        pending_reasons=pending,
        condition_since=current_since,
        psi_alert=psi_alert,
    )


def stabilize_recovery(
    evaluation: Evaluation,
    old_severity: str,
    old_reasons: list[str],
    now: float,
    normal_since: float,
    settings: Settings,
) -> tuple[Evaluation, float, bool]:
    if evaluation.severity != "normal":
        return evaluation, 0.0, False
    if old_severity == "normal":
        return evaluation, 0.0, False
    if evaluation.pending_reasons:
        held = Evaluation(
            old_severity,
            old_reasons,
            evaluation.offenders,
            observations=evaluation.observations,
            pending_reasons=evaluation.pending_reasons,
            condition_since=evaluation.condition_since,
        )
        return held, 0.0, False
    started_at = normal_since if 0 < normal_since <= now else now
    elapsed = max(0.0, now - started_at)
    if elapsed >= settings.recovery_sustain_seconds:
        return evaluation, started_at, True
    remaining = max(1, math.ceil(settings.recovery_sustain_seconds - elapsed))
    held = Evaluation(
        old_severity,
        old_reasons,
        evaluation.offenders,
        observations=evaluation.observations,
        pending_reasons=[f"정상 상태 확인 중 ({remaining}초 남음)"],
        condition_since=evaluation.condition_since,
    )
    return held, started_at, False


def _state_float(state: dict[str, object], key: str) -> float:
    try:
        return float(state.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _metrics_from_state(data: dict[str, object]) -> SystemMetrics | None:
    raw = data.get("metrics")
    if not isinstance(raw, dict):
        return None
    try:
        metrics = SystemMetrics(**raw)
    except (TypeError, ValueError):
        return None
    numeric = ("timestamp", "mem_total_kib", "mem_available_kib", "swap_total_kib",
               "swap_free_kib",
               "pswpout_pages", "oom_kills", "swap_out_mib_per_minute",
               "pswpin_pages", "swap_in_mib_per_minute")
    if any(not isinstance(getattr(metrics, key), (int, float)) for key in numeric):
        return None
    for key in ("psi_some_avg60", "psi_full_avg60"):
        value = getattr(metrics, key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or not 0 <= value <= 100):
            return None
    if metrics.psi_status not in ("normal", "missing", "error", "legacy", "partial"):
        return None
    return metrics if isinstance(metrics.disks, list) else None


def load_state(path: Path) -> dict[str, object]:
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _event_reports(
    metrics: SystemMetrics,
    snapshot: ProcessSnapshot,
    evaluation: Evaluation,
) -> tuple[str, str]:
    rows = collect_resource_rows(snapshot)
    arguments = (
        metrics,
        snapshot,
        evaluation.severity,
        evaluation.reasons,
        evaluation.observations,
        evaluation.pending_reasons,
        rows,
    )
    return build_text_report(*arguments), build_html_report(*arguments)


def _event_title(metrics: SystemMetrics, label: str = "WSL 자원") -> str:
    memory_used_gib = max(0, metrics.mem_total_kib - metrics.mem_available_kib) / KIB_PER_GIB
    memory_total_gib = metrics.mem_total_kib / KIB_PER_GIB
    swap_total_gib = metrics.swap_total_kib / KIB_PER_GIB
    disk_title = ""
    measured = [d for d in metrics.disks if d.get("kind") == "windows" and d.get("available_percent") is not None]
    if measured:
        lowest = min(measured, key=lambda d: d["available_percent"])
        disk_title = f" · Disk {lowest['id']} 여유 {lowest['available_percent']:.1f}%"
    return (
        f"{label} · RAM {memory_used_gib:.1f}/{memory_total_gib:.1f} GiB · "
        f"Swap {metrics.swap_used_gib:.1f}/{swap_total_gib:.1f} GiB{disk_title}"
    )


def _history_record(metrics: SystemMetrics, snapshot: ProcessSnapshot, evaluation: Evaluation) -> dict[str, object]:
    return {
        "timestamp": metrics.timestamp,
        "severity": evaluation.severity,
        "reasons": evaluation.reasons,
        "observations": evaluation.observations,
        "pending_reasons": evaluation.pending_reasons,
        "metrics": metrics.to_dict(),
        "sessions": [session.to_dict() for session in snapshot.sessions],
        "mcp_rss_kib": snapshot.mcp_rss_kib,
        "mcp_count": snapshot.mcp_count,
        "mcp_process_count": snapshot.mcp_process_count,
    }


def append_history(state_dir: Path, record: dict[str, object], retention_days: int) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        stamp = float(record.get("timestamp") or time.time())
    except (TypeError, ValueError):
        stamp = time.time()
    sampled_at = datetime.fromtimestamp(stamp).astimezone()
    path = state_dir / f"history-{sampled_at:%Y-%m-%d}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    cutoff = sampled_at.date() - timedelta(days=retention_days)
    for candidate in state_dir.glob("history-*.jsonl"):
        try:
            day = datetime.strptime(candidate.stem.removeprefix("history-"), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                candidate.unlink()
            except OSError:
                pass


def should_log_sample(
    severity: str,
    last_severity: str | None,
    now: float,
    last_logged: float | None,
    interval: float = 600.0,
) -> bool:
    """Limit journal volume: log the first sample, severity changes, and a heartbeat."""
    if last_logged is None or last_severity is None or severity != last_severity:
        return True
    return now - last_logged >= interval


def _log_notification_results(results: list[NotificationResult]) -> None:
    for result in results:
        outcome = "sent" if result.sent else "skipped" if result.skipped else "not-sent"
        print(f"notification channel={result.channel} outcome={outcome} detail={result.detail}", flush=True)


def _record_channel_results(channel_errors: dict[str, str], results: list[NotificationResult]) -> None:
    for result in results:
        if result.sent or result.skipped:
            channel_errors.pop(result.channel, None)
        else:
            channel_errors[result.channel] = result.detail


def append_disk_history(state_dir: Path, metrics: SystemMetrics, retention_days: int) -> None:
    if not metrics.disks:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    sampled_at = datetime.fromtimestamp(metrics.timestamp).astimezone()
    path = state_dir / f"disk-history-{sampled_at:%Y-%m-%d}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": metrics.timestamp, "disks": metrics.disks},
                                ensure_ascii=False, separators=(",", ":")) + "\n")
    cutoff = sampled_at.date() - timedelta(days=retention_days)
    for candidate in state_dir.glob("disk-history-*.jsonl"):
        try:
            day = datetime.strptime(candidate.stem.removeprefix("disk-history-"), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            candidate.unlink(missing_ok=True)


def in_quiet_hours(now: float, spec: str) -> bool:
    """True when local time is inside an "HH[:MM]-HH[:MM]" window (may cross midnight)."""
    match = re.fullmatch(
        r"([01]?\d|2[0-3])(?::([0-5]\d))?-([01]?\d|2[0-3])(?::([0-5]\d))?",
        spec.strip(),
    )
    if not match:
        return False
    start = int(match.group(1)) * 60 + int(match.group(2) or 0)
    end = int(match.group(3)) * 60 + int(match.group(4) or 0)
    if start == end:
        return False
    local = datetime.fromtimestamp(now).astimezone()
    minutes = local.hour * 60 + local.minute
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def alert_reminder_due(
    severity: str,
    old_severity: str,
    now: float,
    last_alert: float,
    settings: Settings,
    snoozed: bool = False,
) -> bool:
    if severity == "normal":
        return False
    if severity != old_severity:
        return True
    # A dashboard snooze pauses reminders of an unchanged severity only.
    if snoozed:
        return False
    # Quiet hours suppress only warning reminders. Severity transitions above
    # already returned, and critical reminders and new disk conditions are kept.
    if severity == "warning" and in_quiet_hours(now, settings.alert_quiet_hours):
        return False
    interval = (
        settings.critical_reminder_seconds
        if severity == "critical"
        else settings.reminder_cooldown_seconds
    )
    return now - last_alert >= interval


def _duration_label(seconds: float) -> str:
    total_minutes = int(seconds // 60)
    days, remaining = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining, 60)
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분"
    return f"{int(seconds)}초"


def email_hour_slot(now: float) -> str:
    return datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%dT%H")


def _heartbeat_at_or_before(now: float, settings: Settings) -> int:
    # Walk real minute boundaries instead of adding a civil hour: DST can
    # repeat/skip an hour, and some zones change offset by only 30 minutes.
    candidate = math.floor(now / 60) * 60
    minute = time.localtime(candidate).tm_min
    quick = candidate - ((minute - settings.email_heartbeat_minute) % 60) * 60
    if time.localtime(quick).tm_min == settings.email_heartbeat_minute:
        return quick
    for offset in range(181):
        stamp = candidate - offset * 60
        if time.localtime(stamp).tm_min == settings.email_heartbeat_minute:
            return stamp
    raise ValueError('Heartbeat schedule has no recent local minute')


def email_heartbeat_slot(now: float, settings: Settings) -> str:
    return f'epoch:{_heartbeat_at_or_before(now, settings)}'


def _last_email_slot_time(slot: str, settings: Settings) -> float | None:
    try:
        if slot.startswith('epoch:'):
            return float(int(slot[6:]))
        # Legacy civil-hour stamps lacked an offset/fold. Interpret them in
        # the current local zone once; all subsequent writes use epoch slots.
        return datetime.strptime(slot, '%Y-%m-%dT%H').replace(
            minute=settings.email_heartbeat_minute).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def email_heartbeat_due(now: float, last_email_hour_slot: str, settings: Settings) -> bool:
    if not settings.email_heartbeat_enabled:
        return False
    previous = _last_email_slot_time(last_email_hour_slot, settings)
    due = _heartbeat_at_or_before(now, settings)
    return previous is None or previous > now or previous < due


def next_email_heartbeat_at(
    now: float,
    last_email_hour_slot: str,
    settings: Settings,
) -> datetime | None:
    if not settings.email_heartbeat_enabled:
        return None
    if email_heartbeat_due(now, last_email_hour_slot, settings):
        return datetime.fromtimestamp(_heartbeat_at_or_before(now, settings)).astimezone()
    minute = math.floor(now / 60) * 60
    for offset in range(1, 181):
        candidate = minute + offset * 60
        if time.localtime(candidate).tm_min == settings.email_heartbeat_minute:
            return datetime.fromtimestamp(candidate).astimezone()
    raise ValueError('Heartbeat schedule has no upcoming local minute')


def sample(settings: Settings, notify: bool = False, persist: bool = True,
           shared_dir: Path = SHARED_DIR) -> tuple[SystemMetrics, ProcessSnapshot, Evaluation]:
    state_path = settings.state_path / "state.json"
    state = load_state(state_path)
    previous = _metrics_from_state(state)
    metrics = read_system_metrics(previous)
    metrics.disks = read_disks(
        settings.disk_drives, settings.wsl_vhd_path,
        previous.disks if previous else None, interval=settings.disk_refresh_seconds,
    )
    metrics.vmmem_bytes, metrics.vmmem_observed_at, metrics.vmmem_attempted_at = read_host_memory(
        previous.vmmem_bytes if previous else None,
        previous.vmmem_observed_at if previous else 0.0,
        interval=settings.host_memory_refresh_seconds,
        previous_attempt=previous.vmmem_attempted_at if previous else 0.0,
    )
    snapshot = build_snapshot(settings.project_roots)
    clock_ticks = os.sysconf("SC_CLK_TCK")
    next_cpu = apply_cpu_rates(snapshot, state.get("cpu_jiffies", {}), metrics.timestamp, clock_ticks)
    raw_condition_since = state.get("condition_since", {})
    condition_since: dict[str, float] = {}
    if isinstance(raw_condition_since, dict):
        for key, value in raw_condition_since.items():
            try:
                condition_since[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    sample_gap = previous is None or (
        metrics.timestamp < previous.timestamp
        or metrics.timestamp - previous.timestamp > max(1, 2 * settings.interval_seconds)
    )
    if sample_gap:
        condition_since = {}
    oom_victims = (
        oom_kill_victims(previous.timestamp)
        if previous and metrics.oom_kills > previous.oom_kills
        else []
    )
    evaluation = evaluate(
        metrics, snapshot, settings, previous, condition_since, oom_victims
    )

    old_severity = str(state.get("severity", "normal"))
    old_notified_severity = str(state.get("notified_severity", old_severity))
    raw_old_reasons = state.get("reasons", [])
    old_reasons = [str(reason) for reason in raw_old_reasons] if isinstance(raw_old_reasons, list) else []
    raw_notified_reasons = state.get("notified_reasons", old_reasons)
    notified_reasons = (
        [str(reason) for reason in raw_notified_reasons]
        if isinstance(raw_notified_reasons, list)
        else old_reasons
    )
    last_alert = _state_float(state, "last_alert")
    last_email_status = _state_float(state, "last_email_status")
    last_email_success = _state_float(state, "last_email_success")
    raw_channel_errors = state.get("last_channel_errors", {})
    channel_errors = (
        {str(key): str(value) for key, value in raw_channel_errors.items()
         if str(value) not in SKIPPED_DETAILS}
        if isinstance(raw_channel_errors, dict)
        else {}
    )
    last_email_hour_slot = str(state.get("last_email_hour_slot", "") or "")
    now = metrics.timestamp
    last_slot_time = _last_email_slot_time(last_email_hour_slot, settings)
    if last_slot_time is not None and last_slot_time > now:
        evaluation.observations.append('미래 heartbeat 처리 기록을 현재 스케줄로 재평가합니다.')
    normal_since = 0.0 if sample_gap else _state_float(state, "normal_since")
    # Start of the current abnormal episode; condition_since is emptied as soon
    # as the conditions clear, so it cannot date the episode at recovery time.
    abnormal_since = _state_float(state, "abnormal_since")
    recovery_from_severity = (
        old_severity if old_severity != "normal" else old_notified_severity
    )
    recovery_from_reasons = old_reasons if old_severity != "normal" else notified_reasons
    # Keep PSI alert identity independently of other resource alerts, including
    # across daemon restarts. Missing data clears pending sustain streaks but
    # cannot prove an active alert recovered.
    previous_psi_alert = state.get("psi_alert")
    if not isinstance(previous_psi_alert, dict):
        # Legacy states have no independent identity; recover it once from the
        # old reasons. An explicit empty dict means PSI already recovered.
        psi_reasons = [r for r in recovery_from_reasons if "PSI" in r]
        previous_psi_alert = ({"severity": recovery_from_severity, "reasons": psi_reasons}
                              if psi_reasons and recovery_from_severity != "normal" else {})
    psi_unobserved = metrics.psi_some_avg60 is None or metrics.psi_full_avg60 is None
    psi_alert = evaluation.psi_alert
    psi_pending = any(".psi_" in key for key in evaluation.condition_since) and not psi_alert
    psi_normal_since = 0.0
    hold_psi = False
    if not psi_alert and previous_psi_alert.get("severity") in ("warning", "critical"):
        if psi_unobserved or psi_pending:
            psi_alert = previous_psi_alert
            hold_psi = True
            normal_since = 0.0
        else:
            started = _state_float(state, "psi_normal_since")
            psi_normal_since = started if not sample_gap and 0 < started <= now else now
            if now - psi_normal_since < settings.recovery_sustain_seconds:
                psi_alert = previous_psi_alert
                # Other alerts must not erase PSI's independent recovery streak.
                # With no other condition, the existing global recovery timer
                # supplies the hold, avoiding two consecutive grace periods.
                hold_psi = evaluation.severity != "normal"
    if hold_psi:
        if psi_alert["severity"] == "critical" or evaluation.severity == "normal":
            evaluation.severity = psi_alert["severity"]
        evaluation.reasons.extend(r for r in psi_alert.get("reasons", []) if r not in evaluation.reasons)
        if psi_unobserved:
            evaluation.pending_reasons.append("PSI 관측 재개 후 정상 확인 필요")
        elif not psi_pending:
            remaining = max(1, math.ceil(settings.recovery_sustain_seconds - (now - psi_normal_since)))
            evaluation.pending_reasons.append(f"PSI 정상 상태 확인 중 ({remaining}초 남음)")
    disks_by_id = {disk["id"]: disk for disk in metrics.disks if disk.get("kind") == "windows"}
    configured_drives = {drive.rstrip(':').upper() + ':' for drive in settings.disk_drives}
    missing_disk_reasons = [reason for reason in recovery_from_reasons
        if any(reason.startswith(f'{drive} 디스크 ') and
               disks_by_id.get(drive, {}).get('status') != 'ok'
               for drive in configured_drives)]
    if missing_disk_reasons and recovery_from_severity != 'normal':
        if recovery_from_severity == 'critical' or evaluation.severity == 'normal':
            evaluation.severity = recovery_from_severity
        evaluation.reasons.extend(r for r in missing_disk_reasons if r not in evaluation.reasons)
        evaluation.pending_reasons.append('디스크 관측 재개 후 정상 확인 필요')
        normal_since = 0.0
    evaluation, normal_since, recovery_ready = stabilize_recovery(
        evaluation,
        recovery_from_severity,
        recovery_from_reasons,
        now,
        normal_since,
        settings,
    )
    # If the optional heartbeat is enabled, a nonnormal status email also
    # counts as that condition's reminder to avoid nearby duplicates.
    if evaluation.severity != "normal" and last_email_status > last_alert:
        last_alert = last_email_status
    should_alert = False
    disk_conditions = sorted(key for key in evaluation.condition_since if ".disk." in key)
    raw_notified_disks = state.get("notified_disk_conditions", [])
    old_notified_disks = [str(key) for key in raw_notified_disks] if isinstance(raw_notified_disks, list) else []
    raw_disk_normal = state.get("disk_normal_since", {})
    disk_normal_since = dict(raw_disk_normal) if isinstance(raw_disk_normal, dict) and not sample_gap else {}
    # Retain notification identities through the recovery grace period so a
    # drive oscillating around 20% does not generate a fresh alert every sample.
    for key in list(old_notified_disks):
        drive = key.split(".disk.", 1)[-1]
        current_disk = disks_by_id.get(drive)
        if current_disk is None:
            if drive not in configured_drives:
                old_notified_disks.remove(key)
            disk_normal_since.pop(drive, None)
        elif current_disk.get("status") == "ok" and disk_severity(
            current_disk, settings.warning_disk_free_percent, settings.critical_disk_free_percent
        ) == "normal":
            try:
                started = float(disk_normal_since.get(drive, now))
            except (TypeError, ValueError):
                started = now
            disk_normal_since[drive] = min(started, now)
            if now - disk_normal_since[drive] >= settings.recovery_sustain_seconds:
                old_notified_disks.remove(key)
                disk_normal_since.pop(drive, None)
        else:
            disk_normal_since.pop(drive, None)
    new_disk_condition = bool(set(disk_conditions) - set(old_notified_disks))
    alert_severity = evaluation.severity
    title = _event_title(metrics)
    recovery_duration = 0.0
    if evaluation.severity != "normal":
        should_alert = new_disk_condition or alert_reminder_due(
            evaluation.severity,
            old_notified_severity,
            now,
            last_alert,
            settings,
            snoozed=read_snooze_until(shared_dir, now) > 0,
        )
    elif old_notified_severity != "normal" and recovery_ready:
        if settings.recovery_notifications:
            should_alert = True
            alert_severity = "recovery"
            title = _event_title(metrics, "WSL 자원 회복")
            # The episode ended when the normal streak began, not when the
            # recovery_sustain_seconds confirmation finished. A start recorded
            # only after that streak began (state from an older version) yields
            # no positive span, so no duration line is claimed.
            if 0 < abnormal_since <= normal_since <= now:
                recovery_duration = normal_since - abnormal_since
        else:
            old_notified_severity = "normal"
            notified_reasons = []

    if notify and should_alert:
        message, html_message = _event_reports(metrics, snapshot, evaluation)
        if alert_severity == "recovery" and recovery_duration > 0:
            line = f"비정상 상태는 약 {_duration_label(recovery_duration)} 동안 지속됐습니다."
            message = f"{message}\n{line}"
            html_message = html_message.replace(
                "</body>", f"<p>{html.escape(line)}</p></body>", 1
            )
        results = Notifier(settings).send(
            title,
            message,
            alert_severity,
            html_message=html_message,
        )
        _log_notification_results(results)
        _record_channel_results(channel_errors, results)
        last_alert = now
        old_notified_severity = evaluation.severity
        notified_reasons = evaluation.reasons
        by_drive = {key.split(".disk.", 1)[-1]: key for key in old_notified_disks}
        by_drive.update({key.split(".disk.", 1)[-1]: key for key in disk_conditions})
        old_notified_disks = sorted(by_drive.values())
        if any(result.channel == "gmail" for result in results):
            if any(result.channel == 'gmail' and not result.skipped for result in results):
                last_email_status = now
            if any(result.channel == 'gmail' and result.sent for result in results):
                last_email_success = now
            if email_heartbeat_due(now, last_email_hour_slot, settings):
                # This sample's alert already processed the latest due slot.
                last_email_hour_slot = email_heartbeat_slot(now, settings)

    if notify and email_heartbeat_due(now, last_email_hour_slot, settings):
        message, html_message = _event_reports(metrics, snapshot, evaluation)
        results = Notifier(settings).send(
            _event_title(metrics, "WSL 상태"),
            message,
            evaluation.severity,
            channels={"gmail"},
            html_message=html_message,
        )
        _log_notification_results(results)
        _record_channel_results(channel_errors, results)
        # Record the attempt as well as success so a broken SMTP configuration
        # cannot trigger a retry every 15 seconds.
        if any(result.channel == 'gmail' and not result.skipped for result in results):
            last_email_status = now
        if any(result.channel == 'gmail' and result.sent for result in results):
            last_email_success = now
        last_email_hour_slot = email_heartbeat_slot(now, settings)
        if evaluation.severity != "normal":
            last_alert = now

    if evaluation.severity == "normal":
        abnormal_since = 0.0
    elif not 0 < abnormal_since <= now:
        abnormal_since = min(evaluation.condition_since.values(), default=now)

    next_state: dict[str, object] = {
        "updated_at": now,
        "severity": evaluation.severity,
        "notified_severity": old_notified_severity,
        "notified_reasons": notified_reasons,
        "reasons": evaluation.reasons,
        "observations": evaluation.observations,
        "pending_reasons": evaluation.pending_reasons,
        "condition_since": evaluation.condition_since,
        "normal_since": normal_since,
        "psi_alert": psi_alert,
        "psi_normal_since": psi_normal_since,
        "abnormal_since": abnormal_since,
        "last_alert": last_alert,
        "last_email_status": last_email_status,
        "last_email_success": last_email_success,
        "last_email_hour_slot": last_email_hour_slot,
        "last_channel_errors": channel_errors,
        "notified_disk_conditions": old_notified_disks,
        "disk_normal_since": disk_normal_since,
        "metrics": metrics.to_dict(),
        "sessions": [session.to_dict() for session in snapshot.sessions],
        "mcp_rss_kib": snapshot.mcp_rss_kib,
        "mcp_count": snapshot.mcp_count,
        "mcp_process_count": snapshot.mcp_process_count,
        "cpu_jiffies": next_cpu,
    }
    if persist:
        save_state(state_path, next_state)
    return metrics, snapshot, evaluation


# Disk trends read only the last record of each day, so hourly records are plenty.
DISK_HISTORY_INTERVAL_SECONDS = 3600


def record_history(
    settings: Settings,
    metrics: SystemMetrics,
    snapshot: ProcessSnapshot,
    evaluation: Evaluation,
    last_history: float,
    last_disk_history: float,
) -> tuple[float, float]:
    """Append due history records; return the updated (last_history, last_disk_history)."""
    if metrics.timestamp - last_history >= settings.history_interval_seconds:
        append_history(
            settings.state_path,
            _history_record(metrics, snapshot, evaluation),
            settings.retention_days,
        )
        last_history = metrics.timestamp
    if metrics.disks and metrics.timestamp - last_disk_history >= DISK_HISTORY_INTERVAL_SECONDS:
        append_disk_history(settings.state_path, metrics, settings.retention_days)
        last_disk_history = metrics.timestamp
    return last_history, last_disk_history


CONFIG_REQUEST_MAX_AGE_SECONDS = 600


def apply_config_request(settings: Settings, shared_dir: Path = SHARED_DIR,
                         now: float | None = None) -> dict | None:
    """Apply a dashboard config change written by the service controller.

    The guard owns config.toml, so the change is validated and written here
    with the same rules as `wrg config set`. Each request id is handled once;
    the outcome is saved for the dashboard to show.
    """
    try:
        request = json.loads((shared_dir / "config-request.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict) or not isinstance(request.get("id"), str):
        return None
    result_path = settings.state_path / "config-request-result.json"
    if load_state(result_path).get("id") == request["id"]:
        return None
    current = time.time() if now is None else now
    key = request.get("key")
    result: dict[str, object] = {"id": request["id"], "key": key, "applied_at": current}
    try:
        if key not in CONFIG_RULES or CONFIG_RULES[key][0] not in WEB_EDITABLE_KINDS:
            raise ValueError("웹에서 바꿀 수 없는 설정입니다.")
        if current - float(request.get("requested_at") or 0) > CONFIG_REQUEST_MAX_AGE_SECONDS:
            raise ValueError("요청이 10분 넘게 처리되지 않아 만료됐습니다.")
        value = coerce_config_value(key, request.get("value"))
        check_relations(Settings.load(settings.config_path), key, value)
        write_config_value(settings.config_path, key, value)
        result.update(ok=True, value=value, message=f"{key} = {value!r} 적용")
    except (ValueError, TypeError, OSError) as exc:
        result.update(ok=False, message=str(exc))
    save_state(result_path, result)
    print(f"config request id={request['id']} key={key} ok={result['ok']}", flush=True)
    return result


def weekly_report_slot(now: float, last_slot: str, settings: Settings) -> str | None:
    """ISO week slot when the weekly report is due now and not yet sent."""
    if not settings.weekly_report_enabled:
        return None
    local = datetime.fromtimestamp(now).astimezone()
    if local.weekday() != settings.weekly_report_weekday or local.hour < settings.weekly_report_hour:
        return None
    year, week, _ = local.isocalendar()
    slot = f"{year}-W{week:02d}"
    return slot if slot != last_slot else None


def send_weekly_report(settings: Settings, now: float | None = None) -> bool:
    """Send this week's report once; the attempt is recorded even on failure."""
    from .reporting import build_weekly_report
    current = time.time() if now is None else now
    path = settings.state_path / "weekly-report.json"
    slot = weekly_report_slot(current, str(load_state(path).get("slot", "")), settings)
    if slot is None:
        return False
    title, text, html_message = build_weekly_report(settings.state_path, current)
    results = Notifier(settings).send(title, text, "report", channels={"gmail", "discord", "webhook"},
                                      html_message=html_message)
    _log_notification_results(results)
    save_state(path, {"slot": slot, "sent_at": current,
                      "results": {r.channel: r.detail for r in results}})
    return True


def _config_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _log_config_warnings(settings: Settings) -> None:
    for warning in settings.load_warnings:
        print(f"config warning {warning}", flush=True)


@contextmanager
def state_writer_lock(state_path: Path):
    """Lock the inode for the writer lifetime; stale PID text is informational."""
    state_path.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_path / "daemon.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("daemon lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"wsl-resource-guard already running for {state_path}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(fd)


def _reload_settings(active: Settings, proposed: Settings) -> Settings:
    if proposed.state_path.resolve() != active.state_path.resolve():
        print("config warning state_dir change requires restart; keeping locked directory", flush=True)
        return replace(proposed, state_dir=active.state_dir)
    return proposed


def run_forever(settings: Settings) -> None:
    with state_writer_lock(settings.state_path):
        _run_locked(settings)


def _run_locked(settings: Settings) -> None:
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_history = last_disk_history = 0.0
    last_logged_at: float | None = None
    last_logged_severity: str | None = None
    config_mtime = _config_mtime(settings.config_path)
    print(f"wsl-resource-guard started interval={settings.interval_seconds}s", flush=True)
    _log_config_warnings(settings)
    while not stopping:
        try:
            apply_config_request(settings)
        except Exception as exc:  # A bad request must never stop monitoring.
            print(f"config request error={type(exc).__name__}: {exc}", flush=True)
        latest_mtime = _config_mtime(settings.config_path)
        if latest_mtime != config_mtime:
            # Keep the old settings on a parse failure; the next edit retries.
            config_mtime = latest_mtime
            try:
                settings = _reload_settings(settings, Settings.load(settings.config_path))
                print("config reloaded", flush=True)
                _log_config_warnings(settings)
            except Exception as exc:
                print(f"config reload error={type(exc).__name__}: {exc}", flush=True)
        started = time.monotonic()
        try:
            metrics, snapshot, evaluation = sample(settings, notify=True)
            last_history, last_disk_history = record_history(
                settings, metrics, snapshot, evaluation, last_history, last_disk_history
            )
            try:
                send_weekly_report(settings, metrics.timestamp)
            except Exception as exc:
                print(f"weekly report error={type(exc).__name__}: {exc}", flush=True)
            if should_log_sample(
                evaluation.severity, last_logged_severity, time.time(), last_logged_at
            ):
                print(
                    f"sample severity={evaluation.severity} available_gib={metrics.mem_available_gib:.2f} "
                    f"swap_gib={metrics.swap_used_gib:.2f} sessions={len(snapshot.sessions)} "
                    f"mcp_trees={snapshot.mcp_count} mcp_processes={snapshot.mcp_process_count}",
                    flush=True,
                )
                last_logged_at = time.time()
                last_logged_severity = evaluation.severity
        except Exception as exc:  # The service must survive transient /proc races.
            print(f"sample error={type(exc).__name__}: {exc}", flush=True)
        remaining = max(0.0, settings.interval_seconds - (time.monotonic() - started))
        end = time.monotonic() + remaining
        while not stopping and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))
    print("wsl-resource-guard stopped", flush=True)
