from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from wsl_resource_guard.metrics import (
    SystemMetrics,
    _read_key_values,
    _read_memory_psi,
    read_host_memory,
    read_system_metrics,
)


class MetricsTests(unittest.TestCase):
    def test_reads_proc_style_key_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meminfo"
            path.write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n", encoding="utf-8")
            values = _read_key_values(path)
        self.assertEqual(values["MemTotal"], 1000)
        self.assertEqual(values["MemAvailable"], 250)

    def test_reads_memory_psi_avg60(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pressure"
            path.write_text(
                "some avg10=1.00 avg60=2.50 avg300=3.00 total=10\n"
                "full avg10=0.10 avg60=0.25 avg300=0.50 total=2\n",
                encoding="utf-8",
            )
            some, full, status = _read_memory_psi(path)
            self.assertEqual(status, "normal")
        self.assertEqual(some, 2.5)
        self.assertEqual(full, 0.25)

    def test_swap_in_and_out_rates(self) -> None:
        previous = SystemMetrics(
            timestamp=100.0, mem_total_kib=1, mem_available_kib=1,
            swap_total_kib=1, swap_free_kib=1, psi_some_avg60=0.0, psi_full_avg60=0.0,
            pswpout_pages=1000, oom_kills=0, pswpin_pages=2000,
        )

        def keys(path: Path) -> dict[str, int]:
            if path.name == "meminfo":
                return {"MemTotal": 1, "MemAvailable": 1, "SwapTotal": 1, "SwapFree": 1}
            return {"pswpout": 2500, "pswpin": 3500, "oom_kill": 0}

        with (
            patch("wsl_resource_guard.metrics._read_key_values", side_effect=keys),
            patch("wsl_resource_guard.metrics._read_memory_psi", return_value=(0.0, 0.0, "normal")),
            patch("wsl_resource_guard.metrics.time") as clock,
        ):
            clock.time.return_value = 160.0  # 60초 경과
            current = read_system_metrics(previous)
        # 1500 페이지 * 4KiB, 60초 → 1500*4096/2**20 MiB/min
        self.assertAlmostEqual(current.swap_out_mib_per_minute, 1500 * 4096 / 2**20, places=3)
        self.assertAlmostEqual(current.swap_in_mib_per_minute, 1500 * 4096 / 2**20, places=3)

    def test_swap_counters_never_go_negative(self) -> None:
        previous = SystemMetrics(
            timestamp=100.0, mem_total_kib=1, mem_available_kib=1,
            swap_total_kib=1, swap_free_kib=1, psi_some_avg60=0.0, psi_full_avg60=0.0,
            pswpout_pages=5000, oom_kills=0, pswpin_pages=5000,
        )
        with (
            patch("wsl_resource_guard.metrics._read_key_values", return_value={}),
            patch("wsl_resource_guard.metrics._read_memory_psi", return_value=(0.0, 0.0, "normal")),
            patch("wsl_resource_guard.metrics.time") as clock,
        ):
            clock.time.return_value = 160.0
            current = read_system_metrics(previous)
        self.assertEqual(current.swap_out_mib_per_minute, 0.0)
        self.assertEqual(current.swap_in_mib_per_minute, 0.0)


class HostMemoryTests(unittest.TestCase):
    def test_parses_powershell_sum(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="7000000000\n", stderr="")
        with (
            patch("wsl_resource_guard.metrics.POWERSHELL") as ps,
            patch("wsl_resource_guard.metrics.subprocess.run", return_value=completed) as run,
        ):
            ps.exists.return_value = True
            value, observed, attempted = read_host_memory(force=True)
        self.assertEqual(value, 7_000_000_000)
        self.assertGreater(observed, 0)
        self.assertEqual(attempted, observed)
        self.assertIn("vmmem*", run.call_args.args[0][-1])

    def test_failure_keeps_previous_value_and_records_the_attempt(self) -> None:
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with (
            patch("wsl_resource_guard.metrics.POWERSHELL") as ps,
            patch("wsl_resource_guard.metrics.subprocess.run", return_value=completed),
        ):
            ps.exists.return_value = True
            value, observed, attempted = read_host_memory(1234, 50.0, force=True)
        self.assertEqual((value, observed), (1234, 50.0))
        self.assertGreater(attempted, 50.0)

    def test_empty_output_keeps_previous_value(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        with (
            patch("wsl_resource_guard.metrics.POWERSHELL") as ps,
            patch("wsl_resource_guard.metrics.subprocess.run", return_value=completed),
        ):
            ps.exists.return_value = True
            value, observed, _ = read_host_memory(1234, 50.0, force=True)
        self.assertEqual((value, observed), (1234, 50.0))

    def test_no_spawn_before_interval(self) -> None:
        recent = time.time() - 10
        with patch("wsl_resource_guard.metrics.subprocess.run") as run:
            result = read_host_memory(1234, recent, interval=300)
        run.assert_not_called()
        self.assertEqual(result, (1234, recent, 0.0))

    def test_failed_attempt_waits_a_full_interval(self) -> None:
        # Interop outage: the old value is stale, but one failure per interval is enough.
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with (
            patch("wsl_resource_guard.metrics.POWERSHELL") as ps,
            patch("wsl_resource_guard.metrics.subprocess.run", return_value=completed) as run,
        ):
            ps.exists.return_value = True
            value, observed, attempted = read_host_memory(1234, 50.0, interval=300)
            for _ in range(3):
                value, observed, attempted = read_host_memory(
                    value, observed, interval=300, previous_attempt=attempted
                )
        self.assertEqual(run.call_count, 1)
        self.assertEqual((value, observed), (1234, 50.0))

    def test_future_timestamps_do_not_block_collection_forever(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="42\n", stderr="")
        with (
            patch("wsl_resource_guard.metrics.POWERSHELL") as ps,
            patch("wsl_resource_guard.metrics.subprocess.run", return_value=completed),
        ):
            ps.exists.return_value = True
            value, _, _ = read_host_memory(1234, 1e18, interval=300)
        self.assertEqual(value, 42)

    def test_clock_rollback_failure_still_waits_a_full_interval(self) -> None:
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="interop unavailable")
        with (
            patch("wsl_resource_guard.metrics.POWERSHELL") as ps,
            patch("wsl_resource_guard.metrics.subprocess.run", return_value=completed) as run,
            patch("wsl_resource_guard.metrics.time.time") as clock,
        ):
            ps.exists.return_value = True
            value, observed, attempted = 1234, 1060.0, 0.0
            for now in (1000, 1015, 1030, 1299):
                clock.return_value = now
                value, observed, attempted = read_host_memory(
                    value, observed, interval=300, previous_attempt=attempted
                )
            self.assertEqual(run.call_count, 1)
            self.assertEqual((value, observed, attempted), (1234, 1060.0, 1000))
            clock.return_value = 1300
            value, observed, attempted = read_host_memory(
                value, observed, interval=300, previous_attempt=attempted
            )
        self.assertEqual(run.call_count, 2)
        self.assertEqual((value, observed, attempted), (1234, 1060.0, 1300))

    def test_missing_powershell_keeps_previous(self) -> None:
        with patch("wsl_resource_guard.metrics.POWERSHELL") as ps:
            ps.exists.return_value = False
            value, observed, attempted = read_host_memory(1234, 50.0, force=True)
        self.assertEqual((value, observed), (1234, 50.0))
        self.assertGreater(attempted, 0)

    def test_corrupt_previous_is_discarded(self) -> None:
        with patch("wsl_resource_guard.metrics.POWERSHELL") as ps:
            ps.exists.return_value = False
            value, observed, _ = read_host_memory("bad", 0.0, force=True)
        self.assertIsNone(value)
        self.assertEqual(observed, 0.0)


if __name__ == "__main__":
    unittest.main()
