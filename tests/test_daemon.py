from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard.config import Settings
from wsl_resource_guard.daemon import (
    Evaluation,
    alert_reminder_due,
    append_history,
    evaluate,
    in_quiet_hours,
    load_state,
    oom_kill_victims,
    record_history,
    sample,
    should_log_sample,
    stabilize_recovery,
)
from wsl_resource_guard.metrics import SystemMetrics
from wsl_resource_guard.notifications import NotificationResult
from wsl_resource_guard.processes import McpUsage, ProcessSnapshot, SessionUsage


def setUpModule() -> None:
    # sample() must never start the real powershell.exe from a unit test.
    patcher = patch("wsl_resource_guard.metrics.POWERSHELL", Path("/nonexistent/powershell.exe"))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def metrics(
    available_gib: float,
    psi_some: float = 0,
    psi_full: float = 0,
    *,
    timestamp: float = 100,
    swap_out: float = 0,
    oom_kills: int = 0,
) -> SystemMetrics:
    return SystemMetrics(
        timestamp=timestamp,
        mem_total_kib=16 * 1024 * 1024,
        mem_available_kib=int(available_gib * 1024 * 1024),
        swap_total_kib=4 * 1024 * 1024,
        swap_free_kib=2 * 1024 * 1024,
        psi_some_avg60=psi_some,
        psi_full_avg60=psi_full,
        pswpout_pages=0,
        oom_kills=oom_kills,
        swap_out_mib_per_minute=swap_out,
    )


def snapshot(rss_gib: float = 0, mcp_rss_gib: float = 0) -> ProcessSnapshot:
    sessions = []
    if rss_gib:
        sessions.append(
            SessionUsage(
                root_pid=10,
                provider="codex",
                root_name="codex",
                project="demo",
                age_seconds=1,
                youngest_process_age_seconds=1,
                rss_kib=int(rss_gib * 1024 * 1024),
            )
        )
    mcp_groups = []
    if mcp_rss_gib:
        mcp_groups.append(
            McpUsage(
                root_pid=20,
                session_root_pid=10,
                provider="codex",
                project="demo",
                root_name="mcp-server",
                age_seconds=1,
                rss_kib=int(mcp_rss_gib * 1024 * 1024),
                process_count=2,
            )
        )
    return ProcessSnapshot({}, sessions, [], {}, {}, mcp_groups)


