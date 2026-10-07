from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
import os
import errno
from pathlib import Path
import pwd
import re
import signal
import subprocess
from typing import Iterable


AGENT_NAMES = {
    "agy": "agy",
    "aider": "aider",
    "antigravity": "agy",
    "claude": "claude",
    "codex": "codex",
    "codex-code-mode": "codex",
    "devin": "devin",
    "gemini": "gemini",
    "opencode": "opencode",
}


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    ppid: int
    uid: int
    name: str
    state: str
    rss_kib: int
    swap_kib: int
    age_seconds: float
    cwd: str
    cgroup: str
    command: str = field(repr=False)
    cpu_jiffies: int = 0
    start_ticks: int | None = None

    @property
    def is_mcp(self) -> bool:
        text = f"{self.name} {self.command}".lower()
        if "wsl_resource_guard" in text or re.search(r"(?:^|/)wrg\s+(?:stop-)?mcp(?:\s|$)", text):
            return False
        markers = (
            "@modelcontextprotocol/",
            "@playwright/mcp",
            "chrome-devtools-mcp",
            "playwright-mcp",
            "mcp-server",
            "server-mcp",
        )
        if any(marker in text for marker in markers):
            return True
        return bool(re.search(r"(^|[ /@_-])mcp($|[ /@_.-])", text))


@dataclass(slots=True)
class ProjectUsage:
    root_pid: int
    provider: str
    project: str
    rss_kib: int = 0
    swap_kib: int = 0
    process_count: int = 0
    mcp_rss_kib: int = 0
    mcp_count: int = 0
    cpu_jiffies: int = 0
    cpu_percent: float = 0.0

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(slots=True)
class SessionUsage:
    root_pid: int
    provider: str
    root_name: str
    project: str
    age_seconds: float
    youngest_process_age_seconds: float = 0.0
    rss_kib: int = 0
    swap_kib: int = 0
    process_count: int = 0
    mcp_rss_kib: int = 0
    mcp_count: int = 0
    cgroup: str = ""
    cpu_jiffies: int = 0
    cpu_percent: float = 0.0
    # Root is a long-running agent server rather than an interactive session.
    persistent: bool = False
    limits: dict[str, int | None] = field(default_factory=dict)
    projects: list[ProjectUsage] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["projects"] = [project.to_dict() for project in self.projects]
        return data


@dataclass(slots=True)
class McpUsage:
    root_pid: int
    session_root_pid: int
    provider: str
    project: str
    root_name: str
    age_seconds: float
    rss_kib: int = 0
    swap_kib: int = 0
    process_count: int = 0
    cpu_jiffies: int = 0
    cpu_percent: float = 0.0

    def to_dict(self) -> dict[str, int | float | str]:
        return asdict(self)


@dataclass(slots=True)
class ProcessSnapshot:
    processes: dict[int, ProcessInfo]
    sessions: list[SessionUsage]
    project_usage: list[ProjectUsage]
    root_for_pid: dict[int, int]
    provider_for_root: dict[int, str]
    mcp_groups: list[McpUsage] = field(default_factory=list)

    @property
    def mcp_rss_kib(self) -> int:
        return sum(group.rss_kib for group in self.mcp_groups)

    @property
    def mcp_count(self) -> int:
        return len(self.mcp_groups)

    @property
    def mcp_process_count(self) -> int:
        return sum(group.process_count for group in self.mcp_groups)


def detect_provider(process: ProcessInfo) -> str | None:
    name = process.name.lower()
    if name in AGENT_NAMES:
        return AGENT_NAMES[name]
    if name.startswith("codex-"):
        return "codex"
    command = process.command.lower()
    if "@google/gemini-cli" in command:
        return "gemini"
    if re.search(r"(^|[/ ])aider($|[ /])", command):
        return "aider"
    persistent = _persistent_provider(name, command)
    if persistent:
        return persistent
    if _launches_program(command, AGY_PROGRAMS):
        return "agy"
    return None


AGY_PROGRAMS = frozenset({"agy", "antigravity"})
_INTERPRETER = re.compile(r"(?:python[\d.]*|node|bun|deno)")


