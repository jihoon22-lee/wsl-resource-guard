"""Installer state paths must match the daemon's writable and recovery paths."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wsl_resource_guard import guard_install


class GuardPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'nonstandard home'
        self.home.mkdir()
        self.config = self.home / '.config/wsl-resource-guard/config.toml'
        self.config.parent.mkdir(parents=True)
        self.owner = SimpleNamespace(pw_name='alice', pw_gid=os.getgid(), pw_dir=str(self.home))

    def configure(self, path):
        self.config.write_text('state_dir = ' + json.dumps(str(path)) + '\n')

    def test_default_and_custom_state_are_resolved_from_owner_home(self):
        context = guard_install.guard_context(self.home, True)
        self.assertEqual(context['state_path'], str(self.home / '.local/state/wsl-resource-guard'))
        self.configure('~/guard data')
        context = guard_install.guard_context(self.home, True)
        self.assertEqual(context['state_path'], str(self.home / 'guard data'))
        self.assertFalse((self.home / 'guard data').exists(), 'read preflight must not create paths')
        self.assertIsNotNone(context['config_sha256'])
        self.config.write_text(self.config.read_text() + '# edited\n')
        self.assertNotEqual(context, guard_install.guard_context(self.home, True))

    def test_system_paths_outside_home_or_through_links_are_rejected(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (self.home / 'linked').symlink_to(outside, target_is_directory=True)
        (self.home / 'file').write_text('fixture')
        for path in ('/etc/guard', '../outside', 'relative', self.home, self.home / 'linked/state',
                     self.home / 'file/state', str(self.home / 'bad\npath')):
            with self.subTest(path=path):
                self.configure(path)
                with self.assertRaises((ValueError, RuntimeError, OSError)):
                    guard_install.guard_context(self.home, True)
        self.configure(outside)
        self.assertEqual(guard_install.guard_context(self.home, False)['state_path'], str(outside))
        self.configure('relative')
        self.assertEqual(guard_install.guard_context(self.home, False)['state_path'], str(self.home / 'relative'))

    def test_unit_paths_follow_directive_parsers_and_specifiers_are_literal(self):
        root = Path(__file__).resolve().parents[1]
        state = self.home / 'guard "data" %n $HOME'
        unit = guard_install.render_unit(root / 'packaging/wsl-resource-guard.service', self.owner, state)
        self.assertIn('WorkingDirectory=' + str(self.home) + '\n', unit)
        self.assertIn('ReadWritePaths="' + str(self.home) + '/guard \\"data\\" %%n $HOME"', unit)
        self.assertIn('WRG_SYSTEM_STATE_DIR=', unit)
        for home in ('/home/fixture\nExecStart=/bin/false', '/home/fixture\r', '/home/fixture\x00', '/home/fixture\t', '/home/fixture\\y', '/home/fixture\"y', '/home/fixture '):
            self.owner.pw_dir = home
            with self.assertRaises(ValueError):
                guard_install.render_unit(root / 'packaging/wsl-resource-guard.service', self.owner)

    def test_missing_or_malformed_config_is_not_silently_replaced(self):
        self.config.write_text('invalid = [')
        with self.assertRaises(ValueError):
            guard_install.guard_context(self.home, True)
        self.configure(123)
        # A string of digits is a relative path and cannot be system writable state.
        with self.assertRaises(ValueError):
            guard_install.guard_context(self.home, True)
        self.config.write_text('state_dir = 123\n')
        with self.assertRaises(ValueError):
            guard_install.guard_context(self.home, True)


class GuardReadyTests(unittest.TestCase):
    def test_real_saved_sample_identifies_its_writer_for_readiness(self):
        import pwd
        import time
        from contextlib import ExitStack
        from wsl_resource_guard import daemon
        from wsl_resource_guard.config import Settings
        from test_daemon import metrics, snapshot
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            state = Path(directory)
            owner = pwd.getpwuid(os.getuid())
            before = time.time()
            stack.enter_context(patch.object(daemon, 'read_system_metrics', return_value=metrics(12, timestamp=time.time())))
            stack.enter_context(patch.object(daemon, 'build_snapshot', return_value=snapshot()))
            stack.enter_context(patch.object(daemon, 'read_disks', return_value=[]))
            stack.enter_context(patch.object(daemon, 'read_host_memory', return_value=(None, 0, 0)))
            daemon.sample(Settings(state_dir=directory, disk_drives=[]), notify=False)
            with patch.object(guard_install, 'run', return_value=str(os.getpid())):
                guard_install.wait_for_guard(owner, True, state, before, timeout=2)

    def test_active_unit_without_new_sample_is_not_ready(self):
        owner = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir='/fixture', pw_name='fixture')
        with patch.object(guard_install, 'run', return_value=str(os.getpid())), \
             patch.object(guard_install, 'owner_query', return_value={}), \
             self.assertRaisesRegex(RuntimeError, 'first sample'):
            guard_install.wait_for_guard(owner, True, Path('/fixture/state'), 1, timeout=0)

    def test_new_sample_must_match_pid_start_token_and_start_time(self):
        from wsl_resource_guard.processes import process_start_ticks
        owner = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir='/fixture', pw_name='fixture')
        valid = dict(writer_pid=os.getpid(), writer_start_ticks=process_start_ticks(os.getpid()),
                     updated_at=20, mtime=21)
        for override in ({'writer_pid': 1}, {'writer_start_ticks': -1}, {'updated_at': 9},
                         {'mtime': 9}, {'updated_at': float('nan')}, {}):
            with self.subTest(override=override), \
                 patch.object(guard_install, 'run', return_value=str(os.getpid())), \
                 patch.object(guard_install, 'owner_query', return_value=dict(valid, **override)):
                if override:
                    with self.assertRaisesRegex(RuntimeError, 'first sample'):
                        guard_install.wait_for_guard(owner, True, Path('/fixture/state'), 10, timeout=0)
                else:
                    guard_install.wait_for_guard(owner, True, Path('/fixture/state'), 10, timeout=0)


class GuardApplyPathTests(unittest.TestCase):
    def exercise_install(self, *, change_config=False, readiness_failure=False):
        from contextlib import ExitStack
        import pwd
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            owner = SimpleNamespace(pw_name=pwd.getpwuid(os.getuid()).pw_name,
                                    pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(home))
            state = home / 'guard data'
            state.mkdir()
            (state / 'state.json').write_text('{"old":true}')
            config = home / '.config/wsl-resource-guard/config.toml'
            config.parent.mkdir(parents=True)
            config.write_text('state_dir = ' + json.dumps(str(state)) + '\n')
            unit = root / 'units/guard.service'
            unit.parent.mkdir()
            unit.write_text('old unit\n')
            states = {'enabled': 'enabled', 'active': True}
            commands, restored = [], []
            original_prepare = guard_install.prepare_state_path
            def prepare(path):
                original_prepare(path)
                if change_config:
                    config.write_text(config.read_text() + '# concurrent edit\n')
            def ready(account, system, path, started_at):
                self.assertEqual(path, state)
                self.assertIn('ReadWritePaths="' + str(state) + '"', unit.read_text())
                (state / 'state.json').write_text('{"new":true}')
                (state / 'history-2026-10-08.jsonl').write_text('new sample\n')
                if readiness_failure:
                    raise RuntimeError('first sample fixture failed')
            with ExitStack() as stack:
                for key, value in (('SYSTEM_UNIT', unit), ('BACKUP_ROOT', root / 'backups')):
                    stack.enter_context(patch.object(guard_install, key, value))
                stack.enter_context(patch.dict(os.environ, {'HOME': str(home)}))
                stack.enter_context(patch.object(guard_install, 'owner_install_lock'))
                stack.enter_context(patch.object(guard_install, 'validate_system_owner'))
                stack.enter_context(patch.object(guard_install, 'validate_root_path'))
                stack.enter_context(patch.object(guard_install, 'unit_state', return_value=states))
                stack.enter_context(patch.object(guard_install, 'prepare_state_path', side_effect=prepare))
                stack.enter_context(patch.object(guard_install, 'run', side_effect=lambda *a, **k: commands.append(a) or ''))
                stack.enter_context(patch.object(guard_install, 'run_as', side_effect=lambda u, *a, **k: commands.append(a) or ''))
                stack.enter_context(patch.object(guard_install, 'restore_unit', side_effect=lambda *a: restored.append(a)))
                stack.enter_context(patch.object(guard_install, 'wait_for_guard', side_effect=ready))
                if change_config or readiness_failure:
                    with self.assertRaisesRegex(RuntimeError, 'Configuration changed' if change_config else 'first sample'):
                        guard_install.install(owner, True)
                else:
                    guard_install.install(owner, True)
            backups = list((home / '.local/state/wsl-resource-guard/install-backups').iterdir())
            self.assertEqual(len(backups), 1)
            if change_config:
                self.assertEqual(commands, [])
                self.assertEqual(restored, [])
                self.assertEqual(unit.read_text(), 'old unit\n')
                self.assertIn('# concurrent edit', config.read_text())
            else:
                self.assertEqual((backups[0] / 'state-path.txt').read_text(), str(state))
                if readiness_failure:
                    self.assertEqual((state / 'state.json').read_text(), '{"old":true}')
                    self.assertFalse((state / 'history-2026-10-08.jsonl').exists())
                    self.assertEqual(unit.read_text(), 'old unit\n')
                    self.assertEqual(len(restored), 2)
                    recovery = next(backups[0].glob('state-restore-*'))
                    contents = [p.read_text() for p in recovery.iterdir() if p.is_file()]
                    self.assertIn('new sample\n', contents)

    def test_changed_config_aborts_before_service_transition(self):
        self.exercise_install(change_config=True)

    def test_first_collection_failure_restores_custom_state_and_preserves_failed_records(self):
        self.exercise_install(readiness_failure=True)

    def test_custom_state_is_used_for_unit_snapshot_and_readiness(self):
        self.exercise_install()

    def test_system_guard_keeps_installed_path_until_reinstall_even_after_restart(self):
        from wsl_resource_guard import daemon
        from wsl_resource_guard.config import Settings
        with patch.dict(os.environ, {'WRG_SYSTEM_STATE_DIR': '/fixture/installed'}), \
             patch.object(daemon, 'state_writer_lock') as lock, \
             patch.object(daemon, '_run_locked') as run:
            daemon.run_forever(Settings(state_dir='/fixture/changed'))
            lock.assert_called_once_with(Path('/fixture/installed'))
            self.assertEqual(run.call_args.args[0].state_path, Path('/fixture/installed'))