class EvaluationTests(unittest.TestCase):
    def test_critical_available_memory(self) -> None:
        result = evaluate(metrics(1.0), snapshot(), Settings())
        self.assertEqual(result.severity, "critical")

    def test_large_session_is_observed_without_warning(self) -> None:
        settings = Settings()
        result = evaluate(metrics(8.0), snapshot(4.5), settings)
        self.assertEqual(result.severity, "normal")
        self.assertTrue(result.observations)

    def test_session_warning_requires_two_minutes(self) -> None:
        settings = Settings()
        initial = evaluate(metrics(8.0), snapshot(6.5), settings)
        self.assertEqual(initial.severity, "normal")
        self.assertTrue(initial.pending_reasons)
        mature = evaluate(
            metrics(8.0, timestamp=220),
            snapshot(6.5),
            settings,
            condition_since=initial.condition_since,
        )
        self.assertEqual(mature.severity, "warning")

    def test_mcp_observation_and_sustained_warning(self) -> None:
        settings = Settings()
        observed = evaluate(metrics(8.0), snapshot(mcp_rss_gib=4.0), settings)
        self.assertEqual(observed.severity, "normal")
        self.assertTrue(observed.observations)
        initial = evaluate(metrics(8.0), snapshot(mcp_rss_gib=5.5), settings)
        mature = evaluate(
            metrics(8.0, timestamp=220),
            snapshot(mcp_rss_gib=5.5),
            settings,
            condition_since=initial.condition_since,
        )
        self.assertEqual(mature.severity, "warning")

    def test_available_memory_warning_requires_thirty_seconds(self) -> None:
        settings = Settings()
        initial = evaluate(metrics(3.5), snapshot(), settings)
        self.assertEqual(initial.severity, "normal")
        mature = evaluate(
            metrics(3.5, timestamp=130),
            snapshot(),
            settings,
            condition_since=initial.condition_since,
        )
        self.assertEqual(mature.severity, "warning")

    def test_psi_stall_with_ample_ram_is_only_a_sustained_warning(self) -> None:
        # Real 2026-09-22 13:45 sample: 11.7 GiB available, PSI some 9.06% / full 8.45%.
        settings = Settings()
        since = None
        for elapsed, expected in ((0, "normal"), (30, "normal"), (60, "normal"),
                                  (299, "normal"), (300, "warning"), (900, "warning")):
            with self.subTest(elapsed=elapsed):
                result = evaluate(
                    metrics(11.7, psi_some=9.06, psi_full=8.45, timestamp=100 + elapsed),
                    snapshot(),
                    settings,
                    condition_since=since,
                )
                since = result.condition_since
                self.assertEqual(result.severity, expected)

    def test_extreme_psi_full_is_critical_after_thirty_seconds(self) -> None:
        settings = Settings()
        initial = evaluate(metrics(12.0, psi_some=26, psi_full=25), snapshot(), settings)
        self.assertEqual(initial.severity, "normal")
        mature = evaluate(
            metrics(12.0, psi_some=26, psi_full=25, timestamp=130),
            snapshot(),
            settings,
            condition_since=initial.condition_since,
        )
        self.assertEqual(mature.severity, "critical")

    def test_low_ram_and_psi_form_composite_critical(self) -> None:
        settings = Settings()
        initial = evaluate(metrics(3.5, psi_full=1.2), snapshot(), settings)
        mature = evaluate(
            metrics(3.5, psi_full=1.2, timestamp=130),
            snapshot(),
            settings,
            condition_since=initial.condition_since,
        )
        self.assertEqual(mature.severity, "critical")

    def test_swap_out_requires_low_available_memory(self) -> None:
        settings = Settings()
        healthy = evaluate(metrics(8.0, swap_out=300), snapshot(), settings)
        self.assertEqual(healthy.severity, "normal")
        self.assertFalse(healthy.pending_reasons)
        initial = evaluate(metrics(5.0, swap_out=300), snapshot(), settings)
        mature = evaluate(
            metrics(5.0, timestamp=130, swap_out=300),
            snapshot(),
            settings,
            condition_since=initial.condition_since,
        )
        self.assertEqual(mature.severity, "warning")

    def test_interrupted_condition_resets_sustain_timer(self) -> None:
        settings = Settings()
        initial = evaluate(metrics(3.5), snapshot(), settings)
        healthy = evaluate(
            metrics(8.0, timestamp=115),
            snapshot(),
            settings,
            condition_since=initial.condition_since,
        )
        restarted = evaluate(
            metrics(3.5, timestamp=130),
            snapshot(),
            settings,
            condition_since=healthy.condition_since,
        )
        self.assertEqual(restarted.severity, "normal")
        self.assertIn("warning.available_ram", restarted.condition_since)

    def test_oom_kill_is_immediately_critical(self) -> None:
        settings = Settings()
        previous = metrics(8.0, timestamp=90, oom_kills=0)
        result = evaluate(
            metrics(8.0, timestamp=100, oom_kills=1),
            snapshot(),
            settings,
            previous,
        )
        self.assertEqual(result.severity, "critical")

    def test_oom_reason_names_victims(self) -> None:
        settings = Settings()
        previous = metrics(8.0, timestamp=90, oom_kills=0)
        result = evaluate(
            metrics(8.0, timestamp=100, oom_kills=1),
            snapshot(),
            settings,
            previous,
            oom_victims=["node", "python"],
        )
        self.assertIn("희생 프로세스", result.reasons[0])
        self.assertIn("node", result.reasons[0])
        self.assertIn("python", result.reasons[0])


