"""Shared durable writes, the systemd watchdog hand-off and weekly catch-up."""
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard.config import Settings

ROOT = Path(__file__).resolve().parents[1]


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_json_is_replaced_durably_with_the_requested_mode(self):
        from wsl_resource_guard import atomic
        path = self.root / 'nested' / 'state.json'
        with patch.object(atomic.os, 'fsync', wraps=os.fsync) as fsync:
            atomic.write_json(path, {'값': 1}, mode=0o640, indent=2, trailing_newline=True)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'값': 1})
        self.assertIn('값', path.read_text(encoding='utf-8'))  # ensure_ascii is off
        self.assertTrue(path.read_text(encoding='utf-8').endswith('}\n'))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertGreaterEqual(fsync.call_count, 2)  # the file, then its directory
        self.assertEqual([p.name for p in path.parent.iterdir()], ['state.json'])

    def test_compact_json_matches_the_state_file_format(self):
        from wsl_resource_guard import atomic
        path = self.root / 'state.json'
        atomic.write_json(path, {'a': [1, 2]}, compact=True, trailing_newline=True)
        self.assertEqual(path.read_text(), '{"a":[1,2]}\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_replace_keeps_the_previous_file_and_leaves_no_temporary(self):
        from wsl_resource_guard import atomic
        path = self.root / 'state.json'
        path.write_text('{"old":true}')
        with patch.object(atomic.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                atomic.write_json(path, {'new': True})
        self.assertEqual(path.read_text(), '{"old":true}')
        self.assertEqual([p.name for p in self.root.iterdir()], ['state.json'])

    def test_unserializable_value_never_touches_the_target(self):
        from wsl_resource_guard import atomic
        path = self.root / 'state.json'
        path.write_text('{"old":true}')
        with self.assertRaises(TypeError):
            atomic.write_json(path, {'bad': object()})
        self.assertEqual(path.read_text(), '{"old":true}')
        self.assertEqual([p.name for p in self.root.iterdir()], ['state.json'])

    def test_directory_sync_failure_does_not_fail_a_completed_write(self):
        from wsl_resource_guard import atomic
        path = self.root / 'state.json'
        real = os.open

        def refuse_directory(target, flags, *args, **kwargs):
            if flags & os.O_DIRECTORY:
                raise OSError('unsupported')
            return real(target, flags, *args, **kwargs)
        with patch.object(atomic.os, 'open', side_effect=refuse_directory):
            atomic.write_text(path, 'done')
        self.assertEqual(path.read_text(), 'done')

    def test_runtime_writers_share_the_one_implementation(self):
        # A second hand-rolled temp-file writer is how fsync and atomicity drifted apart.
        for name in ('daemon', 'incidents', 'service_control', 'config', 'cli', 'webpush', 'delivery', 'actions'):
            with self.subTest(module=name):
                source = (ROOT / 'wsl_resource_guard' / f'{name}.py').read_text()
                self.assertNotIn('mkstemp', source)
        self.assertNotIn('.write_text(', (ROOT / 'wsl_resource_guard/webpush.py').read_text())

    def test_existing_entry_points_keep_their_file_formats(self):
        from wsl_resource_guard import daemon, incidents, service_control
        daemon.save_state(self.root / 'state.json', {'a': 1})
        incidents.atomic_write(self.root / 'incidents.json', {'a': 1})
        service_control.atomic_json(self.root / 'registry.json', {'a': 1}, mode=0o640)
        self.assertEqual((self.root / 'state.json').read_text(), '{"a":1}\n')
        self.assertEqual((self.root / 'incidents.json').read_text(), '{"a": 1}')
        self.assertEqual((self.root / 'registry.json').read_text(), '{\n  "a": 1\n}\n')
        self.assertEqual(stat.S_IMODE((self.root / 'state.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / 'incidents.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / 'registry.json').stat().st_mode), 0o640)


class WatchdogTests(unittest.TestCase):
    def listener(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(server.close)
        path = str(Path(temp.name) / 'notify.sock')
        server.bind(path)
        server.settimeout(10)
        return server, path

    def test_notify_reaches_the_manager_socket(self):
        from wsl_resource_guard.sdnotify import notify
        server, path = self.listener()
        with patch.dict(os.environ, {'NOTIFY_SOCKET': path}):
            self.assertTrue(notify('READY=1'))
        self.assertEqual(server.recv(64), b'READY=1')

    def test_notify_is_a_quiet_no_op_without_or_with_a_broken_socket(self):
        from wsl_resource_guard.sdnotify import notify
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NOTIFY_SOCKET', None)
            self.assertFalse(notify('WATCHDOG=1'))
        with patch.dict(os.environ, {'NOTIFY_SOCKET': '/nonexistent/wrg/notify.sock'}):
            self.assertFalse(notify('WATCHDOG=1'))
        with patch.dict(os.environ, {'NOTIFY_SOCKET': 'relative.sock'}):
            self.assertFalse(notify('WATCHDOG=1'))

    def test_units_declare_notify_and_a_watchdog_longer_than_a_sample(self):
        for name in ('wsl-resource-guard.service', 'wsl-resource-guard.user.service'):
            with self.subTest(unit=name):
                text = (ROOT / 'packaging' / name).read_text()
                for line in ('Type=notify', 'NotifyAccess=main', 'WatchdogSec=180', 'Restart=always'):
                    self.assertIn(line + '\n', text)

    def test_daemon_reports_ready_then_keeps_the_watchdog_fed_between_samples(self):
        server, path = self.listener()
        with tempfile.TemporaryDirectory() as root:
            code = """
import sys
sys.path.insert(0, 'tests')
from test_daemon import metrics, snapshot
from wsl_resource_guard import daemon
from wsl_resource_guard.config import Settings
daemon.sample = lambda *a, **kw: (metrics(12), snapshot(), daemon.Evaluation('normal', [], []))
daemon.WATCHDOG_FEED_SECONDS = .2
daemon.run_forever(Settings(state_dir=sys.argv[1], interval_seconds=600, weekly_report_enabled=False))
"""
            process = subprocess.Popen([sys.executable, '-u', '-c', code, root], cwd=ROOT,
                                       env=dict(os.environ, NOTIFY_SOCKET=path),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                received = [server.recv(64) for _ in range(3)]
            finally:
                process.terminate()
                _, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err)
        # With a 600 s interval the later signals can only come from the wait loop.
        self.assertEqual(received[0], b'READY=1')
        self.assertEqual(received[1:], [b'WATCHDOG=1', b'WATCHDOG=1'])

    def test_a_stuck_sample_stops_feeding_the_watchdog(self):
        server, path = self.listener()
        with tempfile.TemporaryDirectory() as root:
            code = """
import sys, time
from wsl_resource_guard import daemon
from wsl_resource_guard.config import Settings
daemon.sample = lambda *a, **kw: time.sleep(60)
daemon.run_forever(Settings(state_dir=sys.argv[1], interval_seconds=5, weekly_report_enabled=False))
"""
            process = subprocess.Popen([sys.executable, '-u', '-c', code, root], cwd=ROOT,
                                       env=dict(os.environ, NOTIFY_SOCKET=path),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertEqual(server.recv(64), b'READY=1')
                server.settimeout(3)
                with self.assertRaises(TimeoutError):
                    server.recv(64)
            finally:
                process.kill()
                process.wait(timeout=10)


class WeeklyCatchUpTests(unittest.TestCase):
    def test_a_missed_day_is_caught_up_within_the_same_iso_week(self):
        from wsl_resource_guard.daemon import weekly_report_slot
        settings = Settings(weekly_report_weekday=2, weekly_report_hour=9)  # Wednesday 09:00
        at = lambda day, hour, minute=0: datetime(2026, 10, day, hour, minute).timestamp()
        self.assertIsNone(weekly_report_slot(at(6, 23), '', settings))        # Tuesday
        self.assertIsNone(weekly_report_slot(at(7, 8, 59), '', settings))     # Wednesday before the hour
        self.assertEqual(weekly_report_slot(at(7, 9), '', settings), '2026-W41')
        self.assertEqual(weekly_report_slot(at(9, 3), '', settings), '2026-W41')   # Friday: PC was off on Wednesday
        self.assertEqual(weekly_report_slot(at(11, 23), '', settings), '2026-W41')  # Sunday, still that week
        self.assertIsNone(weekly_report_slot(at(9, 3), '2026-W41', settings))  # Already sent this week
        self.assertIsNone(weekly_report_slot(at(12, 12), '2026-W41', settings))  # Next Monday: not due yet


class DataFormatTests(unittest.TestCase):
    def written(self):
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_actionable_alerts import fixture, metrics
        from wsl_resource_guard import daemon
        from wsl_resource_guard.delivery import Delivery
        from wsl_resource_guard.notifications import NotificationResult
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        shared = root / 'shared'
        shared.mkdir()
        settings = Settings(state_dir=str(root), secrets_path=root / 'secrets.json', windows_toast_enabled=False,
                            discord_enabled=False, push_enabled=False)
        settings.secrets_path.write_text(json.dumps(
            {'gmail_to': 'one@example.com', 'gmail_user': 'fixture', 'gmail_app_password': 'fixture'}))
        with (patch.object(daemon, 'build_snapshot', return_value=fixture()),
              patch.object(daemon, 'read_disks', return_value=[]),
              patch.object(daemon, 'read_host_memory', return_value=(None, 0, 0)),
              patch.object(daemon, 'read_system_metrics', return_value=metrics(1000, 1)),
              patch('builtins.print')):
            sampled = daemon.sample(settings, incident_mode=True, shared_dir=shared)
            Delivery(settings, shared, clock=lambda: 1000).tick(
                send=lambda *a: NotificationResult('gmail', False, 'HTTP 500'))
        read = lambda name: json.loads((root / name).read_text())
        return read('state.json'), read('incidents.json'), read('delivery.json'), daemon._history_record(*sampled)

    def test_document_names_every_field_the_guard_writes(self):
        text = (ROOT / 'docs/data-formats.md').read_text()
        state, incidents, ledger, history = self.written()
        event = next(iter(ledger['events'].values()))
        groups = {'state.json': state, 'metrics': state['metrics'], 'incidents.json': incidents,
                  'incident row': incidents['incidents'][0], 'delivery.json': ledger, 'delivery event': event,
                  'destination': next(iter(event['destinations'].values())), 'history record': history}
        missing = sorted(f'{group}: {key}' for group, value in groups.items() for key in value
                         if f'`{key}`' not in text)
        self.assertEqual(missing, [])

    def test_versions_match_the_document_and_unknown_metric_fields_are_ignored(self):
        from wsl_resource_guard import daemon
        state, incidents, ledger, _ = self.written()
        self.assertEqual((state['version'], incidents['version'], ledger['version']), (1, 1, 1))
        newer = dict(state, metrics=dict(state['metrics'], host={'schema': 1, 'memory': {}}))
        restored = daemon._metrics_from_state(newer)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.mem_total_kib, state['metrics']['mem_total_kib'])

    def test_document_lists_every_controller_operation(self):
        import re
        text = (ROOT / 'docs/data-formats.md').read_text()
        source = (ROOT / 'wsl_resource_guard/service_control.py').read_text()
        dispatch = source[source.index('    def dispatch(self, request: dict'):source.index('class ControlServer')]
        operations = set(re.findall(r"op == '([a-z-]+)'", dispatch))
        for group in re.findall(r"op in \(([^)]*)\)", dispatch):
            operations.update(re.findall(r"'([a-z-]+)'", group))
        self.assertGreater(len(operations), 25)
        self.assertEqual(sorted(op for op in operations if f'`{op}`' not in text), [])


if __name__ == '__main__':
    unittest.main()