def _persistent_provider(name: str, command: str) -> str | None:
    """Provider of an agent server root that stays up by design, else None.

    devin-web runs the agent under devin-acpd (a node daemon, comm MainThread);
    detecting the daemon makes it the session root, so `devin acp` and the exec
    PTYs it adopts are all attributed to the devin session. Antigravity's
    desktop app runs its agent backend as a language_server from the WSL-side
    .antigravity-server install. `name` and `command` are lowercased.
    """
    if "bin/devin-acpd.mjs" in command:
        return "devin"
    if name == "language_server" and "/.antigravity-server/" in command:
        return "agy"
    return None


def _launches_program(command: str, programs: frozenset[str]) -> bool:
    """True when argv runs one of `programs`, directly or through an interpreter.

    Only the executable (or the interpreter's module/script) counts; a program
    name appearing as an ordinary argument such as `grep agy` or a path like
    `vim ~/projects/antigravity/x` must not make the process an agent session.
    """
    tokens = command.split()
    if not tokens:
        return False
    executable = os.path.basename(tokens[0])
    if executable in programs:
        return True
    if not _INTERPRETER.fullmatch(executable):
        return False
    rest = tokens[1:]
    if len(rest) >= 2 and rest[0] == "-m":
        return rest[1] in programs
    script = next((token for token in rest if not token.startswith("-")), None)
    return script is not None and os.path.basename(script).split(".", 1)[0] in programs


def _read_process(pid: int, clock_ticks: int, uptime: float) -> ProcessInfo | None:
    first_token = process_start_ticks(pid)
    if first_token is None:
        return None
    proc_dir = Path("/proc") / str(pid)
    try:
        status_text = (proc_dir / "status").read_text(encoding="utf-8")
        stat_text = (proc_dir / "stat").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    status: dict[str, str] = {}
    for line in status_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        status[key] = value.strip()
    try:
        uid = int(status.get("Uid", "-1").split()[0])
        ppid = int(status.get("PPid", "0").split()[0])
        rss = int(status.get("VmRSS", "0 kB").split()[0])
        swap = int(status.get("VmSwap", "0 kB").split()[0])
    except (ValueError, IndexError):
        return None

    closing = stat_text.rfind(")")
    fields = stat_text[closing + 2 :].split() if closing >= 0 else []
    try:
        start_ticks = int(fields[19])
    except (ValueError, IndexError):
        start_ticks = 0
    age = max(0.0, uptime - start_ticks / clock_ticks) if start_ticks else 0.0
    try:
        cpu_jiffies = int(fields[11]) + int(fields[12])
    except (ValueError, IndexError):
        cpu_jiffies = 0

    try:
        raw_command = (proc_dir / "cmdline").read_bytes()
        command = raw_command.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        command = ""
    try:
        cwd = os.readlink(proc_dir / "cwd")
    except OSError:
        cwd = ""
    try:
        cgroup = ""
        for line in (proc_dir / "cgroup").read_text(encoding="utf-8").splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                cgroup = parts[2]
                break
    except OSError:
        cgroup = ""

    if first_token != start_ticks or process_start_ticks(pid) != start_ticks:
        return None
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        uid=uid,
        name=status.get("Name", "?"),
        state=status.get("State", "?").split()[0],
        rss_kib=rss,
        swap_kib=swap,
        age_seconds=age,
        cwd=cwd,
        cgroup=cgroup,
        cpu_jiffies=cpu_jiffies,
        command=command,
        start_ticks=start_ticks,
    )


def scan_processes(uid: int | None = None) -> dict[int, ProcessInfo]:
    wanted_uid = os.getuid() if uid is None else uid
    clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        uptime = 0.0
    processes: dict[int, ProcessInfo] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        process = _read_process(int(entry.name), clock_ticks, uptime)
        if process and process.uid == wanted_uid:
            processes[process.pid] = process
    return processes


def process_start_ticks(pid: int) -> int | None:
    """Kernel start-time token for pid; a reused pid yields a different value."""
    try:
        stat_text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    closing = stat_text.rfind(")")
    fields = stat_text[closing + 2 :].split() if closing >= 0 else []
    try:
        return int(fields[19])
    except (ValueError, IndexError):
        return None


SERVICE_KILL_BLOCK = "systemd 관리 서비스는 여기서 종료하지 않습니다. 서비스 화면의 중지를 사용하세요."