class QuietHoursTests(unittest.TestCase):
    @staticmethod
    def at(hour: int, minute: int = 0) -> float:
        return datetime(2026, 1, 1, hour, minute).astimezone().timestamp()

    def test_window_matching(self) -> None:
        self.assertTrue(in_quiet_hours(self.at(23, 30), "23-08"))
        self.assertTrue(in_quiet_hours(self.at(3), "23-08"))
        self.assertFalse(in_quiet_hours(self.at(12), "23-08"))
        self.assertTrue(in_quiet_hours(self.at(12), "08-23"))
        self.assertTrue(in_quiet_hours(self.at(23, 45), "23:30-07:15"))
        self.assertFalse(in_quiet_hours(self.at(23, 15), "23:30-07:15"))
        self.assertFalse(in_quiet_hours(self.at(23, 30), "08-08"))
        self.assertFalse(in_quiet_hours(self.at(12), "garbage"))
        self.assertFalse(in_quiet_hours(self.at(12), ""))

    def test_suppresses_only_warning_reminders(self) -> None:
        settings = Settings(alert_quiet_hours="23-08")
        quiet = self.at(23, 30)
        noon = self.at(12)
        stale = 100000
        self.assertFalse(
            alert_reminder_due("warning", "warning", quiet, quiet - stale, settings)
        )
        self.assertTrue(
            alert_reminder_due("warning", "warning", noon, noon - stale, settings)
        )
        self.assertTrue(
            alert_reminder_due("warning", "normal", quiet, quiet - 10, settings)
        )
        self.assertTrue(
            alert_reminder_due("critical", "critical", quiet, quiet - stale, settings)
        )


class SnoozeTests(unittest.TestCase):
    def test_snooze_pauses_reminders_but_not_transitions(self) -> None:
        settings = Settings()
        now, stale = 1_000_000.0, 100000
        self.assertFalse(alert_reminder_due("critical", "critical", now, now - stale, settings, snoozed=True))
        self.assertFalse(alert_reminder_due("warning", "warning", now, now - stale, settings, snoozed=True))
        # Escalation and a new episode still notify.
        self.assertTrue(alert_reminder_due("critical", "warning", now, now - 10, settings, snoozed=True))
        self.assertTrue(alert_reminder_due("warning", "normal", now, now - 10, settings, snoozed=True))

    def test_read_snooze_until_validates_the_file(self) -> None:
        import json as _json
        import tempfile as _tempfile
        from wsl_resource_guard.daemon import read_snooze_until
        with _tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.assertEqual(read_snooze_until(folder, 100.0), 0.0)
            (folder / "alert-snooze.json").write_text(_json.dumps({"until": 3700}))
            self.assertEqual(read_snooze_until(folder, 100.0), 3700)
            self.assertEqual(read_snooze_until(folder, 4000.0), 0.0)  # expired
            (folder / "alert-snooze.json").write_text(_json.dumps({"until": 100 + 3 * 86400}))
            self.assertEqual(read_snooze_until(folder, 100.0), 0.0)  # over a day: ignored
            (folder / "alert-snooze.json").write_text("not json")
            self.assertEqual(read_snooze_until(folder, 100.0), 0.0)


class OomVictimTests(unittest.TestCase):
    def test_parses_journal_lines(self) -> None:
        output = (
            "Out of memory: Killed process 123 (node) total-vm:100kB\n"
            "Out of memory: Killed process 456 (python) total-vm:200kB\n"
            "oom_reaper: reaped process 123 (node)\n"
        )
        completed = subprocess.CompletedProcess([], 0, stdout=output)
        with patch("wsl_resource_guard.daemon.subprocess.run", return_value=completed):
            self.assertEqual(oom_kill_victims(100), ["node", "python"])

    def test_journal_failure_returns_empty(self) -> None:
        with patch("wsl_resource_guard.daemon.subprocess.run", side_effect=OSError):
            self.assertEqual(oom_kill_victims(100), [])

    def test_recovery_requires_two_normal_minutes(self) -> None:
        settings = Settings(recovery_sustain_seconds=120)
        normal = Evaluation("normal", [], [])
        held, started_at, ready = stabilize_recovery(
            normal, "warning", ["old warning"], 100, 0, settings
        )
        self.assertEqual(held.severity, "warning")
        self.assertFalse(ready)
        recovered, _, ready = stabilize_recovery(
            normal, "warning", ["old warning"], 220, started_at, settings
        )
        self.assertEqual(recovered.severity, "normal")
        self.assertTrue(ready)

    def test_healthy_sample_is_normal(self) -> None:
        result = evaluate(metrics(8.0), snapshot(), Settings())
        self.assertEqual(result.severity, "normal")


