from pathlib import Path
import tempfile
import unittest

from wsl_resource_guard.metrics import SystemMetrics
from wsl_resource_guard.processes import ProcessSnapshot, SessionUsage
from wsl_resource_guard.reporting import (
    build_html_report,
    build_text_report,
    collect_resource_rows,
)


def metrics() -> SystemMetrics:
    return SystemMetrics(
        timestamp=100,
        mem_total_kib=16 * 1024 * 1024,
        mem_available_kib=10 * 1024 * 1024,
        swap_total_kib=4 * 1024 * 1024,
        swap_free_kib=2 * 1024 * 1024,
        psi_some_avg60=0.25,
        psi_full_avg60=0.05,
        pswpout_pages=0,
        oom_kills=0,
        swap_out_mib_per_minute=12.5,
    )


def snapshot() -> ProcessSnapshot:
    session = SessionUsage(
        root_pid=10,
        provider="codex&tools",
        root_name="codex",
        project="<demo>",
        age_seconds=3660,
        rss_kib=3 * 1024 * 1024,
        swap_kib=256 * 1024,
        process_count=12,
        mcp_count=4,
        cgroup="/init.scope",
    )
    return ProcessSnapshot({}, [session], [], {}, {}, [])


class ReportingTests(unittest.TestCase):
    def test_text_report_shows_used_and_total_capacity(self) -> None:
        current = snapshot()
        rows = collect_resource_rows(current, service_root=Path("/missing"))
        report = build_text_report(metrics(), current, "warning", ["reason"], [], [], rows)
        self.assertIn("RAM   6.00 GiB / 16.00 GiB 사용", report)
        self.assertIn("Swap  2.00 GiB / 4.00 GiB 사용", report)
        self.assertIn("codex&tools", report)
        self.assertIn("프로세스 12 · MCP 4", report)

    def test_html_report_escapes_dynamic_values(self) -> None:
        current = snapshot()
        rows = collect_resource_rows(current, service_root=Path("/missing"))
        report = build_html_report(metrics(), current, "warning", [], [], [], rows)
        self.assertIn("codex&amp;tools", report)
        self.assertIn("&lt;demo&gt;", report)
        self.assertNotIn("<demo>", report)

    def test_collects_systemd_service_cgroup_without_duplicate_session(self) -> None:
        current = snapshot()
        with tempfile.TemporaryDirectory() as directory:
            service = Path(directory) / "demo.service"
            service.mkdir()
            (service / "memory.current").write_text(str(512 * 1024 * 1024), encoding="utf-8")
            (service / "memory.swap.current").write_text(str(64 * 1024 * 1024), encoding="utf-8")
            (service / "pids.current").write_text("3", encoding="utf-8")
            (service / "cgroup.procs").write_text("31\n32\n", encoding="utf-8")
            rows = collect_resource_rows(current, service_root=Path(directory))
        service_row = next(row for row in rows if row.name == "demo")
        self.assertEqual(service_row.memory_kib, 512 * 1024)
        self.assertEqual(service_row.swap_kib, 64 * 1024)
        self.assertEqual(service_row.process_count, 3)
        self.assertIn("PID 31", service_row.identifier)


if __name__ == "__main__":
    unittest.main()
