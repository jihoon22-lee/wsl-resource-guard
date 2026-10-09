from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import signal
import tempfile
import time
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wsl_resource_guard import cli
from wsl_resource_guard.config import Settings
from wsl_resource_guard.daemon import Evaluation
from wsl_resource_guard.notifications import NotificationResult
from test_daemon import metrics, snapshot


class ReadOnlyCommandTests(unittest.TestCase):
    def test_status_reads_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            args = SimpleNamespace(config=None)
            with (
                patch.object(cli, "_load_settings", return_value=settings),
                patch.object(
                    cli,
                    "sample",
                    return_value=(metrics(8.0), snapshot(), Evaluation("normal", [], [])),
                ) as sampled,
                patch.object(cli, "Notifier") as notifier,
            ):
                notifier.return_value.channel_status.return_value = {}
                self.assertEqual(cli.cmd_status(args), 0)
            sampled.assert_called_once_with(settings, notify=False, persist=False)

    def test_test_alert_reads_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            args = SimpleNamespace(channels=None)
            with (
                patch.object(cli, "_load_settings", return_value=settings),
                patch.object(
                    cli,
                    "sample",
                    return_value=(metrics(8.0), snapshot(), Evaluation("normal", [], [])),
                ) as sampled,
                patch.object(cli, "Notifier") as notifier,
            ):
                notifier.return_value.send.return_value = [
                    NotificationResult("toast", True, "ok")
                ]
                self.assertEqual(cli.cmd_test_alert(args), 0)
            sampled.assert_called_once_with(settings, notify=False, persist=False)



    def run_test_alert(self, results: list) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            out, err = io.StringIO(), io.StringIO()
            with (
                patch.object(cli, "_load_settings", return_value=settings),
                patch.object(cli, "sample",
                             return_value=(metrics(8.0), snapshot(), Evaluation("normal", [], []))),
                patch.object(cli, "Notifier") as notifier,
                redirect_stdout(out), redirect_stderr(err),
            ):
                notifier.return_value.send.return_value = results
                code = cli.cmd_test_alert(SimpleNamespace(channels=None))
        return code, out.getvalue(), err.getvalue()

    def test_test_alert_skipped_channel_is_not_a_failure(self) -> None:
        code, out, _ = self.run_test_alert([
            NotificationResult("gmail", True, "sent"),
            NotificationResult("discord", False, "webhook not configured", skipped=True),
        ])
        self.assertEqual(code, 0)
        self.assertIn("SKIP", out)

    def test_test_alert_with_nothing_sent_fails(self) -> None:
        code, _, err = self.run_test_alert([
            NotificationResult("discord", False, "webhook not configured", skipped=True),
        ])
        self.assertEqual(code, 1)
        self.assertIn("전송한 채널이 없습니다", err)



    def test_disks_tolerates_state_without_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory, disk_drives=[])
            (Path(directory) / "state.json").write_text('{"metrics": null}')
            with (
                patch.object(cli, "_load_settings", return_value=settings),
                patch.object(cli, "read_disks", return_value=[]) as read,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.cmd_disks(SimpleNamespace(json=True)), 0)
            self.assertEqual(read.call_args.args[2], [])