class SampleStateTests(unittest.TestCase):
    def test_malformed_email_preserves_samples_history_and_reminder_interval(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory, windows_toast_enabled=False,
                                push_enabled=False, email_heartbeat_enabled=False,
                                history_interval_seconds=10)
            secrets = {"gmail_user": "sender@example.test\nprivate-marker",
                       "gmail_to": "receiver@example.test", "gmail_app_password": "fixture",
                       "discord_webhook_url": "https://discord.com/api/webhooks/fixture"}
            response = MagicMock()
            response.__enter__.return_value.status = 204
            with (patch.object(Settings, "load_secrets", return_value=secrets),
                  patch("wsl_resource_guard.daemon.read_system_metrics") as reader,
                  patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                  patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
                  patch("wsl_resource_guard.notifications.urllib.request.urlopen", return_value=response) as sent,
                  patch("wsl_resource_guard.notifications.smtplib.SMTP_SSL") as smtp):
                history_at = disk_at = 0
                for stamp in (100, 115):
                    reader.return_value = metrics(1.0, timestamp=stamp)
                    current, processes, evaluation = sample(settings, notify=True, shared_dir=Path(directory))
                    history_at, disk_at = record_history(settings, current, processes, evaluation,
                                                         history_at, disk_at)
                    self.assertEqual(load_state(Path(directory)/"state.json")["updated_at"], stamp)
                saved = load_state(Path(directory)/"state.json")
                self.assertIn("gmail", saved["last_channel_errors"])
                self.assertNotIn("discord", saved["last_channel_errors"])
                self.assertEqual(sent.call_count, 1)  # Failed Gmail must not repeat the Discord alert.
                smtp.assert_not_called()
            lines = [line for p in Path(directory).glob("history-*.jsonl") for line in p.read_text().splitlines()]
            self.assertEqual([json.loads(line)["timestamp"] for line in lines], [100, 115])

    def test_corrupted_state_fields_do_not_stop_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "last_alert": "not-a-number",
                        "last_email_status": {"broken": True},
                        "normal_since": [1, 2],
                        "notified_disk_conditions": "oops",
                        "disk_normal_since": ["oops"],
                        "condition_since": {"warning.available_ram": "junk"},
                        "metrics": {
                            "timestamp": "soon",
                            "mem_total_kib": 1,
                            "mem_available_kib": 1,
                            "swap_total_kib": 1,
                            "swap_free_kib": 1,
                            "psi_some_avg60": 0,
                            "psi_full_avg60": 0,
                            "pswpout_pages": 0,
                            "oom_kills": 0,
                        },
                    }
                )
            )
            with (
                patch("wsl_resource_guard.daemon.read_system_metrics", return_value=metrics(8.0)),
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
                patch("wsl_resource_guard.daemon.Notifier") as notifier,
            ):
                notifier.return_value.send.return_value = []
                _, _, evaluation = sample(settings, notify=True)
            self.assertEqual(evaluation.severity, "normal")
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["severity"], "normal")
            self.assertEqual(saved["notified_disk_conditions"], [])
            self.assertEqual(saved["disk_normal_since"], {})
            self.assertEqual(saved["last_channel_errors"], {})

    def test_read_only_sample_does_not_write_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            state_path = Path(directory) / "state.json"
            state_path.write_text('{"severity":"warning","updated_at":1}')
            before = state_path.read_bytes()
            with (
                patch("wsl_resource_guard.daemon.read_system_metrics", return_value=metrics(8.0)),
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
            ):
                sample(settings, persist=False)
            self.assertEqual(state_path.read_bytes(), before)

    def test_channel_failures_are_recorded_and_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            state_path = Path(directory) / "state.json"
            with (
                patch("wsl_resource_guard.daemon.read_system_metrics") as read_metrics,
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
                patch("wsl_resource_guard.daemon.Notifier") as notifier,
            ):
                notifier.return_value.send.return_value = [
                    NotificationResult("gmail", False, "smtp refused")
                ]
                read_metrics.return_value = metrics(1.0, timestamp=100)
                sample(settings, notify=True)
                self.assertEqual(
                    load_state(state_path)["last_channel_errors"], {"gmail": "smtp refused"}
                )
                notifier.return_value.send.return_value = [
                    NotificationResult("gmail", True, "sent")
                ]
                read_metrics.return_value = metrics(
                    1.0, timestamp=100 + settings.critical_reminder_seconds
                )
                sample(settings, notify=True)
                self.assertEqual(load_state(state_path)["last_channel_errors"], {})

    def test_skipped_channels_clear_old_not_configured_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps({"last_channel_errors": {
                "discord": "webhook not configured", "gmail": "smtp refused"}}))
            with (
                patch("wsl_resource_guard.daemon.read_system_metrics", return_value=metrics(8.0)),
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
                patch("wsl_resource_guard.metrics.POWERSHELL", Path("/nonexistent/powershell.exe")),
            ):
                sample(settings, notify=True)
            self.assertEqual(load_state(state_path)["last_channel_errors"], {"gmail": "smtp refused"})
            with (
                patch("wsl_resource_guard.daemon.read_system_metrics", return_value=metrics(1.0, timestamp=200)),
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
                patch("wsl_resource_guard.metrics.POWERSHELL", Path("/nonexistent/powershell.exe")),
                patch("wsl_resource_guard.daemon.Notifier") as notifier,
            ):
                notifier.return_value.send.return_value = [
                    NotificationResult("gmail", True, "sent"),
                    NotificationResult("discord", False, "webhook not configured", skipped=True),
                ]
                sample(settings, notify=True)
            self.assertEqual(load_state(state_path)["last_channel_errors"], {})

    def test_vmmem_attempt_time_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            with (
                patch("wsl_resource_guard.daemon.read_system_metrics", return_value=metrics(8.0)),
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
                patch("wsl_resource_guard.daemon.read_host_memory", return_value=(None, 0.0, 123.0)) as host,
            ):
                sample(settings)
                sample(settings)
            saved = load_state(Path(directory) / "state.json")["metrics"]
            self.assertEqual(saved["vmmem_attempted_at"], 123.0)
            self.assertEqual(host.call_args.kwargs["previous_attempt"], 123.0)

    def test_state_from_the_previous_version_is_still_a_valid_previous_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            old = metrics(8.0, timestamp=100).to_dict()
            del old["vmmem_attempted_at"]
            (Path(directory) / "state.json").write_text(json.dumps({"metrics": old}))
            with (
                patch("wsl_resource_guard.daemon.read_system_metrics", return_value=metrics(8.0, timestamp=115)) as read,
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
            ):
                sample(settings)
            self.assertEqual(read.call_args.args[0].timestamp, 100)

    def test_disk_history_is_hourly_while_resource_history_stays_per_minute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            last = (0.0, 0.0)
            start = datetime(2026, 1, 1, 9, 0).timestamp()
            for minute in (0, 1, 2, 59, 60, 61):
                sampled = metrics(8.0, timestamp=start + minute * 60)
                sampled.disks = [{"id": "C:", "kind": "windows", "status": "ok"}]
                last = record_history(settings, sampled, snapshot(), Evaluation("normal", [], []), *last)
            day = datetime.fromtimestamp(start).strftime("%Y-%m-%d")
            history = (Path(directory) / f"history-{day}.jsonl").read_text().splitlines()
            disk_history = (Path(directory) / f"disk-history-{day}.jsonl").read_text().splitlines()
            self.assertEqual(len(history), 6)
            self.assertEqual([json.loads(line)["timestamp"] for line in disk_history],
                             [start, start + 3600])

    def test_history_files_use_local_date_and_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "history-2000-01-01.jsonl").write_text("old\n")
            stamp = datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc).timestamp()
            append_history(path, {"timestamp": stamp}, 14)
            local_day = datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%d")
            utc_day = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d")
            self.assertTrue((path / f"history-{local_day}.jsonl").exists())
            self.assertEqual(
                json.loads((path / f"history-{local_day}.jsonl").read_text()),
                {"timestamp": stamp},
            )
            if local_day != utc_day:
                self.assertFalse((path / f"history-{utc_day}.jsonl").exists())
            self.assertFalse((path / "history-2000-01-01.jsonl").exists())

    def test_recovery_message_reports_abnormal_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "severity": "warning",
                        "notified_severity": "warning",
                        "notified_reasons": ["가용 RAM 3.5 GiB"],
                        "reasons": ["가용 RAM 3.5 GiB"],
                        "normal_since": 880,
                        "abnormal_since": 60,
                        "metrics": metrics(12, timestamp=985).to_dict(),
                        "condition_since": {},
                    }
                )
            )
            sent: dict[str, str] = {}

            def capture(title: str, message: str, severity: str, **kwargs: object) -> list:
                sent.update(severity=severity, message=message)
                return []

            with (
                patch(
                    "wsl_resource_guard.daemon.read_system_metrics",
                    return_value=metrics(8.0, timestamp=1000),
                ),
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
                patch("wsl_resource_guard.daemon.Notifier") as notifier,
            ):
                notifier.return_value.send.side_effect = capture
                sample(settings, notify=True)
            self.assertEqual(sent["severity"], "recovery")
            # 60 -> 880 (start of the normal streak); the confirmation hold
            # up to 1000 is not part of the abnormal episode.
            self.assertIn("약 13분 동안 지속됐습니다", sent["message"])

    def test_recovery_without_recorded_start_claims_no_duration(self) -> None:
        # State written before abnormal_since existed: the start is unknown.
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "severity": "warning",
                        "notified_severity": "warning",
                        "notified_reasons": ["가용 RAM 3.5 GiB"],
                        "reasons": ["가용 RAM 3.5 GiB"],
                        "normal_since": 880,
                        "condition_since": {},
                        "metrics": metrics(12, timestamp=985).to_dict(),
                    }
                )
            )
            sent: dict[str, str] = {}

            def capture(title: str, message: str, severity: str, **kwargs: object) -> list:
                sent.update(severity=severity, message=message)
                return []

            with (
                patch(
                    "wsl_resource_guard.daemon.read_system_metrics",
                    return_value=metrics(8.0, timestamp=1000),
                ),
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
                patch("wsl_resource_guard.daemon.Notifier") as notifier,
            ):
                notifier.return_value.send.side_effect = capture
                sample(settings, notify=True)
            self.assertEqual(sent["severity"], "recovery")
            self.assertNotIn("동안 지속됐습니다", sent["message"])


    def test_recovery_duration_excludes_the_two_minute_hold(self) -> None:
        # Drives real ticks: 60 s critical, then normal until the 120 s hold ends.
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            sent: list[tuple[str, str]] = []

            def capture(title: str, message: str, severity: str, **kwargs: object) -> list:
                sent.append((severity, message))
                return []

            with (
                patch("wsl_resource_guard.daemon.read_disks", return_value=[]),
                patch("wsl_resource_guard.daemon.build_snapshot", return_value=snapshot()),
                patch("wsl_resource_guard.metrics.POWERSHELL", Path("/nonexistent/powershell.exe")),
                patch("wsl_resource_guard.daemon.Notifier") as notifier,
                patch("wsl_resource_guard.daemon.read_system_metrics") as read_metrics,
            ):
                notifier.return_value.send.side_effect = capture
                for tick in range(16):
                    stamp = 1000 + tick * 15
                    read_metrics.return_value = metrics(1.5 if tick < 4 else 8.0, timestamp=stamp)
                    sample(settings, notify=True)
            self.assertEqual([severity for severity, _ in sent], ["critical", "recovery"])
            # 1000 -> 1060: the abnormal span, not the 120 s confirmation hold.
            self.assertIn("약 1분 동안 지속됐습니다", sent[-1][1])
            self.assertEqual(load_state(Path(directory) / "state.json")["abnormal_since"], 0.0)


