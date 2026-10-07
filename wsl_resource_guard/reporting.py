from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
from pathlib import Path

from .metrics import KIB_PER_GIB, SystemMetrics, psi_text, psi_status
from .disks import disk_summary_lines
from .processes import ProcessSnapshot


@dataclass(frozen=True, slots=True)
class ResourceRow:
    kind: str
    name: str
    identifier: str
    project: str
    memory_kib: int
    swap_kib: int
    process_count: int
    mcp_count: int
    age_seconds: float | None
    cgroup: str


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def collect_resource_rows(
    snapshot: ProcessSnapshot,
    limit: int = 10,
    service_root: Path = Path("/sys/fs/cgroup/system.slice"),
) -> list[ResourceRow]:
    rows: list[ResourceRow] = []
    represented_cgroups: set[str] = set()
    for session in snapshot.sessions:
        if session.cgroup:
            represented_cgroups.add(session.cgroup)
        service_name = ""
        if session.cgroup.startswith("/system.slice/") and session.cgroup.endswith(".service"):
            service_name = Path(session.cgroup).name
        rows.append(
            ResourceRow(
                kind="LLM 서비스" if service_name else "LLM 프로세스",
                name=session.provider,
                identifier=service_name or f"PID {session.root_pid}",
                project=session.project,
                memory_kib=session.rss_kib,
                swap_kib=session.swap_kib,
                process_count=session.process_count,
                mcp_count=session.mcp_count,
                age_seconds=session.age_seconds,
                cgroup=session.cgroup,
            )
        )

    if service_root.is_dir():
        for service_dir in service_root.glob("*.service"):
            cgroup = f"/system.slice/{service_dir.name}"
            if cgroup in represented_cgroups:
                continue
            memory_bytes = _read_int(service_dir / "memory.current")
            if memory_bytes <= 0:
                continue
            swap_bytes = _read_int(service_dir / "memory.swap.current")
            process_count = _read_int(service_dir / "pids.current")
            try:
                pids = [
                    int(value)
                    for value in (service_dir / "cgroup.procs").read_text(encoding="utf-8").split()
                ]
            except (OSError, ValueError):
                pids = []
            rows.append(
                ResourceRow(
                    kind="systemd 서비스",
                    name=service_dir.name.removesuffix(".service"),
                    identifier=service_dir.name + (f" · PID {min(pids)}" if pids else ""),
                    project="-",
                    memory_kib=memory_bytes // 1024,
                    swap_kib=swap_bytes // 1024,
                    process_count=process_count,
                    mcp_count=0,
                    age_seconds=None,
                    cgroup=cgroup,
                )
            )
    rows.sort(key=lambda row: (row.memory_kib, row.swap_kib), reverse=True)
    return rows[:limit]


