"""PSI availability, persistence and single-writer regressions."""
import contextlib
import io
from pathlib import Path
import tempfile
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wsl_resource_guard.config import Settings
from wsl_resource_guard import daemon, cli
from wsl_resource_guard.reporting import build_text_report, build_html_report
from wsl_resource_guard.history import downsample
from wsl_resource_guard.metrics import _read_memory_psi
from test_daemon import metrics, snapshot


class PsiTests(unittest.TestCase):
    def test_missing_error_and_zero_are_distinct(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'psi'
            self.assertEqual(_read_memory_psi(path), (None, None, 'missing'))
            for body in ('', 'some avg60=1', 'some avg60=nan\nfull avg60=0',
                         'some avg60=bad\nfull avg60=0'):
                path.write_text(body)
                self.assertEqual(_read_memory_psi(path), (None, None, 'error'))
            path.write_text('some avg60=0.00\nfull avg60=0.00\n')
            self.assertEqual(_read_memory_psi(path), (0.0, 0.0, 'normal'))
            with patch.object(Path, 'read_text', side_effect=PermissionError):
                self.assertEqual(_read_memory_psi(path), (None, None, 'error'))

    def test_nullable_persisted_metrics_and_legacy_numbers(self):
        old = metrics(12).to_dict()
        old.pop('psi_status', None)
        self.assertEqual(daemon._metrics_from_state({'metrics': old}).psi_status, 'legacy')
        old.update(psi_some_avg60=None, psi_full_avg60=None, psi_status='missing')
        self.assertIsNotNone(daemon._metrics_from_state({'metrics': old}))

    def test_missing_resets_pending_streak_without_resource_warning(self):
        settings = Settings()
        first = daemon.evaluate(metrics(12, psi_some=9), snapshot(), settings)
        gap = metrics(12, None, None, timestamp=1000)
        gap.psi_status = 'missing'
        absent = daemon.evaluate(gap, snapshot(), settings, condition_since=first.condition_since)
        self.assertEqual(absent.severity, 'normal')
        self.assertFalse(absent.condition_since)
        resumed = daemon.evaluate(metrics(12, psi_some=9, timestamp=1015), snapshot(), settings,
                                  condition_since=absent.condition_since)
        self.assertEqual(resumed.severity, 'normal')
        self.assertTrue(resumed.pending_reasons)

    def test_active_alert_survives_missing_and_restart_until_observed_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(state_dir=root, warning_psi_sustain_seconds=0,
                                recovery_sustain_seconds=30, disk_drives=[])
            readings = [metrics(12, psi_some=9, timestamp=100),
                        metrics(12, None, None, timestamp=200),
                        metrics(12, timestamp=220),
                        metrics(12, None, None, timestamp=260),
                        metrics(12, timestamp=300), metrics(12, timestamp=330)]
            for m in readings:
                m.psi_status = 'missing' if m.psi_some_avg60 is None else 'normal'
            with patch.object(daemon, 'read_system_metrics', side_effect=readings), \
                 patch.object(daemon, 'build_snapshot', return_value=snapshot()), \
                 patch.object(daemon, 'read_disks', return_value=[]), \
                 patch.object(daemon, 'read_host_memory', return_value=(None, 0, 0)):
                results = [daemon.sample(settings)[2] for _ in readings]
            self.assertEqual([r.severity for r in results],
                             ['warning', 'warning', 'warning', 'warning', 'warning', 'normal'])
            state = daemon.load_state(Path(root) / 'state.json')
            self.assertEqual(state['normal_since'], 300)

    def test_psi_recovery_remains_independent_of_concurrent_ram_warning(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(state_dir=root, warning_psi_sustain_seconds=0,
                                warning_available_sustain_seconds=0,
                                recovery_sustain_seconds=30, disk_drives=[])
            readings = [metrics(12, psi_some=9, timestamp=100),
                        metrics(3, timestamp=115),
                        metrics(3, None, None, timestamp=130),
                        *[metrics(12, None, None, timestamp=t) for t in (145, 160, 175)],
                        *[metrics(12, timestamp=t) for t in (190, 205, 220)]]
            for m in readings:
                m.psi_status = 'missing' if m.psi_some_avg60 is None else 'normal'
            states = []
            with patch.object(daemon, 'read_system_metrics', side_effect=readings), \
                 patch.object(daemon, 'build_snapshot', return_value=snapshot()), \
                 patch.object(daemon, 'read_disks', return_value=[]), \
                 patch.object(daemon, 'read_host_memory', return_value=(None, 0, 0)):
                for _ in readings:
                    daemon.sample(settings)
                    states.append(daemon.load_state(Path(root) / 'state.json'))
            self.assertEqual([s['severity'] for s in states], ['warning'] * 8 + ['normal'])
            self.assertEqual(states[1]['psi_alert']['severity'], 'warning')
            self.assertEqual(states[2]['psi_normal_since'], 0)
            self.assertEqual(states[-1]['psi_alert'], {})

    def test_completed_psi_recovery_does_not_block_unrelated_ram_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(state_dir=root, warning_psi_sustain_seconds=0,
                                warning_available_sustain_seconds=0,
                                recovery_sustain_seconds=30, disk_drives=[])
            readings = [metrics(12, psi_some=9, timestamp=100),
                        *[metrics(3, timestamp=t) for t in (115, 130, 145)],
                        *[metrics(12, None, None, timestamp=t) for t in (160, 175, 190)]]
            for m in readings:
                m.psi_status = 'missing' if m.psi_some_avg60 is None else 'normal'
            states = []
            with patch.object(daemon, 'read_system_metrics', side_effect=readings), \
                 patch.object(daemon, 'build_snapshot', return_value=snapshot()), \
                 patch.object(daemon, 'read_disks', return_value=[]), \
                 patch.object(daemon, 'read_host_memory', return_value=(None, 0, 0)):
                for _ in readings:
                    daemon.sample(settings)
                    states.append(daemon.load_state(Path(root) / 'state.json'))
            self.assertEqual(states[3]['psi_alert'], {})
            self.assertEqual(states[-1]['severity'], 'normal')
            self.assertFalse(any('PSI' in reason for reason in states[3]['reasons']))

    def test_restart_gap_does_not_mature_pending_psi(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(state_dir=root, warning_psi_sustain_seconds=60, disk_drives=[])
            readings = [metrics(12, psi_some=9, timestamp=100), metrics(12, psi_some=9, timestamp=1000)]
            for m in readings:
                m.psi_status = 'normal'
            with patch.object(daemon, 'read_system_metrics', side_effect=readings), \
                 patch.object(daemon, 'build_snapshot', return_value=snapshot()), \
                 patch.object(daemon, 'read_disks', return_value=[]), \
                 patch.object(daemon, 'read_host_memory', return_value=(None, 0, 0)):
                first = daemon.sample(settings)[2]
                resumed = daemon.sample(settings)[2]
            self.assertEqual(first.severity, 'normal')
            self.assertEqual(resumed.severity, 'normal')
            self.assertEqual(resumed.condition_since['warning.psi_some'], 1000)

    def test_missing_reports_and_cli_are_explicit(self):
        m = metrics(12, None, None)
        m.psi_status = 'error'
        for report in (build_text_report(m, snapshot(), 'normal', [], [], [], []),
                       build_html_report(m, snapshot(), 'normal', [], [], [], [])):
            self.assertIn('관측 불가', report)
            self.assertIn('수집 오류', report)
        with tempfile.TemporaryDirectory() as root, \
             patch.object(cli, '_load_settings', return_value=Settings(state_dir=root)), \
             patch.object(cli, 'sample', return_value=(m, snapshot(), daemon.Evaluation('normal', [], []))), \
             patch.object(cli, 'Notifier') as notifier, contextlib.redirect_stdout(io.StringIO()) as out:
            notifier.return_value.channel_status.return_value = {}
            cli.cmd_status(SimpleNamespace(config=None))
        psi_line = next(line for line in out.getvalue().splitlines() if line.startswith('PSI'))
        self.assertIn('관측 불가', psi_line)
        self.assertNotIn('0.00%', psi_line)

    def test_history_marks_partial_and_preserves_all_missing(self):
        def row(ts, value, status):
            return {'timestamp': ts, 'metrics': {'psi_some_avg60': value,
                    'psi_full_avg60': value, 'psi_status': status}}
        partial = downsample([row(0, None, 'missing'), row(60, 0, 'normal')], 300)[0]
        self.assertEqual(partial['metrics']['psi_status'], 'partial')
        self.assertEqual(partial['metrics']['psi_some_avg60'], 0)
        absent = downsample([row(0, None, 'missing'), row(60, None, 'error')], 300)[0]
        self.assertIsNone(absent['metrics']['psi_some_avg60'])
        self.assertEqual(absent['metrics']['psi_status'], 'error')
        legacy = downsample([row(0, 0, 'legacy'), row(60, 1, 'normal')], 300)[0]
        self.assertEqual(legacy['metrics']['psi_status'], 'partial')


class WriterLockTests(unittest.TestCase):
    def test_duplicate_rejected_before_writes_and_stale_pid_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'daemon.lock').write_text('99999999\n')
            with daemon.state_writer_lock(path):
                with patch.object(daemon, 'sample') as sample:
                    with self.assertRaisesRegex(RuntimeError, 'already running'):
                        daemon.run_forever(Settings(state_dir=root))
                    sample.assert_not_called()
                self.assertFalse((path / 'state.json').exists())
                self.assertFalse(list(path.glob('history*')))
            with daemon.state_writer_lock(path):
                pass

    def test_reload_keeps_locked_state_directory(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(state_dir=root)
            proposed = Settings(state_dir=root + '-other', interval_seconds=99)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                actual = daemon._reload_settings(settings, proposed)
            self.assertEqual(actual.state_path, settings.state_path)
            self.assertEqual(actual.interval_seconds, 99)
            self.assertIn('restart', output.getvalue())

    def test_once_cannot_write_while_daemon_holds_lock(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(cli, '_load_settings', return_value=Settings(state_dir=root)), \
             patch.object(cli, 'sample') as sampled:
            with daemon.state_writer_lock(Path(root)):
                with self.assertRaises(RuntimeError):
                    cli.cmd_once(SimpleNamespace(notify=False))
                sampled.assert_not_called()

    def test_process_lifetime_lock_releases_on_sigterm(self):
        with tempfile.TemporaryDirectory() as root:
            code = """
import sys
from pathlib import Path
sys.path.insert(0, 'tests')
from test_daemon import metrics, snapshot
from wsl_resource_guard import daemon
from wsl_resource_guard.config import Settings
daemon.sample = lambda *a, **kw: (metrics(12), snapshot(), daemon.Evaluation('normal', [], []))
daemon.run_forever(Settings(state_dir=sys.argv[1], interval_seconds=1, weekly_report_enabled=False))
"""
            process = subprocess.Popen([sys.executable, '-u', '-c', code, root],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertIn('started', process.stdout.readline())
                duplicate = subprocess.run([sys.executable, '-c', code, root],
                                           capture_output=True, text=True, timeout=5)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn('already running', duplicate.stderr)
            finally:
                process.terminate()
                out, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, err)
            self.assertIn('stopped', out)
            with daemon.state_writer_lock(Path(root)):
                pass