class SampleLoggingTest(unittest.TestCase):
    def test_logs_first_sample_and_every_interval(self) -> None:
        self.assertTrue(should_log_sample("normal", None, 100, None))
        self.assertFalse(should_log_sample("normal", "normal", 699, 100))
        self.assertTrue(should_log_sample("normal", "normal", 700, 100))
        self.assertTrue(should_log_sample("normal", "normal", 100 + 600, 100))

    def test_logs_on_severity_change_only(self) -> None:
        self.assertTrue(should_log_sample("warning", "normal", 200, 100))
        self.assertFalse(should_log_sample("warning", "warning", 300, 200))


if __name__ == "__main__":
    unittest.main()


class ConfigRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile as _tempfile
        self.temp = _tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.shared = root / "shared"
        self.shared.mkdir()
        self.config = root / "config.toml"
        self.config.write_text("# mine\nwarning_available_gib = 4.0\n")
        self.settings = Settings(config_path=self.config, state_dir=str(root / "state"))

    def request(self, **fields) -> None:
        import json as _json
        body = {"id": "abc", "key": "warning_available_gib", "value": 5.5, "requested_at": 1000.0}
        body.update(fields)
        (self.shared / "alert-snooze.json").unlink(missing_ok=True)
        (self.shared / "config-request.json").write_text(_json.dumps(body))

    def test_applies_once_and_keeps_comments(self) -> None:
        from wsl_resource_guard.daemon import apply_config_request
        self.request()
        result = apply_config_request(self.settings, self.shared, now=1010.0)
        self.assertTrue(result["ok"], result)
        self.assertIn("warning_available_gib = 5.5", self.config.read_text())
        self.assertIn("# mine", self.config.read_text())
        self.assertIsNone(apply_config_request(self.settings, self.shared, now=1020.0))

    def test_rejects_switches_relations_and_stale_requests(self) -> None:
        from wsl_resource_guard.daemon import apply_config_request
        self.request(id="a", key="gmail_enabled", value=False)
        self.assertFalse(apply_config_request(self.settings, self.shared, now=1010.0)["ok"])
        self.request(id="b", value=1.0)  # below critical_available_gib (2.0)
        self.assertIn("보다 커야", apply_config_request(self.settings, self.shared, now=1010.0)["message"])
        self.request(id="c")
        self.assertIn("만료", apply_config_request(self.settings, self.shared, now=5000.0)["message"])
        self.assertIn("warning_available_gib = 4.0", self.config.read_text())


