from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from wsl_resource_guard.metrics import (
    SystemMetrics,
    _read_key_values,
    _read_memory_psi,
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


if __name__ == "__main__":
    unittest.main()