def _gib(kib: int) -> str:
    return f"{kib / KIB_PER_GIB:.2f} GiB"


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total_minutes = int(seconds // 60)
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def build_text_report(
    metrics: SystemMetrics,
    snapshot: ProcessSnapshot,
    severity: str,
    reasons: list[str],
    observations: list[str],
    pending_reasons: list[str],
    rows: list[ResourceRow] | None = None,
) -> str:
    resources = rows if rows is not None else collect_resource_rows(snapshot)
    memory_used_kib = max(0, metrics.mem_total_kib - metrics.mem_available_kib)
    checked_at = datetime.fromtimestamp(metrics.timestamp).astimezone()
    lines = [
        f"WSL RESOURCE GUARD · {severity.upper()}",
        f"확인 시각: {checked_at:%Y-%m-%d %H:%M:%S %Z}",
        "",
        "[자원 요약]",
        f"RAM   {_gib(memory_used_kib)} / {_gib(metrics.mem_total_kib)} 사용 "
        f"(가용 {_gib(metrics.mem_available_kib)})",
        f"Swap  {_gib(metrics.swap_used_kib)} / {_gib(metrics.swap_total_kib)} 사용",
        f"PSI   some / full {psi_text(metrics)} (60초)",
        f"Swap-out {metrics.swap_out_mib_per_minute:.1f} MiB/min",
        f"LLM 세션 {len(snapshot.sessions)}개 · MCP 트리 {snapshot.mcp_count}개 / "
        f"프로세스 {snapshot.mcp_process_count}개 · MCP RSS {_gib(snapshot.mcp_rss_kib)}",
    ]
    if metrics.disks:
        lines.extend(["", "[디스크 사용량]", *disk_summary_lines(metrics.disks)])
    if reasons:
        lines.extend(["", "[경보 원인]", *[f"- {reason}" for reason in reasons]])
    if observations:
        lines.extend(["", "[관찰 항목 · 알림 조건 아님]", *[f"- {item}" for item in observations]])
    if pending_reasons:
        lines.extend(["", "[판정 대기]", *[f"- {item}" for item in pending_reasons]])
    lines.extend(["", "[메모리 사용 Top 10]"])
    if not resources:
        lines.append("- 표시할 세션 또는 서비스가 없습니다.")
    for index, row in enumerate(resources, start=1):
        mcp = f" · MCP {row.mcp_count}" if row.mcp_count else ""
        lines.append(
            f"{index}. {row.kind} · {row.name} · {row.identifier} · {row.project}\n"
            f"   RAM {_gib(row.memory_kib)} · Swap {_gib(row.swap_kib)} · "
            f"프로세스 {row.process_count}{mcp} · 실행 {_age(row.age_seconds)}\n"
            f"   cgroup {row.cgroup or '-'}"
        )
    lines.extend(["", "확인 명령: wrg status · wrg sessions --projects · wrg mcp"])
    return "\n".join(lines)


def build_html_report(
    metrics: SystemMetrics,
    snapshot: ProcessSnapshot,
    severity: str,
    reasons: list[str],
    observations: list[str],
    pending_reasons: list[str],
    rows: list[ResourceRow] | None = None,
) -> str:
    resources = rows if rows is not None else collect_resource_rows(snapshot)
    memory_used_kib = max(0, metrics.mem_total_kib - metrics.mem_available_kib)
    checked_at = datetime.fromtimestamp(metrics.timestamp).astimezone()
    palette = {
        "normal": ("#166534", "#dcfce7"),
        "recovery": ("#166534", "#dcfce7"),
        "warning": ("#92400e", "#fef3c7"),
        "critical": ("#991b1b", "#fee2e2"),
    }
    foreground, background = palette.get(severity, ("#1e3a8a", "#dbeafe"))

    def escaped(value: object) -> str:
        return html.escape(str(value), quote=True)

    def bullet_section(title: str, values: list[str], color: str) -> str:
        if not values:
            return ""
        items = "".join(f"<li style='margin:4px 0'>{escaped(value)}</li>" for value in values)
        return (
            f"<div style='margin:18px 0;padding:12px 16px;border-left:4px solid {color};"
            "background:#f8fafc;border-radius:6px'>"
            f"<strong>{escaped(title)}</strong><ul style='margin:8px 0 0 18px;padding:0'>{items}</ul></div>"
        )

    table_rows = []
    for index, row in enumerate(resources, start=1):
        table_rows.append(
            "<tr>"
            f"<td>{index}</td><td><strong>{escaped(row.name)}</strong><br>"
            f"<span style='color:#64748b'>{escaped(row.kind)}</span></td>"
            f"<td>{escaped(row.identifier)}<br><span style='color:#64748b'>{escaped(row.cgroup or '-')}</span></td>"
            f"<td>{escaped(row.project)}</td><td style='white-space:nowrap'>{escaped(_gib(row.memory_kib))}</td>"
            f"<td style='white-space:nowrap'>{escaped(_gib(row.swap_kib))}</td>"
            f"<td>{row.process_count}</td><td>{row.mcp_count}</td><td>{escaped(_age(row.age_seconds))}</td>"
            "</tr>"
        )
    if not table_rows:
        table_rows.append("<tr><td colspan='9'>표시할 세션 또는 서비스가 없습니다.</td></tr>")

    return f"""<!doctype html>
<html><body style="margin:0;background:#f1f5f9;color:#0f172a;font-family:Segoe UI,Arial,sans-serif">
<div style="max-width:980px;margin:0 auto;padding:24px">
  <div style="background:#ffffff;border-radius:12px;padding:22px;box-shadow:0 1px 3px rgba(15,23,42,.12)">
    <div style="display:inline-block;padding:6px 10px;border-radius:999px;background:{background};color:{foreground};font-weight:700">
      {escaped(severity.upper())}
    </div>
    <h1 style="font-size:22px;margin:12px 0 4px">WSL Resource Guard</h1>
    <div style="color:#64748b;font-size:13px">{checked_at:%Y-%m-%d %H:%M:%S %Z}</div>

    <table role="presentation" style="width:100%;border-collapse:separate;border-spacing:8px;margin:16px -8px">
      <tr>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px">
          <div style="color:#64748b;font-size:12px">RAM 사용 / 전체</div>
          <div style="font-size:19px;font-weight:700">{_gib(memory_used_kib)} / {_gib(metrics.mem_total_kib)}</div>
          <div style="font-size:12px;color:#64748b">가용 {_gib(metrics.mem_available_kib)}</div>
        </td>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px">
          <div style="color:#64748b;font-size:12px">Swap 사용 / 전체</div>
          <div style="font-size:19px;font-weight:700">{_gib(metrics.swap_used_kib)} / {_gib(metrics.swap_total_kib)}</div>
          <div style="font-size:12px;color:#64748b">Swap-out {metrics.swap_out_mib_per_minute:.1f} MiB/min</div>
        </td>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px">
          <div style="color:#64748b;font-size:12px">Memory PSI avg60</div>
          <div style="font-size:19px;font-weight:700">{html.escape(psi_text(metrics))}</div>
          <div style="font-size:12px;color:#64748b">some / full</div>
        </td>
      </tr>
    </table>

    <div style="color:#475569;font-size:13px">LLM 세션 {len(snapshot.sessions)}개 · MCP 트리 {snapshot.mcp_count}개 / 프로세스 {snapshot.mcp_process_count}개 · MCP RSS {_gib(snapshot.mcp_rss_kib)}</div>
    {bullet_section('경보 원인', reasons, '#dc2626')}
    {bullet_section('디스크 사용량', disk_summary_lines(metrics.disks), '#0f766e')}
    {bullet_section('관찰 항목 · 알림 조건 아님', observations, '#2563eb')}
    {bullet_section('판정 대기', pending_reasons, '#d97706')}

    <h2 style="font-size:17px;margin:24px 0 10px">메모리 사용 Top 10</h2>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead><tr style="background:#e2e8f0;text-align:left">
          <th>#</th><th>종류 / 이름</th><th>PID 또는 서비스 / cgroup</th><th>프로젝트</th>
          <th>RAM</th><th>Swap</th><th>프로세스</th><th>MCP</th><th>실행</th>
        </tr></thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table>
    </div>
    <div style="margin-top:18px;padding-top:12px;border-top:1px solid #e2e8f0;color:#64748b;font-size:12px">
      확인: <code>wrg status</code> · <code>wrg sessions --projects</code> · <code>wrg mcp</code>
    </div>
  </div>
</div>
<style>th,td{{padding:9px 7px;border-bottom:1px solid #e2e8f0;vertical-align:top}}</style>
</body></html>"""


def build_weekly_report(state_path: Path, now: float) -> tuple[str, str, str]:
    """(title, text, html) summarising the last 7 days from the guard's files.

    Values that were not recorded are reported as missing, never as zero.
    """
    from .disks import daily_disk_records
    from .history import (alert_episodes, read_history_downsampled, reason_summary_7d,
                          session_history)

    since = now - 7 * 86400
    rows = read_history_downsampled(state_path, since, 3600)
    episodes = [e for e in alert_episodes(
        read_history_downsampled(state_path, since, 300), now=now) if e['start'] >= since]
    causes = reason_summary_7d(episodes, now=now)[:3]
    sessions = sorted(session_history(state_path, since, now=now),
                      key=lambda r: -r['peak_rss_kib'])[:5]
    metrics = [r['metrics'] for r in rows if isinstance(r.get('metrics'), dict)]

    def extreme(fn, key):
        values = [m[key] for m in metrics if isinstance(m.get(key), (int, float))]
        return fn(values) if values else None

    lowest_ram = extreme(min, 'mem_available_kib')
    peak_psi = extreme(max, 'psi_some_avg60')
    psi_incomplete = any(psi_status(m) != 'normal' for m in metrics)
    psi_peak_label = (f"최고 PSI some {peak_psi:.2f}%" +
                      (" (일부 관측 또는 과거 수집 상태 미확인 포함)" if psi_incomplete else "")
                      if peak_psi is not None else "PSI 관측 불가 · 기록된 수치 없음")
    peak_swap_out = extreme(max, 'swap_out_mib_per_minute')
    disks = daily_disk_records(state_path, 8)
    disk_lines = []
    if len(disks) >= 2:
        first = {d.get('id'): d for d in disks[0].get('disks', [])}
        for disk in disks[-1].get('disks', []):
            old = first.get(disk.get('id'))
            if disk.get('kind') != 'windows' or not old:
                continue
            now_free, old_free = disk.get('available_bytes'), old.get('available_bytes')
            if isinstance(now_free, (int, float)) and isinstance(old_free, (int, float)):
                delta = (now_free - old_free) / 2**30
                change = '변화 없음' if abs(delta) < 0.05 else f"{'+' if delta > 0 else ''}{delta:.1f} GiB"
                disk_lines.append(
                    f"{disk['id']} 여유 {now_free / 2**30:.1f} GiB ({change}, {disks[0]['date']} 대비)")
    total = sum(e['duration_seconds'] for e in episodes)
    sections = [
        ('경보', [f"{len(episodes)}회 · 누적 {total / 3600:.1f}시간"]
         + [f"{c['label']} — {c['episodes']}회" for c in causes]),
        ('메모리', [
            f"최저 가용 RAM {lowest_ram / KIB_PER_GIB:.1f} GiB" if lowest_ram is not None else "가용 RAM 기록 없음",
            psi_peak_label,
            f"최고 swap-out {peak_swap_out:.0f} MiB/분" if peak_swap_out is not None else "swap 기록 없음",
        ]),
        ('메모리를 가장 많이 쓴 세션', [
            f"{r['provider']} · {r['project']} — 최대 {r['peak_rss_kib'] / KIB_PER_GIB:.2f} GiB, "
            f"{r['duration_seconds'] / 3600:.1f}시간{' (실행 중)' if not r['ended'] else ''}"
            for r in sessions] or ["기록된 세션 없음"]),
        ('디스크', disk_lines or ["비교할 일별 디스크 기록이 부족합니다"]),
    ]
    start = datetime.fromtimestamp(since).strftime('%m-%d')
    end = datetime.fromtimestamp(now).strftime('%m-%d')
    title = f"WSL 주간 리포트 · {start} ~ {end}"
    text = "\n".join(f"[{name}]\n" + "\n".join(f"- {line}" for line in lines) + "\n"
                     for name, lines in sections).strip()
    body = "".join(
        f"<h3>{html.escape(name)}</h3><ul>"
        + "".join(f"<li>{html.escape(line)}</li>" for line in lines) + "</ul>"
        for name, lines in sections)
    return title, text, f"<html><body><h2>{html.escape(title)}</h2>{body}</body></html>"