class WeeklyReportTests(unittest.TestCase):
    def test_slot_fires_once_per_iso_week_after_the_hour(self) -> None:
        from wsl_resource_guard.daemon import weekly_report_slot
        settings = Settings(weekly_report_weekday=0, weekly_report_hour=9)
        monday_9 = datetime(2026, 10, 5, 9, 0).timestamp()
        monday_8 = datetime(2026, 10, 5, 8, 59).timestamp()
        tuesday = datetime(2026, 10, 6, 10, 0).timestamp()
        slot = weekly_report_slot(monday_9, "", settings)
        self.assertEqual(slot, "2026-W41")
        self.assertIsNone(weekly_report_slot(monday_9 + 3600, slot, settings))
        self.assertIsNone(weekly_report_slot(monday_8, "", settings))
        self.assertIsNone(weekly_report_slot(tuesday, "", settings))
        settings.weekly_report_enabled = False
        self.assertIsNone(weekly_report_slot(monday_9, "", settings))

    def test_report_names_missing_data_instead_of_zero(self) -> None:
        import tempfile as _tempfile
        from wsl_resource_guard.reporting import build_weekly_report
        with _tempfile.TemporaryDirectory() as directory:
            title, text, html_body = build_weekly_report(Path(directory), 1_790_000_000.0)
        self.assertIn("주간 리포트", title)
        self.assertIn("가용 RAM 기록 없음", text)
        self.assertIn("0회", text)
        self.assertIn("<h3>디스크</h3>", html_body)