def kill_block_reason(root: ProcessInfo | None) -> str:
    """Non-empty when this tree root must not be signalled from session controls."""
    # The nearest owning unit also covers sub-cgroups inside a service, while
    # an interactive scope under user@UID.service remains an interactive scope.
    for component in reversed(((root.cgroup if root else "") or "").split('/')):
        if component.endswith('.scope'):
            return ''
        if component.endswith('.service'):
            return SERVICE_KILL_BLOCK
    return ''


def signal_verified_process(process: ProcessInfo, expected_uid: int, sig: int) -> bool:
    """Signal the snapshot's process instance, never a newly reused numeric PID.

    Root sends through a fixed, unprivileged subprocess so a concurrent setuid
    exec cannot turn an owner operation into a signal to another user's process.
    False means the target exited; identity/policy/permission failures are errors.
    """
    if (process.pid <= 1 or process.pid == os.getpid() or expected_uid <= 0
            or process.uid != expected_uid or not process.start_ticks
            or sig not in (signal.SIGTERM, signal.SIGKILL)):
        raise PermissionError('종료 대상의 소유자·시작 시각을 확인할 수 없습니다.')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise OSError(errno.ENOSYS, '이 환경은 안전한 pidfd 프로세스 종료를 지원하지 않습니다.')
    try:
        fd = os.pidfd_open(process.pid)
    except ProcessLookupError:
        return False
    try:
        current = _read_process(process.pid, os.sysconf('SC_CLK_TCK'), 0)
        if current is None or current.state in ('Z', 'X'):
            return False
        if (current.pid != process.pid or current.uid != expected_uid
                or current.start_ticks != process.start_ticks or not current.cgroup):
            raise PermissionError('종료 대상의 신원이 변경됐거나 확인되지 않습니다.')
        reason = kill_block_reason(process) or kill_block_reason(current)
        if reason:
            raise PermissionError(reason)
        if os.geteuid() == 0:
            account = pwd.getpwuid(expected_uid)
            code = ('import signal,sys\n'
                    'try: signal.pidfd_send_signal(int(sys.argv[1]),int(sys.argv[2]))\n'
                    'except ProcessLookupError: sys.exit(3)\n'
                    'except PermissionError: sys.exit(4)\n')
            result = subprocess.run(['/usr/bin/python3', '-I', '-c', code, str(fd), str(int(sig))],
                                    user=expected_uid, group=account.pw_gid, extra_groups=(),
                                    pass_fds=(fd,), cwd='/', env={'LANG': 'C.UTF-8'},
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=5, check=False)
            if result.returncode == 3:
                return False
            if result.returncode:
                raise PermissionError('소유자 권한으로 프로세스에 신호를 보낼 수 없습니다.')
        else:
            signal.pidfd_send_signal(fd, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(fd)


def is_stale_session(session: "SessionUsage", stale_session_hours: float) -> bool:
    """The single long-idle rule shared by the dashboard, CLI, and monitor API.

    Agent servers (opencode, devin-acpd, the Antigravity language server) stay
    up by design, so their age says nothing about an abandoned session.
    """
    return (
        session.provider != "opencode"
        and not session.persistent
        and session.youngest_process_age_seconds >= stale_session_hours * 3600
    )


def _ancestor_chain(pid: int, processes: dict[int, ProcessInfo]) -> Iterable[int]:
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        seen.add(current)
        process = processes.get(current)
        if not process:
            return
        current = process.ppid
        if current in processes:
            yield current


def _session_roots(processes: dict[int, ProcessInfo]) -> dict[int, str]:
    roots: dict[int, str] = {}
    for pid, process in processes.items():
        provider = detect_provider(process)
        if not provider:
            continue
        if any(detect_provider(processes[ancestor]) for ancestor in _ancestor_chain(pid, processes)):
            continue
        roots[pid] = provider
    return roots


def _root_assignments(processes: dict[int, ProcessInfo], roots: dict[int, str]) -> dict[int, int]:
    assignments: dict[int, int] = {}
    root_ids = set(roots)
    for pid in processes:
        current = pid
        seen: set[int] = set()
        while current > 1 and current not in seen:
            if current in root_ids:
                assignments[pid] = current
                break
            seen.add(current)
            process = processes.get(current)
            if not process:
                break
            current = process.ppid
    return assignments


def project_for_cwd(cwd: str, project_roots: list[str]) -> str | None:
    if not cwd:
        return None
    path = Path(cwd)
    for raw_root in project_roots:
        root = Path(raw_root).expanduser()
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            return "(workspace)"
        return relative.parts[0]
    return None


def _project_for_process(
    process: ProcessInfo,
    root_pid: int,
    processes: dict[int, ProcessInfo],
    project_roots: list[str],
) -> str:
    direct = project_for_cwd(process.cwd, project_roots)
    if direct:
        return direct
    current = process.ppid
    seen: set[int] = set()
    while current in processes and current not in seen:
        seen.add(current)
        parent = processes[current]
        project = project_for_cwd(parent.cwd, project_roots)
        if project:
            return project
        if current == root_pid:
            break
        current = parent.ppid
    return "(host)"


CGROUP_ROOT = Path("/sys/fs/cgroup")


def scope_limits(cgroup: str) -> dict[str, int | None]:
    """Cgroup limits of a wrg-launch restricted scope, if this session runs in one."""
    leaf = cgroup.rsplit("/", 1)[-1]
    if not (leaf.startswith("wrg-") and leaf.endswith(".scope")):
        return {}
    base = CGROUP_ROOT / cgroup.lstrip("/")
    limits: dict[str, int | None] = {}
    for key, name in (
        ("memory_high", "memory.high"),
        ("memory_max", "memory.max"),
        ("swap_max", "memory.swap.max"),
        ("tasks_max", "pids.max"),
    ):
        try:
            raw = (base / name).read_text().strip()
        except OSError:
            continue
        if raw == "max":
            limits[key] = None
        else:
            try:
                limits[key] = int(raw)
            except ValueError:
                continue
    return limits


def apply_cpu_rates(
    snapshot: ProcessSnapshot,
    previous: dict,
    now: float,
    clock_ticks: int,
) -> dict[str, dict[str, float]]:
    """Set cpu_percent on sessions/projects/MCP groups; return the map to persist.

    Rates are only computed when the root process age is continuous with the
    stored entry, so a recycled PID never inherits another process's counters.
    """
    if not isinstance(previous, dict):
        previous = {}
    next_map: dict[str, dict[str, float]] = {}

    def rate(key: str, age_seconds: float, jiffies: int) -> float:
        next_map[key] = {"jiffies": jiffies, "age": age_seconds, "at": now}
        prev = previous.get(key)
        if not isinstance(prev, dict) or clock_ticks <= 0:
            return 0.0
        try:
            elapsed = now - float(prev["at"])
            expected_age = float(prev["age"]) + elapsed
            delta = jiffies - int(prev["jiffies"])
        except (TypeError, ValueError, KeyError):
            return 0.0
        if elapsed <= 0 or delta < 0:
            return 0.0
        if abs(age_seconds - expected_age) > max(10.0, elapsed * 0.25):
            return 0.0
        return delta / clock_ticks / elapsed * 100.0

    # session.projects and project_usage share the same ProjectUsage objects.
    for session in snapshot.sessions:
        session.cpu_percent = rate(str(session.root_pid), session.age_seconds, session.cpu_jiffies)
        for project in session.projects:
            project.cpu_percent = rate(
                f"{session.root_pid}:{project.project}",
                session.age_seconds,
                project.cpu_jiffies,
            )
    for group in snapshot.mcp_groups:
        group.cpu_percent = rate(
            f"mcp:{group.root_pid}", group.age_seconds, group.cpu_jiffies
        )
    return next_map


def build_snapshot(project_roots: list[str], uid: int | None = None) -> ProcessSnapshot:
    processes = scan_processes(uid)
    roots = _session_roots(processes)
    assignments = _root_assignments(processes, roots)
    grouped: dict[tuple[int, str], ProjectUsage] = {}

    for pid, root_pid in assignments.items():
        process = processes[pid]
        provider = roots[root_pid]
        project = _project_for_process(process, root_pid, processes, project_roots)
        key = (root_pid, project)
        usage = grouped.setdefault(key, ProjectUsage(root_pid=root_pid, provider=provider, project=project))
        usage.rss_kib += process.rss_kib
        usage.swap_kib += process.swap_kib
        usage.process_count += 1
        usage.cpu_jiffies += process.cpu_jiffies
        if process.is_mcp:
            usage.mcp_rss_kib += process.rss_kib
            usage.mcp_count += 1

    projects_by_root: dict[int, list[ProjectUsage]] = defaultdict(list)
    for usage in grouped.values():
        projects_by_root[usage.root_pid].append(usage)

    sessions: list[SessionUsage] = []
    for root_pid, provider in roots.items():
        root = processes[root_pid]
        projects = sorted(projects_by_root.get(root_pid, []), key=lambda item: item.rss_kib, reverse=True)
        member_ages = [processes[pid].age_seconds for pid, assigned_root in assignments.items() if assigned_root == root_pid]
        sessions.append(
            SessionUsage(
                root_pid=root_pid,
                provider=provider,
                root_name=root.name,
                project=project_for_cwd(root.cwd, project_roots) or "(host)",
                age_seconds=root.age_seconds,
                youngest_process_age_seconds=min(member_ages, default=root.age_seconds),
                rss_kib=sum(item.rss_kib for item in projects),
                swap_kib=sum(item.swap_kib for item in projects),
                process_count=sum(item.process_count for item in projects),
                mcp_rss_kib=sum(item.mcp_rss_kib for item in projects),
                mcp_count=sum(item.mcp_count for item in projects),
                cgroup=root.cgroup,
                cpu_jiffies=sum(item.cpu_jiffies for item in projects),
                persistent=_persistent_provider(root.name.lower(), root.command.lower()) is not None,
                limits=scope_limits(root.cgroup),
                projects=projects,
            )
        )
    sessions.sort(key=lambda item: item.rss_kib, reverse=True)
    project_usage = sorted(grouped.values(), key=lambda item: item.rss_kib, reverse=True)
    mcp_groups = _build_mcp_groups(processes, assignments, roots, project_roots)
    return ProcessSnapshot(
        processes=processes,
        sessions=sessions,
        project_usage=project_usage,
        root_for_pid=assignments,
        provider_for_root=roots,
        mcp_groups=mcp_groups,
    )


def _build_mcp_groups(
    processes: dict[int, ProcessInfo],
    assignments: dict[int, int],
    roots: dict[int, str],
    project_roots: list[str],
) -> list[McpUsage]:
    flagged = {pid for pid, process in processes.items() if pid in assignments and process.is_mcp}
    mcp_roots: set[int] = set()
    for pid in flagged:
        has_flagged_ancestor = False
        for ancestor in _ancestor_chain(pid, processes):
            if ancestor in flagged:
                has_flagged_ancestor = True
                break
            if ancestor == assignments[pid]:
                break
        if not has_flagged_ancestor:
            mcp_roots.add(pid)

    group_for_pid: dict[int, int] = {}
    for pid, session_root in assignments.items():
        current = pid
        seen: set[int] = set()
        while current in processes and current not in seen:
            if current in mcp_roots:
                group_for_pid[pid] = current
                break
            if current == session_root:
                break
            seen.add(current)
            current = processes[current].ppid

    groups: dict[int, McpUsage] = {}
    for pid, mcp_root in group_for_pid.items():
        process = processes[pid]
        session_root = assignments[pid]
        root_process = processes[mcp_root]
        group = groups.setdefault(
            mcp_root,
            McpUsage(
                root_pid=mcp_root,
                session_root_pid=session_root,
                provider=roots[session_root],
                project=_project_for_process(root_process, session_root, processes, project_roots),
                root_name=root_process.name,
                age_seconds=root_process.age_seconds,
            ),
        )
        group.rss_kib += process.rss_kib
        group.swap_kib += process.swap_kib
        group.process_count += 1
        group.cpu_jiffies += process.cpu_jiffies
    return sorted(groups.values(), key=lambda item: item.rss_kib, reverse=True)


def descendants_of(pid: int, processes: dict[int, ProcessInfo]) -> list[ProcessInfo]:
    children: dict[int, list[int]] = defaultdict(list)
    for process in processes.values():
        children[process.ppid].append(process.pid)
    result: list[ProcessInfo] = []
    stack: list[tuple[int, int]] = [(pid, 0)]
    depth_by_pid: dict[int, int] = {pid: 0}
    while stack:
        current, depth = stack.pop()
        process = processes.get(current)
        if process:
            result.append(process)
        for child in children.get(current, []):
            depth_by_pid[child] = depth + 1
            stack.append((child, depth + 1))
    return sorted(result, key=lambda item: depth_by_pid.get(item.pid, 0), reverse=True)