class ConfigCommandTests(unittest.TestCase):
    def run_config(self, directory: str, config_text: str, action=None, key=None, value=None):
        path = Path(directory) / "config.toml"
        path.write_text(config_text, encoding="utf-8")
        args = SimpleNamespace(config=str(path), config_action=action, key=key, value=value)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.cmd_config(args)
        return code, path.read_text(encoding="utf-8"), out.getvalue(), err.getvalue()

    def test_set_validates_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, text, out, _ = self.run_config(
                directory, "warning_available_gib = 4.0\n", "set", "warning_available_gib", "6.5"
            )
            self.assertEqual(code, 0)
            self.assertIn("warning_available_gib = 6.5", text)
            self.assertIn("저장", out)
            # 변경된 파일이 실제로 파싱된다
            self.assertEqual(Settings.load(Path(directory) / "config.toml").warning_available_gib, 6.5)

    def test_set_appends_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, text, _, _ = self.run_config(
                directory, "interval_seconds = 15\n", "set", "retention_days", "30"
            )
            self.assertEqual(code, 0)
            self.assertIn("retention_days = 30", text)
            self.assertIn("interval_seconds = 15", text)

    def test_set_replaces_quoted_key(self) -> None:
        for written in ('"retention_days" = 14', "'retention_days' = 14"):
            with self.subTest(written=written), tempfile.TemporaryDirectory() as directory:
                code, text, _, err = self.run_config(
                    directory, f"interval_seconds = 15\n{written}\n", "set", "retention_days", "30"
                )
                self.assertEqual(code, 0, err)
                self.assertEqual(text, "interval_seconds = 15\nretention_days = 30\n")

    def test_set_rejects_invalid_values(self) -> None:
        cases = [
            ("warning_available_gib", "-1", "범위"),
            ("interval_seconds", "abc", "숫자"),
            ("email_heartbeat_enabled", "maybe", "true/false"),
            ("alert_quiet_hours", "25-99", "형식"),
            ("host_memory_refresh_seconds", "30", "범위"),
            ("host_memory_refresh_seconds", "7200", "범위"),
            ("unknown_key", "1", "알 수 없는"),
        ]
        for key, value, hint in cases:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                code, text, _, err = self.run_config(directory, "", "set", key, value)
                self.assertEqual(code, 2)
                self.assertIn(hint, err)
                self.assertEqual(text, "")

    def test_set_host_memory_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, text, _, _ = self.run_config(
                directory, "", "set", "host_memory_refresh_seconds", "600"
            )
            self.assertEqual(code, 0)
            self.assertIn("host_memory_refresh_seconds = 600", text)

    def test_set_enforces_warning_above_critical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, text, _, err = self.run_config(
                directory, "critical_available_gib = 2.0\n", "set", "warning_available_gib", "1.0"
            )
            self.assertEqual(code, 2)
            self.assertIn("critical_available_gib", err)
            self.assertNotIn("warning_available_gib = 1.0", text)

    def test_quiet_hours_accepted(self) -> None:
        for value in ("23-08", "23:30-07:15", ""):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                code, text, _, _ = self.run_config(directory, "", "set", "alert_quiet_hours", value)
                self.assertEqual(code, 0)
                self.assertIn(f'alert_quiet_hours = "{value}"', text)

    def test_set_replaces_only_root_level_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, text, _, _ = self.run_config(
                directory, "interval_seconds = 15\n[extra]\ninterval_seconds = 99\n",
                "set", "interval_seconds", "30"
            )
            self.assertEqual(code, 0)
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["interval_seconds"], 30)
            self.assertEqual(parsed["extra"]["interval_seconds"], 99)

    def test_set_appends_at_root_level_before_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, text, _, _ = self.run_config(
                directory, "[extra]\nx = 1\n", "set", "interval_seconds", "30"
            )
            self.assertEqual(code, 0)
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["interval_seconds"], 30)
            self.assertEqual(parsed["extra"]["x"], 1)

    def test_set_refuses_replacement_inside_a_string(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = 'notes = """\ninterval_seconds = 5\n"""\n'
            code, text, _, err = self.run_config(
                directory, original, "set", "interval_seconds", "30"
            )
            self.assertEqual(code, 2)
            self.assertEqual(text, original)
            self.assertIn("치환 결과", err)

    def test_list_shows_current_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, _, out, _ = self.run_config(directory, "interval_seconds = 30\n")
            self.assertEqual(code, 0)
            self.assertIn("interval_seconds", out)
            self.assertIn("30", out)


class StopCommandTests(unittest.TestCase):
    def test_protected_root_or_child_blocks_every_signal_before_execution(self):
        for protected_pid in (10, 11):
            for command in (cli.cmd_stop, cli.cmd_stop_mcp):
                with self.subTest(protected_pid=protected_pid, command=command.__name__):
                    snap = self.make_snapshot()
                    snap.processes[protected_pid].name = 'codex'
                    snap.processes[protected_pid].command = 'codex app-server'
                    args = SimpleNamespace(config=None, pid=10, confirm=True, kill=True, timeout=0)
                    with (patch.object(cli, '_load_settings', return_value=Settings()),
                          patch.object(cli, 'build_snapshot', return_value=snap),
                          patch.object(cli, 'signal_verified_process') as send,
                          redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                        self.assertEqual(command(args), 2)
                    send.assert_not_called()

    def make_snapshot(self):
        import os
        from wsl_resource_guard.processes import ProcessInfo, ProcessSnapshot, SessionUsage, McpUsage
        processes = {pid: ProcessInfo(
            pid=pid, ppid=1 if pid == 10 else 10, uid=os.getuid(), name="proc", state="S",
            rss_kib=1, swap_kib=0, age_seconds=10, cwd="/", cgroup="/user.slice/x.scope",
            command="x", start_ticks=pid*10) for pid in (10, 11)}
        return ProcessSnapshot(processes,
            [SessionUsage(root_pid=10, provider="claude", root_name="claude", project="demo", age_seconds=100)],
            [], {}, {10:"claude"},
            [McpUsage(root_pid=10, session_root_pid=10, provider="claude", project="demo", root_name="mcp", age_seconds=100)])

    def test_kill_skips_pid_reused_after_sigterm(self):
        snap = self.make_snapshot()
        args = SimpleNamespace(config=None, pid=10, confirm=True, kill=True, timeout=0.01)
        with (patch.object(cli, "_load_settings", return_value=Settings()),
              patch.object(cli, "build_snapshot", return_value=snap),
              patch('wsl_resource_guard.processes._read_process', side_effect=lambda pid, *_: snap.processes[pid]),
              patch('os.pidfd_open', side_effect=lambda pid: pid+1000), patch('os.close'),
              patch.object(cli, 'process_start_ticks', side_effect=lambda pid:100 if pid == 10 else 999),
              patch('signal.pidfd_send_signal') as send, patch('os.kill') as numeric,
              redirect_stdout(io.StringIO())):
            code = cli.cmd_stop(args)
        self.assertEqual(code, 0)
        self.assertEqual([call.args[0] for call in send.call_args_list if call.args[1]==signal.SIGKILL],[1010])
        numeric.assert_not_called()

    def test_exited_child_is_not_signalled_and_term_uses_snapshot_identity(self):
        snap = self.make_snapshot()
        args = SimpleNamespace(config=None, pid=10, confirm=True, kill=True, timeout=0.01)
        with (patch.object(cli, "_load_settings", return_value=Settings()),
              patch.object(cli, "build_snapshot", return_value=snap),
              patch('wsl_resource_guard.processes._read_process', side_effect=lambda pid, *_:snap.processes[pid] if pid==10 else None),
              patch('os.pidfd_open', side_effect=lambda pid:pid+1000), patch('os.close'),
              patch.object(cli, 'process_start_ticks', return_value=None),
              patch('signal.pidfd_send_signal') as send,
              redirect_stdout(io.StringIO())):
            code = cli.cmd_stop(args)
        self.assertEqual(code, 0)
        send.assert_called_once_with(1010, signal.SIGTERM)

    def test_zero_timeout_still_checks_survivors_before_reporting_success(self):
        snap = self.make_snapshot()
        args = SimpleNamespace(config=None, pid=10, confirm=True, kill=False, timeout=0)
        with (patch.object(cli, '_load_settings', return_value=Settings()),
              patch.object(cli, 'build_snapshot', return_value=snap),
              patch.object(cli, 'signal_verified_process', return_value=True),
              patch.object(cli, 'process_start_ticks', side_effect=lambda pid:pid*10),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(cli.cmd_stop(args), 1)

    def test_mcp_already_exited_does_not_claim_signal_sent(self):
        args = SimpleNamespace(config=None, pid=10, confirm=True)
        output = io.StringIO()
        with (patch.object(cli, '_load_settings', return_value=Settings()),
              patch.object(cli, 'build_snapshot', return_value=self.make_snapshot()),
              patch.object(cli, 'signal_verified_process', return_value=False),
              redirect_stdout(output)):
            self.assertEqual(cli.cmd_stop_mcp(args), 0)
        self.assertNotIn('SIGTERM을 보냈습니다', output.getvalue())

    def test_session_and_mcp_refuse_replacement_before_first_signal(self):
        from dataclasses import replace
        snap = self.make_snapshot()
        args = SimpleNamespace(config=None, pid=10, confirm=True, kill=True, timeout=0.01)
        for command in (cli.cmd_stop, cli.cmd_stop_mcp):
            with self.subTest(command=command.__name__):
                with (patch.object(cli, "_load_settings", return_value=Settings()),
                      patch.object(cli, "build_snapshot", return_value=snap),
                      patch('wsl_resource_guard.processes._read_process', side_effect=lambda pid, *_:replace(snap.processes[pid],start_ticks=999)),
                      patch('os.pidfd_open', return_value=987), patch('os.close'),
                      patch('signal.pidfd_send_signal') as send, patch('os.kill') as numeric,
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                    self.assertEqual(command(args),2)
                send.assert_not_called()
                numeric.assert_not_called()


class HistoryFilterTests(unittest.TestCase):
    def test_severity_and_hours_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            now = time.time()
            records = [
                {"timestamp": now - 7200, "severity": "warning", "metrics": {}},
                {"timestamp": now - 60, "severity": "critical", "metrics": {}},
                {"timestamp": now - 30, "severity": "warning", "metrics": {}},
            ]
            path = Path(directory) / f"history-{time.strftime('%Y-%m-%d')}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            args = SimpleNamespace(config=None, limit=20, severity="critical", hours=None, json=True)
            out = io.StringIO()
            with (
                patch.object(cli, "_load_settings", return_value=settings),
                redirect_stdout(out),
            ):
                self.assertEqual(cli.cmd_history(args), 0)
            rows = json.loads(out.getvalue())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["severity"], "critical")
            # 시간 필터
            args = SimpleNamespace(config=None, limit=20, severity=None, hours=1, json=True)
            out = io.StringIO()
            with (
                patch.object(cli, "_load_settings", return_value=settings),
                redirect_stdout(out),
            ):
                cli.cmd_history(args)
            self.assertEqual(len(json.loads(out.getvalue())), 2)

    def test_episodes_print_whole_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(state_dir=directory)
            now = time.time()
            # Float timestamps make duration_seconds a float; the printed
            # duration must still be whole minutes, not "1.0분".
            records = [
                {"timestamp": now - 120.5, "severity": "warning",
                 "reasons": ["가용 RAM 부족"]},
                {"timestamp": now - 60.5, "severity": "warning",
                 "reasons": ["가용 RAM 부족"]},
                {"timestamp": now - 0.5, "severity": "normal", "reasons": []},
            ]
            (Path(directory) / f"history-{time.strftime('%Y-%m-%d')}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            args = SimpleNamespace(config=None, episodes=True, hours=1, json=False)
            out = io.StringIO()
            with (
                patch.object(cli, "_load_settings", return_value=settings),
                redirect_stdout(out),
            ):
                self.assertEqual(cli.cmd_history(args), 0)
            self.assertIn("1분", out.getvalue())
            self.assertNotIn("1.0", out.getvalue())


if __name__ == "__main__":
    unittest.main()
