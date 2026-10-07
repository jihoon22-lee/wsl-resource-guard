import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard.processes import (
    McpUsage,
    ProcessInfo,
    ProcessSnapshot,
    SessionUsage,
    _root_assignments,
    _session_roots,
    apply_cpu_rates,
    build_snapshot,
    detect_provider,
    is_stale_session,
    kill_block_reason,
    process_start_ticks,
    project_for_cwd,
    scope_limits,
)


def process(pid: int, ppid: int, name: str, command: str = "", cwd: str = "/home/demo/projects/demo") -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        uid=1000,
        name=name,
        state="S",
        rss_kib=100,
        swap_kib=0,
        age_seconds=10,
        cwd=cwd,
        cgroup="/init.scope",
        command=command,
    )


class ProcessTests(unittest.TestCase):
    def test_finds_top_agent_root_and_assigns_descendants(self) -> None:
        processes = {
            10: process(10, 1, "claude"),
            11: process(11, 10, "2.1.236"),
            12: process(12, 11, "node", "node chrome-devtools-mcp"),
            13: process(13, 10, "codex"),
        }
        roots = _session_roots(processes)
        assignments = _root_assignments(processes, roots)
        self.assertEqual(roots, {10: "claude"})
        self.assertEqual(assignments[12], 10)
        self.assertEqual(assignments[13], 10)

    def test_provider_and_mcp_detection(self) -> None:
        codex = process(10, 1, "codex")
        mcp = process(11, 10, "node", "node /pkg/chrome-devtools-mcp/index.js")
        ordinary = process(12, 10, "node", "node application.js --description mcpish")
        self.assertEqual(detect_provider(codex), "codex")
        self.assertTrue(mcp.is_mcp)
        self.assertFalse(ordinary.is_mcp)

    def test_wrg_management_commands_are_not_mcp(self) -> None:
        for command in (
            "python3 /home/demo/.local/bin/wrg mcp",
            "python3 /home/demo/.local/bin/wrg stop-mcp 123",
            "/home/demo/.local/bin/wrg stop-mcp",
        ):
            with self.subTest(command=command):
                self.assertFalse(process(30, 10, "python3", command).is_mcp)

    def test_devin_daemon_groups_agent_and_pty_children(self) -> None:
        # devin-web: exec PTYs are children of devin-acpd, not of `devin acp`.
        processes = {
            20: process(20, 1, "MainThread", "node /home/demo/projects/devin-web/bin/devin-acpd.mjs"),
            21: process(21, 20, "devin", "devin acp"),
            22: process(22, 20, "bash", "/bin/bash -c make test"),
            23: process(23, 1, "MainThread", "node /home/demo/projects/devin-web/bin/devin-web.mjs --port 7100"),
            24: process(24, 1, "MainThread", "node /home/demo/projects/devin-web/bin/devin-web-watch.mjs"),
        }
        roots = _session_roots(processes)
        self.assertEqual(roots, {20: "devin"})
        assignments = _root_assignments(processes, roots)
        self.assertEqual(assignments, {20: 20, 21: 20, 22: 20})

    def test_devin_cli_detected_without_daemon(self) -> None:
        self.assertEqual(detect_provider(process(30, 1, "devin")), "devin")
        self.assertEqual(detect_provider(process(31, 1, "devin", "devin acp")), "devin")
        # The web app and its supervisor are not agent sessions.
        self.assertIsNone(detect_provider(process(32, 1, "MainThread", "node bin/devin-web.mjs")))
        self.assertIsNone(detect_provider(process(33, 1, "MainThread", "node bin/devin-web-watch.mjs")))

    def test_antigravity_desktop_and_cli_detection(self) -> None:
        server_cmd = (
            "/home/demo/.antigravity-server/bin/2.19.1/language_server --standalone "
            "--override_ide_name antigravity --subclient_type hub"
        )
        self.assertEqual(detect_provider(process(40, 1, "language_server", server_cmd)), "agy")
        self.assertIsNone(detect_provider(process(41, 1, "language_server", "/usr/bin/language_server --lsp")))
        self.assertEqual(detect_provider(process(42, 1, "agy")), "agy")
        self.assertEqual(detect_provider(process(43, 1, "antigravity")), "agy")
        self.assertEqual(detect_provider(process(44, 1, "python3", "python3 -m agy start")), "agy")
        self.assertEqual(detect_provider(process(45, 1, "node", "node /usr/lib/agy/bin/agy.js chat")), "agy")
        self.assertEqual(detect_provider(process(46, 1, "MainThread", "/opt/bin/antigravity --wait")), "agy")
        # A language_server outside the .antigravity-server install is not Antigravity.
        self.assertIsNone(detect_provider(process(47, 1, "language_server", "/usr/bin/language_server --name antigravity")))

    def test_agy_word_in_arguments_is_not_a_session(self) -> None:
        for name, command in (
            ("grep", "grep agy log.txt"),
            ("rg", "rg antigravity src/"),
            ("vim", "vim /home/demo/projects/antigravity/readme.md"),
            ("python3", "python3 /home/demo/antigravity/tool.py"),
            ("python3", "python3 -m pytest tests/agy"),
            ("node", "node server.js --name agy"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(detect_provider(process(50, 1, name, command)))

    def test_antigravity_server_groups_children(self) -> None:
        server_cmd = "/home/demo/.antigravity-server/bin/2.19.1/language_server --override_ide_name antigravity"
        processes = {
            40: process(40, 1, "language_server", server_cmd, cwd="/mnt/c/Users/Example/AppData/Local/Programs/antigravity"),
            41: process(41, 40, "bash", "/bin/bash -l", cwd="/home/demo/projects/wsl-resource-guard"),
            42: process(42, 41, "python3", "python3 -m unittest", cwd="/home/demo/projects/wsl-resource-guard"),
            43: process(43, 40, "node", "node chrome-devtools-mcp", cwd="/home/demo/projects/wsl-resource-guard"),
        }
        roots = _session_roots(processes)
        self.assertEqual(roots, {40: "agy"})
        assignments = _root_assignments(processes, roots)
        self.assertEqual(assignments, {40: 40, 41: 40, 42: 40, 43: 40})

    def test_project_attribution(self) -> None:
        self.assertEqual(
            project_for_cwd("/home/demo/projects/sample-app/.worktrees/x", ["/home/demo/projects"]),
            "sample-app",
        )
        self.assertEqual(project_for_cwd("/home/demo/projects", ["/home/demo/projects"]), "(workspace)")
        self.assertIsNone(project_for_cwd("/home/user", ["/home/demo/projects"]))


class CpuRateTests(unittest.TestCase):
    def snapshot(self) -> ProcessSnapshot:
        session = SessionUsage(
            root_pid=10, provider="claude", root_name="claude", project="demo",
            age_seconds=100.0, cpu_jiffies=2000,
        )
        group = McpUsage(
            root_pid=20, session_root_pid=10, provider="claude", project="demo",
            root_name="node", age_seconds=80.0, cpu_jiffies=500,
        )
        return ProcessSnapshot({}, [session], [], {}, {}, [group])

    def test_first_sample_only_stores_baseline(self) -> None:
        snap = self.snapshot()
        stored = apply_cpu_rates(snap, {}, now=1000.0, clock_ticks=100)
        self.assertEqual(snap.sessions[0].cpu_percent, 0.0)
        self.assertEqual(stored["10"], {"jiffies": 2000, "age": 100.0, "at": 1000.0})
        self.assertIn("mcp:20", stored)

    def test_rate_from_jiffies_delta(self) -> None:
        snap = self.snapshot()
        previous = {"10": {"jiffies": 1900, "age": 90.0, "at": 990.0}}
        apply_cpu_rates(snap, previous, now=1000.0, clock_ticks=100)
        # 100 jiffies / 100 ticks / 10s = 10%
        self.assertAlmostEqual(snap.sessions[0].cpu_percent, 10.0)

    def test_pid_reuse_never_inherits_counters(self) -> None:
        snap = self.snapshot()
        # 같은 PID지만 age가 불연속(재사용된 PID) — 비율 0
        previous = {"10": {"jiffies": 1900, "age": 9999.0, "at": 990.0}}
        apply_cpu_rates(snap, previous, now=1000.0, clock_ticks=100)
        self.assertEqual(snap.sessions[0].cpu_percent, 0.0)
        # jiffies가 줄어도(카운터 리셋) 0
        previous = {"10": {"jiffies": 9999, "age": 90.0, "at": 990.0}}
        apply_cpu_rates(snap, previous, now=1000.0, clock_ticks=100)
        self.assertEqual(snap.sessions[0].cpu_percent, 0.0)


class JudgmentTests(unittest.TestCase):
    def test_service_cgroup_blocks_kill(self) -> None:
        root = process(10, 1, "claude")
        root.cgroup = "/system.slice/opencode-web.service"
        self.assertIn("systemd", kill_block_reason(root))
        root.cgroup = "/user.slice/demo.scope"
        self.assertEqual(kill_block_reason(root), "")
        self.assertEqual(kill_block_reason(None), "")

    def test_stale_session_rule(self) -> None:
        session = SessionUsage(
            root_pid=10, provider="claude", root_name="claude", project="demo",
            age_seconds=100.0, youngest_process_age_seconds=49 * 3600,
        )
        self.assertTrue(is_stale_session(session, 48))
        session.provider = "opencode"
        self.assertFalse(is_stale_session(session, 48))
        session.provider = "claude"
        session.youngest_process_age_seconds = 3600
        self.assertFalse(is_stale_session(session, 48))

    def test_persistent_agent_servers_are_never_stale(self) -> None:
        old = 72 * 3600
        processes = {
            20: process(20, 1, "MainThread", "node /home/demo/projects/devin-web/bin/devin-acpd.mjs"),
            40: process(40, 1, "language_server",
                        "/home/demo/.antigravity-server/bin/2.19.1/language_server --standalone"),
            60: process(60, 1, "claude", "claude"),
            61: process(61, 1, "devin", "devin"),
        }
        for item in processes.values():
            item.age_seconds = old
        with patch("wsl_resource_guard.processes.scan_processes", return_value=processes), \
                patch("wsl_resource_guard.processes.scope_limits", return_value={}):
            snap = build_snapshot([])
        by_root = {session.root_pid: session for session in snap.sessions}
        self.assertEqual(set(by_root), {20, 40, 60, 61})
        self.assertTrue(by_root[20].persistent)
        self.assertTrue(by_root[40].persistent)
        self.assertFalse(by_root[61].persistent)
        self.assertEqual(
            {pid for pid, session in by_root.items() if is_stale_session(session, 48)},
            {60, 61},
        )

    def test_process_start_ticks_identifies_pid_instance(self) -> None:
        self.assertIsNotNone(process_start_ticks(os.getpid()))
        self.assertEqual(process_start_ticks(os.getpid()), process_start_ticks(os.getpid()))


class ScopeLimitTests(unittest.TestCase):
    def test_only_wrg_scopes_report_limits(self) -> None:
        self.assertEqual(scope_limits("/init.scope"), {})
        self.assertEqual(scope_limits("/user.slice/user-1000.slice/app.service"), {})

    def test_reads_scope_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cgroup = "/user.slice/wrg-demo.scope"
            base = Path(directory) / "user.slice/wrg-demo.scope"
            base.mkdir(parents=True)
            (base / "memory.max").write_text("4294967296\n")
            (base / "memory.high").write_text("max\n")
            (base / "memory.swap.max").write_text("not-a-number\n")
            with patch("wsl_resource_guard.processes.CGROUP_ROOT", Path(directory)):
                limits = scope_limits(cgroup)
        self.assertEqual(limits["memory_max"], 4294967296)
        self.assertIsNone(limits["memory_high"])
        self.assertNotIn("swap_max", limits)


if __name__ == "__main__":
    unittest.main()
