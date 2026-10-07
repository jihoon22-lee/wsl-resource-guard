from __future__ import annotations

import argparse
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

from . import __version__
from .build_info import WEB_PACKAGE, compare_stamps, read_stamp
from .config import (CONFIG_RULES, Settings, _toml_literal, check_relations, load_daemon_settings,
                     parse_config_value, write_config_value)
from .daemon import (
    _event_reports,
    _event_title,
    load_state,
    next_email_heartbeat_at,
    run_forever,
    sample,
    state_writer_lock,
)
from .metrics import KIB_PER_GIB, psi_text
from .notifications import Notifier
from .processes import (
    apply_cpu_rates,
    build_snapshot,
    descendants_of,
    is_stale_session,
    process_start_ticks,
    signal_verified_process,
)
from .services import ACTIONS, cmd_services, cmd_web
from .disks import (
    daily_disk_records,
    disk_insights,
    read_disks,
    disk_severity,
    disk_summary_lines,
    recent_disk_records,
)


def _gib(kib: int) -> str:
    return f"{kib / KIB_PER_GIB:.2f} GiB"


def _age(seconds: float) -> str:
    hours = int(seconds // 3600)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    minutes = int(seconds % 3600 // 60)
    return f"{hours}h {minutes}m"


def _config_path(args: argparse.Namespace) -> Path | None:
    return Path(args.config).expanduser() if getattr(args, "config", None) else None


def _load_settings(args: argparse.Namespace) -> Settings:
    return Settings.load(_config_path(args))


def _apply_cpu_rates(settings: Settings, snapshot) -> None:
    state = load_state(settings.state_path / "state.json")
    apply_cpu_rates(snapshot, state.get("cpu_jiffies", {}), time.time(), os.sysconf("SC_CLK_TCK"))


def _channel_errors(settings: Settings) -> dict:
    raw = load_state(settings.state_path / "state.json").get("last_channel_errors", {})
    return dict(raw) if isinstance(raw, dict) else {}


def cmd_status(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    metrics, snapshot, evaluation = sample(settings, notify=False, persist=False)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "severity": evaluation.severity,
                    "metrics": metrics.to_dict(),
                    "reasons": evaluation.reasons,
                    "observations": evaluation.observations,
                    "pending_reasons": evaluation.pending_reasons,
                    "sessions": [session.to_dict() for session in snapshot.sessions],
                    "mcp": [group.to_dict() for group in snapshot.mcp_groups],
                    "mcp_count": snapshot.mcp_count,
                    "mcp_process_count": snapshot.mcp_process_count,
                    "mcp_rss_kib": snapshot.mcp_rss_kib,
                    "alerts": {
                        "channels": Notifier(settings).channel_status(),
                        "channel_errors": _channel_errors(settings),
                        "last_alert": load_state(settings.state_path / "state.json").get("last_alert"),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"상태              {evaluation.severity}")
    print(f"가용 RAM          {metrics.mem_available_gib:.2f} GiB / {metrics.mem_total_kib / KIB_PER_GIB:.2f} GiB")
    print(f"Swap 사용         {metrics.swap_used_gib:.2f} GiB / {metrics.swap_total_kib / KIB_PER_GIB:.2f} GiB")
    print(f"PSI some/full     {psi_text(metrics)}")
    print(f"Swap in/out       {metrics.swap_in_mib_per_minute:.1f} / {metrics.swap_out_mib_per_minute:.1f} MiB/min")
    if metrics.vmmem_bytes is not None:
        observed = datetime.fromtimestamp(metrics.vmmem_observed_at).astimezone()
        print(f"호스트 vmmem      {metrics.vmmem_bytes / 2**30:.2f} GiB (관측 {observed:%H:%M:%S})")
    print(f"LLM 세션          {len(snapshot.sessions)}개")
    print(
        f"MCP 계열          트리 {snapshot.mcp_count}개 / "
        f"프로세스 {snapshot.mcp_process_count}개, {_gib(snapshot.mcp_rss_kib)}"
    )
    if evaluation.reasons:
        print("경보 원인          " + "; ".join(evaluation.reasons))
    if evaluation.observations:
        print("관찰 항목          " + "; ".join(evaluation.observations))
    if evaluation.pending_reasons:
        print("판정 대기          " + "; ".join(evaluation.pending_reasons))
    print("디스크")
    for line in disk_summary_lines(metrics.disks):
        print(f"  {line}")
    print("알림")
    for channel, state in Notifier(settings).channel_status().items():
        print(f"  {channel:<16}{state}")
    for channel, detail in _channel_errors(settings).items():
        print(f"  {channel:<16}최근 실패: {detail}")
    current_state = load_state(settings.state_path / "state.json")
    last_email_status = float(current_state.get("last_email_status", 0.0) or 0.0)
    last_email_hour_slot = str(current_state.get("last_email_hour_slot", "") or "")
    heartbeat_state = "enabled" if settings.email_heartbeat_enabled else "disabled"
    if settings.email_heartbeat_enabled:
        print(f"  email heartbeat {heartbeat_state} / 매시 {settings.email_heartbeat_minute:02d}분")
    else:
        print(f"  email heartbeat {heartbeat_state}")
    if last_email_status:
        sent_at = datetime.fromtimestamp(last_email_status).astimezone()
        print(f"  last email       {sent_at:%Y-%m-%d %H:%M:%S %Z}")
    else:
        print("  last email       아직 기록 없음")
    next_email = next_email_heartbeat_at(metrics.timestamp, last_email_hour_slot, settings)
    if next_email is not None:
        print(f"  next heartbeat   {next_email:%Y-%m-%d %H:%M:%S %Z}")
    if snapshot.sessions:
        print("상위 세션")
        for session in snapshot.sessions[:5]:
            print(
                f"  PID {session.root_pid:<8} {session.provider:<9} {_gib(session.rss_kib):>10} "
                f"swap {_gib(session.swap_kib):>10} {session.project}"
            )
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    snapshot = build_snapshot(settings.project_roots)
    _apply_cpu_rates(settings, snapshot)
    rows = snapshot.project_usage[: getattr(args, "limit", 20)]
    if getattr(args, "json", False):
        print(json.dumps([row.to_dict() for row in rows], ensure_ascii=False, indent=2))
        return 0
    print(f"{'PID':>8}  {'PROVIDER':<10} {'PROJECT':<24} {'RSS':>10} {'SWAP':>10} {'CPU%':>6} {'PROCS':>6} {'MCP':>5}")
    for usage in rows:
        print(
            f"{usage.root_pid:>8}  {usage.provider:<10} {usage.project[:24]:<24} "
            f"{_gib(usage.rss_kib):>10} {_gib(usage.swap_kib):>10} "
            f"{usage.cpu_percent:>5.1f}% {usage.process_count:>6} {usage.mcp_count:>5}"
        )
    return 0


def _insight_text(info: dict, monitored: bool = True) -> str:
    parts = []
    delta = info.get("daily_delta_gib")
    if delta is not None:
        if abs(delta) < 0.05:
            parts.append("하루 전과 여유 비슷함")
        else:
            direction = "늘어남" if delta >= 0 else "줄어듦"
            parts.append(f"하루 전보다 여유 {abs(delta):.1f} GiB {direction}")
    if info.get("gib_per_day", 0) > 0.05:
        parts.append(f"소모 {info['gib_per_day']:.1f} GiB/일")
    if monitored and info.get("recent_gib_per_hour"):
        parts.append(f"최근 {info['recent_gib_per_hour']:.1f} GiB/시간 소모")
        def span(hours: float) -> str:
            return f"{hours / 24:.0f}일" if hours > 72 else f"{hours:.0f}시간"
        if info.get("hours_to_warning") is not None:
            parts.append(f"경고까지 약 {span(info['hours_to_warning'])}")
        elif info.get("hours_to_critical") is not None:
            parts.append(f"위험까지 약 {span(info['hours_to_critical'])}")
    elif monitored:
        if info.get("days_to_warning") is not None:
            parts.append(f"경고까지 약 {info['days_to_warning']:.0f}일")
        elif info.get("days_to_critical") is not None:
            parts.append(f"위험까지 약 {info['days_to_critical']:.0f}일")
    if info.get("vhd_reclaim_gib"):
        parts.append(f"VHDX 정리로 약 {info['vhd_reclaim_gib']:.0f} GiB 회수 가능")
    return " · ".join(parts)


def cmd_disks(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    previous = (load_state(settings.state_path / "state.json").get("metrics") or {}).get("disks", [])
    disks = read_disks(settings.disk_drives, settings.wsl_vhd_path, previous, force=True)
    history = daily_disk_records(settings.state_path, settings.retention_days)
    recent = recent_disk_records(settings.state_path) + [{"timestamp": time.time(), "disks": disks}]
    insights = disk_insights(
        disks, history, settings.warning_disk_free_percent, settings.critical_disk_free_percent,
        recent=recent,
    )
    for disk in disks:
        if disk["id"] in insights:
            disk["insight"] = insights[disk["id"]]
    if getattr(args, "json", False):
        print(json.dumps(disks, ensure_ascii=False, indent=2))
        return 0
    for disk, line in zip(disks, disk_summary_lines(disks)):
        level = disk_severity(disk, settings.warning_disk_free_percent, settings.critical_disk_free_percent)
        label = level.upper() if disk["kind"] == "windows" else "표시 전용"
        print(f"{label:<10} {line}")
        if disk.get("vhd", {}).get("file_bytes") is not None:
            vhd = disk["vhd"]
            print(f"  VHDX 파일 {vhd['file_bytes'] / 2**30:.1f} GiB · 저장 드라이브 {vhd['host_drive']} · {vhd['path']}")
        info = insights.get(disk["id"])
        if info:
            print(f"  추세: {_insight_text(info, disk['kind'] == 'windows')}")
    print(f"C/D/E 경보: 여유 {settings.warning_disk_free_percent:g}% 미만 Warning / "
          f"{settings.critical_disk_free_percent:g}% 미만 Critical")
    return 0


def _limit_text(limits: dict) -> str:
    parts = []
    for key, label in (
        ("memory_high", "high"),
        ("memory_max", "max"),
        ("swap_max", "swap"),
        ("tasks_max", "tasks"),
    ):
        if key in limits:
            value = limits[key]
            if value is None:
                parts.append(f"{label}=max")
            elif key == "tasks_max":
                parts.append(f"{label}={value}")
            else:
                parts.append(f"{label}={value / 2**30:.0f}G")
    return " ".join(parts)


def cmd_sessions(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    snapshot = build_snapshot(settings.project_roots)
    _apply_cpu_rates(settings, snapshot)
    sessions = snapshot.sessions
    if getattr(args, "stale", False):
        sessions = [
            session
            for session in sessions
            if is_stale_session(session, settings.stale_session_hours)
        ]
    if getattr(args, "json", False):
        print(json.dumps([session.to_dict() for session in sessions], ensure_ascii=False, indent=2))
        return 0
    print(f"{'ROOT PID':>8}  {'PROVIDER':<10} {'AGE':>8} {'NEWEST':>8} {'RSS':>10} {'SWAP':>10} {'CPU%':>6} {'PROCS':>6} {'MCP':>5}  PROJECT / CGROUP")
    for session in sessions:
        stale = (
            " stale?"
            if is_stale_session(session, settings.stale_session_hours)
            else ""
        )
        limited = f" 제한:{_limit_text(session.limits)}" if session.limits else ""
        print(
            f"{session.root_pid:>8}  {session.provider:<10} {_age(session.age_seconds):>8} "
            f"{_age(session.youngest_process_age_seconds):>8} "
            f"{_gib(session.rss_kib):>10} {_gib(session.swap_kib):>10} "
            f"{session.cpu_percent:>5.1f}% {session.process_count:>6} {session.mcp_count:>5}  "
            f"{session.project} [{session.cgroup}]{stale}{limited}"
        )
        if getattr(args, "projects", False):
            for project in session.projects:
                print(
                    f"          -> {project.project:<24} RSS {_gib(project.rss_kib):>10}, "
                    f"swap {_gib(project.swap_kib):>10}, CPU {project.cpu_percent:.1f}%, MCP {project.mcp_count}"
                )
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    snapshot = build_snapshot(settings.project_roots)
    _apply_cpu_rates(settings, snapshot)
    groups = snapshot.mcp_groups
    if getattr(args, "older_than", None) is not None:
        groups = [group for group in groups if group.age_seconds >= args.older_than * 3600]
    provider_filter = getattr(args, "provider", None)
    if provider_filter:
        groups = [group for group in groups if group.provider == provider_filter]
    project_filter = getattr(args, "project", None)
    if project_filter:
        groups = [group for group in groups if group.project == project_filter]
    groups = groups[: getattr(args, "limit", 50)]
    if getattr(args, "json", False):
        print(json.dumps([group.to_dict() for group in groups], ensure_ascii=False, indent=2))
        return 0
    print(f"{'MCP PID':>8} {'SESSION':>8} {'PROVIDER':<10} {'AGE':>8} {'RSS':>10} {'SWAP':>10} {'CPU%':>6} {'PROCS':>5} {'NAME':<20} PROJECT")
    for group in groups:
        print(
            f"{group.root_pid:>8} {group.session_root_pid:>8} {group.provider:<10} {_age(group.age_seconds):>8} "
            f"{_gib(group.rss_kib):>10} {_gib(group.swap_kib):>10} {group.cpu_percent:>5.1f}% {group.process_count:>5} "
            f"{group.root_name[:20]:<20} {group.project}"
        )
    return 0


def cmd_stop_mcp(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    snapshot = build_snapshot(settings.project_roots)
    group = next((item for item in snapshot.mcp_groups if item.root_pid == args.pid), None)
    if not group:
        print(f"PID {args.pid}는 현재 감지된 MCP 트리 루트가 아닙니다.", file=sys.stderr)
        return 2
    processes = descendants_of(args.pid, snapshot.processes)
    print(
        f"대상 MCP: {group.root_name} PID {group.root_pid}, {group.provider}/{group.project}, "
        f"{_gib(group.rss_kib)}, 프로세스 {group.process_count}개"
    )
    if any(process.pid == os.getpid() for process in processes):
        print("이 관리 명령 자체가 대상 MCP 트리에 포함됩니다. 별도 터미널에서 실행하세요.", file=sys.stderr)
        return 2
    if not args.confirm:
        print(f"종료하려면 `wrg stop-mcp {args.pid} --confirm`을 실행하세요.")
        return 2
    sent = 0
    for process in processes:
        try:
            sent += int(signal_verified_process(process, os.getuid(), signal.SIGTERM))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"PID {process.pid} 종료를 중단했습니다: {exc}", file=sys.stderr)
            return 2
    if sent:
        print(f"MCP 트리의 {sent}개 프로세스에 SIGTERM을 보냈습니다. 부모 LLM이 필요하면 다시 시작할 수 있습니다.")
    else:
        print("대상 MCP 프로세스가 이미 종료되었습니다.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    snapshot = build_snapshot(settings.project_roots)
    if args.pid not in snapshot.provider_for_root:
        print(f"PID {args.pid}는 현재 감지된 LLM 세션 루트가 아닙니다.", file=sys.stderr)
        return 2
    processes = descendants_of(args.pid, snapshot.processes)
    session = next(item for item in snapshot.sessions if item.root_pid == args.pid)
    print(
        f"대상: {session.provider} PID {session.root_pid}, {session.project}, "
        f"{_gib(session.rss_kib)}, 프로세스 {len(processes)}개"
    )
    if any(process.pid == os.getpid() for process in processes):
        print("이 관리 명령 자체가 대상 세션의 자식입니다. 별도 WSL 터미널에서 다시 실행하세요.", file=sys.stderr)
        return 2
    if not args.confirm:
        print(f"종료하려면 별도 터미널에서 `wrg stop {args.pid} --confirm`을 실행하세요.")
        return 2
    # Preserve the original snapshot identity for both TERM and KILL. Re-reading
    # a token and accepting it here could adopt an already reused PID.
    signalled = {}
    for process in processes:
        try:
            if signal_verified_process(process, os.getuid(), signal.SIGTERM):
                signalled[process.pid] = process
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"PID {process.pid} 종료를 중단했습니다: {exc}", file=sys.stderr)
            return 2
    deadline = time.monotonic() + args.timeout
    survivors: list[int] = []
    while True:
        survivors = [
            pid
            for pid, process in signalled.items()
            if process_start_ticks(pid) == process.start_ticks
        ]
        if not survivors or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if survivors and args.kill:
        killed = 0
        for pid in survivors:
            try:
                killed += int(signal_verified_process(signalled[pid], os.getuid(), signal.SIGKILL))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print(f"PID {pid} 강제 종료를 중단했습니다: {exc}", file=sys.stderr)
                return 2
        print(f"SIGTERM 후 남은 프로세스 {killed}개에 SIGKILL을 보냈습니다.")
        return 0
    if survivors:
        print(f"{len(survivors)}개 프로세스가 남았습니다. 강제 종료하려면 `--kill`을 추가하세요.", file=sys.stderr)
        return 1
    print("세션 트리가 정상 종료되었습니다.")
    return 0


def _prompt_value(label: str, current: str, secret: bool = False) -> str:
    suffix = " [저장됨, Enter=유지, -=삭제]" if current else " [Enter=건너뜀]"
    prompt = label + suffix + ": "
    value = getpass.getpass(prompt) if secret else input(prompt)
    if value == "-":
        return ""
    if not value:
        return current
    return value.strip()


def cmd_configure_alerts(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    current = settings.load_secrets()
    print("비밀값은 화면에 다시 표시하지 않으며 0600 권한으로 저장합니다.")
    updated = {
        "gmail_user": _prompt_value("Gmail 발신 계정", current.get("gmail_user", "")),
        "gmail_to": _prompt_value("수신 이메일(여러 개는 쉼표 구분)", current.get("gmail_to", "")),
        "gmail_app_password": _prompt_value(
            "Gmail 앱 비밀번호", current.get("gmail_app_password", ""), secret=True
        ).replace(" ", ""),
        "discord_webhook_url": _prompt_value(
            "Discord webhook URL", current.get("discord_webhook_url", ""), secret=True
        ),
        "webhook_url": _prompt_value(
            "범용 webhook URL", current.get("webhook_url", ""), secret=True
        ),
    }
    path = settings.secrets_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".secrets-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(updated, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    print(f"저장 완료: {path} (권한 {oct(path.stat().st_mode & 0o777)})")
    print("전송 검증: wrg test-alert")
    return 0


def cmd_test_alert(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    channels = set(args.channels.split(",")) if args.channels else None
    metrics, snapshot, evaluation = sample(settings, notify=False, persist=False)
    message, html_message = _event_reports(metrics, snapshot, evaluation)
    results = Notifier(settings).send(
        _event_title(metrics, "WSL 보고서 테스트"),
        message,
        evaluation.severity,
        channels,
        html_message,
    )
    failed = sent = False
    for result in results:
        label = "OK" if result.sent else "SKIP" if result.skipped else "FAIL"
        print(f"{result.channel:<16} {label:<4}  {result.detail}")
        sent = sent or result.sent
        failed = failed or not (result.sent or result.skipped)
    if not sent and not failed:
        print("전송한 채널이 없습니다. wrg configure-alerts로 채널을 구성하세요.", file=sys.stderr)
    return 1 if failed or not sent else 0


def cmd_history(args: argparse.Namespace) -> int:
    try:
        return _cmd_history(args)
    except (OSError, ValueError) as exc:
        print(f"이력을 읽지 못했습니다: {exc}", file=sys.stderr)
        return 2


def _cmd_history(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    if getattr(args, "episodes", False):
        from .history import alert_episodes, iter_history
        since = (time.time() - args.hours * 3600 if getattr(args, "hours", None)
                 else time.time() - settings.retention_days * 86400)
        episodes = alert_episodes(
            iter_history(settings.state_path, since,
                         fields=("timestamp", "severity", "reasons")))
        if getattr(args, "json", False):
            print(json.dumps(episodes, ensure_ascii=False, indent=2))
            return 0
        for episode in reversed(episodes):
            start = datetime.fromtimestamp(episode["start"]).astimezone()
            end = ("진행 중" if episode["ongoing"]
                   else datetime.fromtimestamp(episode["end"]).astimezone().strftime("%m-%d %H:%M"))
            minutes = int(episode["duration_seconds"] // 60)
            duration = (f"{minutes // 60}시간 {minutes % 60}분" if minutes >= 60
                        else f"{minutes}분")
            reasons = ", ".join(episode["reasons_top5"]) or "-"
            print(f"{start:%Y-%m-%d %H:%M} → {end} · {episode['worst_severity']:<8} · {duration} · {reasons}")
        return 0
    from .history import read_history
    cutoff = time.time() - args.hours * 3600 if getattr(args, "hours", None) else 0
    records = read_history(settings.state_path, cutoff, fields=None, limit=args.limit,
                           severity=getattr(args, 'severity', None))
    if getattr(args, "json", False):
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return 0
    for record in records:
        metrics = record.get("metrics", {})
        timestamp = datetime.fromtimestamp(float(record.get("timestamp", 0))).astimezone()
        available = float(metrics.get("mem_available_kib", 0)) / KIB_PER_GIB
        swap = (float(metrics.get("swap_total_kib", 0)) - float(metrics.get("swap_free_kib", 0))) / KIB_PER_GIB
        print(f"{timestamp:%Y-%m-%d %H:%M:%S} {record.get('severity', '?'):<8} RAM avail {available:.2f} GiB swap {swap:.2f} GiB")
    return 0


def _probe(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _file_has(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    results: list[tuple[str, str, str]] = []

    def check(name: str, level: str, detail: str = "") -> None:
        results.append((level, name, detail))

    check("WSL kernel", "OK" if _file_has(Path("/proc/sys/kernel/osrelease"), "microsoft") else "FAIL")
    check("cgroup v2", "OK" if Path("/sys/fs/cgroup/cgroup.controllers").exists() else "FAIL")
    check("systemd", "OK" if Path("/run/systemd/system").exists() else "FAIL")
    check("config", "OK" if settings.config_path.exists() else "FAIL", str(settings.config_path))
    if settings.load_warnings:
        check("config values", "WARN", f"{len(settings.load_warnings)}개 값을 무시하고 기본값 사용 — wrg config로 확인")
    check(
        "PowerShell interop",
        "OK" if Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists() else "FAIL",
    )
    settings.state_path.mkdir(parents=True, exist_ok=True)
    check("state directory writable", "OK" if os.access(settings.state_path, os.W_OK) else "FAIL",
          str(settings.state_path))

    configs = sorted(Path("/mnt/c/Users").glob("*/.wslconfig"))
    if configs:
        detail = str(configs[0])
        try:
            text = configs[0].read_text(encoding="utf-8", errors="replace").lower()
            found = [key for key in ("memory", "swap", "automemoryreclaim") if key in text]
            if found:
                detail += f" ({', '.join(found)} 설정)"
        except OSError:
            pass
        check(".wslconfig", "OK", detail)
    else:
        check(".wslconfig", "WARN", "미설정 — README의 memory/swap 권장값을 참고하세요")

    for label, unit_path, command in (
        (
            "guard daemon (system)",
            Path("/etc/systemd/system/wsl-resource-guard.service"),
            ["systemctl", "is-active", "wsl-resource-guard.service"],
        ),
        (
            "guard daemon (user)",
            Path.home() / ".config/systemd/user/wsl-resource-guard.service",
            ["systemctl", "--user", "is-active", "wsl-resource-guard.service"],
        ),
    ):
        state = _probe(command)
        if state == "active":
            check(label, "OK")
        elif state == "failed":
            check(label, "FAIL", "failed")
        elif unit_path.exists():
            check(label, "WARN", state or "installed but not running")
        else:
            check(label, "WARN", "유닛 미설치 — install-user.sh 또는 install-root.sh")

    control_unit = _probe(["systemctl", "is-active", "wrg-service-control.service"])
    socket_ready = Path("/run/wrg-services/control.sock").exists()
    if control_unit == "active" and socket_ready:
        check("service control", "OK", "/run/wrg-services/control.sock")
    elif control_unit == "failed":
        check("service control", "FAIL", "wrg-service-control 실패")
    else:
        check("service control", "WARN", "미설치 — install-services.sh")

    level, detail = compare_stamps(
        read_stamp(Path(__file__).resolve().parent),
        read_stamp(WEB_PACKAGE),
        WEB_PACKAGE.is_dir(),
    )
    check("code copies", level, detail)

    web_unit = _probe(["systemctl", "is-active", "wrg-web.service"])
    check(
        "web dashboard",
        "OK" if web_unit == "active" else ("FAIL" if web_unit == "failed" else "WARN"),
        "" if web_unit == "active" else (web_unit or "미설치 — install-services.sh"),
    )

    try:
        tailscale_ok = (
            subprocess.run(
                ["tailscale", "status", "--json"], capture_output=True, timeout=5
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        tailscale_ok = False
    check("Tailscale", "OK" if tailscale_ok else "WARN", "" if tailscale_ok else "연결 또는 설치 확인")

    for level, name, detail in results:
        print(f"{level:<5} {name}" + (f" — {detail}" if detail else ""))
    return 1 if any(level == "FAIL" for level, _, _ in results) else 0


def cmd_config(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    if getattr(args, "config_action", None) != "set":
        print(f"설정 파일: {settings.config_path}")
        for warning in settings.load_warnings:
            print(f"  경고: {warning}", file=sys.stderr)
        for key, (kind, low, high, desc) in CONFIG_RULES.items():
            hint = f" ({low:g}~{high:g})" if kind in ("int", "float") else ""
            print(f"  {key:<46} {getattr(settings, key)!r:<12} {desc}{hint}")
        print("\n변경: wrg config set <키> <값>  (실행 중인 데몬이 자동으로 다시 읽습니다)")
        return 0
    key, raw = args.key, args.value
    if key not in CONFIG_RULES:
        print(f"알 수 없는 설정입니다: {key}\n설정 가능한 키는 `wrg config`로 확인하세요.", file=sys.stderr)
        return 2
    try:
        value = parse_config_value(key, raw)
    except ValueError as exc:
        print(f"{key}: {exc}", file=sys.stderr)
        return 2
    try:
        check_relations(settings, key, value)
        write_config_value(settings.config_path, key, value)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    path = settings.config_path
    print(f"{key} = {_toml_literal(value)} — {path}에 저장했습니다. 데몬이 자동으로 다시 읽습니다.")
    return 0


def _valid_limit(value: str) -> bool:
    return bool(re.fullmatch(r"(?:\d+(?:\.\d+)?)[KMGT]", value, flags=re.IGNORECASE))


def cmd_launch(args: argparse.Namespace) -> int:
    if not args.command:
        print("실행할 명령이 없습니다. `--` 뒤에 명령을 지정하세요.", file=sys.stderr)
        return 2
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        print(f"프로젝트 디렉터리가 없습니다: {project}", file=sys.stderr)
        return 2
    for value in (args.memory_high, args.memory_max, args.swap_max):
        if not _valid_limit(value):
            print(f"잘못된 systemd 용량 값: {value}", file=sys.stderr)
            return 2
    safe_project = re.sub(r"[^a-zA-Z0-9]+", "-", project.name).strip("-").lower() or "project"
    safe_provider = re.sub(r"[^a-zA-Z0-9]+", "-", args.provider).strip("-").lower() or "llm"
    unit = f"wrg-{safe_provider}-{safe_project}-{int(time.time())}"
    command = [
        "systemd-run",
        "--user",
        "--scope",
        "--collect",
        "--same-dir",
        f"--unit={unit}",
        f"--description=LLM session {args.provider} for {project.name}",
        f"--property=MemoryHigh={args.memory_high}",
        f"--property=MemoryMax={args.memory_max}",
        f"--property=MemorySwapMax={args.swap_max}",
        f"--property=TasksMax={args.tasks_max}",
        "--",
        *args.command,
    ]
    print(f"scope={unit}.scope project={project}")
    return subprocess.call(command, cwd=project)


def cmd_weekly_report(args: argparse.Namespace) -> int:
    from .reporting import build_weekly_report
    settings = _load_settings(args)
    title, text, html_message = build_weekly_report(settings.state_path, time.time())
    print(title)
    print(text)
    if not args.send:
        print("\n미리보기입니다. 실제 발송: wrg weekly-report --send")
        return 0
    results = Notifier(settings).send(title, text, "report", channels={"gmail", "discord", "webhook"},
                                      html_message=html_message)
    for result in results:
        print(f"{result.channel:<8} {'SENT' if result.sent else 'SKIP' if result.skipped else 'FAIL'} {result.detail}")
    return 0 if all(r.sent or r.skipped for r in results) else 1


def cmd_once(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    with state_writer_lock(settings.state_path):
        sample(settings, notify=args.notify)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wrg", description="WSL memory and LLM session resource guard")
    parser.add_argument("--config", help="config.toml path")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the monitoring daemon")
    run_parser.set_defaults(func=lambda args: (run_forever(load_daemon_settings(_config_path(args))) or 0))

    once_parser = subparsers.add_parser("once", help="take one sample")
    once_parser.add_argument("--notify", action="store_true")
    once_parser.set_defaults(func=cmd_once)

    status_parser = subparsers.add_parser("status", help="show current health")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    disks_parser = subparsers.add_parser("disks", help="show Windows volume and WSL disk usage")
    disks_parser.add_argument("--json", action="store_true")
    disks_parser.set_defaults(func=cmd_disks)

    services_parser = subparsers.add_parser("services", help="manage WSL services and Compose projects")
    services_parser.add_argument("action", nargs="?", choices=ACTIONS, help="omit for interactive menu")
    services_parser.add_argument("service", nargs="?", help="service ID, or discovery key for register")
    services_parser.add_argument("--confirm", action="store_true", help="confirm foundation changes or unregister")
    services_parser.add_argument("--name", help="display name when registering a service")
    services_parser.add_argument("--url", help="HTTP(S) shortcut when registering a service")
    services_parser.add_argument("--json", action="store_true", help="show structured service status")
    services_parser.add_argument("--lines", type=int, default=80, help="log lines for the logs action")
    services_parser.add_argument("--dry-run", action="store_true", help="show the restore plan without changes")
    services_parser.set_defaults(func=cmd_services)

    web_parser = subparsers.add_parser("web", help="show the private Tailscale dashboard URL")
    web_parser.set_defaults(func=cmd_web)

    top_parser = subparsers.add_parser("top", help="show project/provider usage")
    top_parser.add_argument("--limit", type=int, default=20)
    top_parser.add_argument("--json", action="store_true")
    top_parser.set_defaults(func=cmd_top)

    sessions_parser = subparsers.add_parser("sessions", help="show LLM process trees")
    sessions_parser.add_argument("--projects", action="store_true", help="show per-project breakdown")
    sessions_parser.add_argument("--stale", action="store_true", help="only show stale candidates")
    sessions_parser.add_argument("--json", action="store_true")
    sessions_parser.set_defaults(func=cmd_sessions)

    mcp_parser = subparsers.add_parser("mcp", help="show MCP-related processes")
    mcp_parser.add_argument("--limit", type=int, default=50)
    mcp_parser.add_argument("--older-than", type=float, help="only show MCP trees older than this many hours")
    mcp_parser.add_argument("--provider")
    mcp_parser.add_argument("--project")
    mcp_parser.add_argument("--json", action="store_true")
    mcp_parser.set_defaults(func=cmd_mcp)

    stop_mcp_parser = subparsers.add_parser("stop-mcp", help="stop one MCP server tree")
    stop_mcp_parser.add_argument("pid", type=int)
    stop_mcp_parser.add_argument("--confirm", action="store_true")
    stop_mcp_parser.set_defaults(func=cmd_stop_mcp)

    stop_parser = subparsers.add_parser("stop", help="safely stop an LLM session tree")
    stop_parser.add_argument("pid", type=int)
    stop_parser.add_argument("--confirm", action="store_true")
    stop_parser.add_argument("--timeout", type=float, default=10.0)
    stop_parser.add_argument("--kill", action="store_true", help="SIGKILL survivors after timeout")
    stop_parser.set_defaults(func=cmd_stop)

    configure_parser = subparsers.add_parser("configure-alerts", help="securely configure Gmail and Discord")
    configure_parser.set_defaults(func=cmd_configure_alerts)

    test_parser = subparsers.add_parser("test-alert", help="send an explicit test notification")
    test_parser.add_argument("--channels", help="comma-separated: toast,gmail,discord,webhook")
    test_parser.set_defaults(func=cmd_test_alert)
    weekly_parser = subparsers.add_parser("weekly-report", help="preview or send the weekly report")
    weekly_parser.add_argument("--send", action="store_true", help="send through gmail/discord/webhook")
    weekly_parser.set_defaults(func=cmd_weekly_report)

    history_parser = subparsers.add_parser("history", help="show recent resource samples")
    history_parser.add_argument("--limit", type=int, default=20)
    history_parser.add_argument("--episodes", action="store_true",
                                help="show alert episodes instead of raw samples")
    history_parser.add_argument("--severity", choices=("normal", "warning", "critical"))
    history_parser.add_argument("--hours", type=float, help="only samples from the last N hours")
    history_parser.add_argument("--json", action="store_true")
    history_parser.set_defaults(func=cmd_history)

    doctor_parser = subparsers.add_parser("doctor", help="verify runtime prerequisites")
    doctor_parser.set_defaults(func=cmd_doctor)

    config_parser = subparsers.add_parser("config", help="show or adjust validated thresholds")
    config_subparsers = config_parser.add_subparsers(dest="config_action")
    config_set = config_subparsers.add_parser("set", help="set a validated value")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_set.set_defaults(func=cmd_config)
    config_parser.set_defaults(func=cmd_config)

    launch_parser = subparsers.add_parser("launch", help="launch a new LLM session in a memory-limited scope")
    launch_parser.add_argument("--provider", required=True)
    launch_parser.add_argument("--project", default=".")
    launch_parser.add_argument("--memory-high", default="3G")
    launch_parser.add_argument("--memory-max", default="4G")
    launch_parser.add_argument("--swap-max", default="1G")
    launch_parser.add_argument("--tasks-max", default="384")
    launch_parser.add_argument("command", nargs=argparse.REMAINDER)
    launch_parser.set_defaults(func=cmd_launch)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) and isinstance(args.command, list) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
