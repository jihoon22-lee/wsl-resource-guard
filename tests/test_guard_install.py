import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


class GuardInstallTests(unittest.TestCase):
    def test_generic_unit_renders_owner_and_escapes_systemd_specifiers(self):
        from wsl_resource_guard.guard_install import render_unit
        owner = SimpleNamespace(pw_name='alice', pw_gid=os.getgid(), pw_dir='/home/alice%work')
        root = Path(__file__).resolve().parents[1]
        unit = render_unit(root / 'packaging/wsl-resource-guard.service', owner)
        self.assertIn('User=alice', unit)
        self.assertIn('WorkingDirectory=/home/alice%%work\n', unit)
        self.assertIn('/home/alice%%work/.local/bin/wrg', unit)
        self.assertNotIn('@HOME@', unit)
        self.assertNotIn('@OWNER@', unit)

    def test_user_install_refuses_any_existing_system_guard(self):
        from wsl_resource_guard.guard_install import ensure_no_system_guard
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / 'guard.service'
            unit.write_text('[Service]\nUser=someone\n')
            with self.assertRaisesRegex(RuntimeError, 'system guard'):
                ensure_no_system_guard(unit)

    def test_install_conflict_is_checked_after_owner_lock_acquisition(self):
        from contextlib import contextmanager
        from wsl_resource_guard import guard_install
        events = []
        @contextmanager
        def lock(owner):
            events.append('locked')
            yield
        def conflict():
            self.assertEqual(events, ['locked'])
            raise RuntimeError('system guard appeared while acquiring lock')
        with patch.object(guard_install, 'owner_install_lock', side_effect=lock), \
             patch.object(guard_install, 'ensure_no_system_guard', side_effect=conflict), \
             patch.object(guard_install, '_install_preflighted') as apply:
            with self.assertRaisesRegex(RuntimeError, 'appeared'):
                guard_install.install(object(), False)
            apply.assert_not_called()

    def test_system_owner_is_revalidated_under_owner_lock(self):
        from contextlib import contextmanager
        from wsl_resource_guard import guard_install
        events = []
        @contextmanager
        def lock(owner):
            events.append('locked')
            yield
        def conflict(owner):
            self.assertEqual(events, ['locked'])
            raise RuntimeError('owner changed')
        with patch.object(guard_install, 'owner_install_lock', side_effect=lock), \
             patch.object(guard_install, 'validate_root_path'), \
             patch.object(guard_install, 'validate_system_owner', side_effect=conflict), \
             patch.object(guard_install, '_install_preflighted') as apply:
            with self.assertRaisesRegex(RuntimeError, 'owner changed'):
                guard_install.install(object(), True)
            apply.assert_not_called()

    def test_owner_home_symlink_cannot_write_root_target(self):
        from wsl_resource_guard.installer import FileTransaction
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'target'
            target.write_text('must stay')
            linked = root / 'linked'
            linked.symlink_to(target)
            tx = FileTransaction(root / 'backup')
            tx.write(linked, b'new')
            self.assertEqual(target.read_text(), 'must stay')
            tx.rollback()
            self.assertTrue(linked.is_symlink())
            self.assertEqual(target.read_text(), 'must stay')


class GuardRollbackTests(unittest.TestCase):
    def test_system_start_failure_restores_previous_user_install(self):
        from contextlib import ExitStack
        import pwd
        from wsl_resource_guard import guard_install
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'home'
            package = home / '.local/lib/wsl-resource-guard/wsl_resource_guard'
            package.mkdir(parents=True)
            (package / 'daemon.py').write_text('# old daemon\n')
            owner = SimpleNamespace(pw_name=pwd.getpwuid(os.getuid()).pw_name, pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(home))
            system_unit = root / 'units/guard.service'
            old_user = {'enabled': 'enabled', 'active': True}
            old_system = {'enabled': 'not-found', 'active': False}
            restored, commands = [], []
            def system_run(*args, **kwargs):
                commands.append(args)
                if args == ('systemctl', 'restart', guard_install.UNIT):
                    raise RuntimeError('injected startup failure')
                return ''
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {'HOME': str(home)}))
                stack.enter_context(patch.object(guard_install, 'SYSTEM_UNIT', system_unit))
                stack.enter_context(patch.object(guard_install, 'BACKUP_ROOT', root / 'backups', create=True))
                stack.enter_context(patch.object(guard_install, 'validate_system_owner'))
                stack.enter_context(patch.object(guard_install, 'validate_root_path'))
                stack.enter_context(patch.object(guard_install, 'owner_install_lock'))
                stack.enter_context(patch.object(guard_install, 'run', side_effect=system_run))
                stack.enter_context(patch.object(guard_install, 'run_as'))
                stack.enter_context(patch.object(guard_install, 'restore_unit', side_effect=lambda name, state, owner: restored.append((state, owner))))
                stack.enter_context(patch.object(guard_install, 'unit_state', side_effect=lambda name, owner=None: old_user if owner else old_system))
                with self.assertRaisesRegex(RuntimeError, 'injected startup'):
                    guard_install.install(owner, True)
            self.assertEqual((package / 'daemon.py').read_text(), '# old daemon\n')
            self.assertFalse((package / 'cli.py').exists())
            self.assertFalse(system_unit.exists())
            self.assertIn((old_user, owner), restored)
            self.assertIn((old_system, None), restored)
            self.assertTrue(list((home / '.local/state/wsl-resource-guard/install-backups').glob('*/state-path.txt')))


class GuardStateRecoveryTests(unittest.TestCase):
    def test_failed_guard_outputs_are_preserved_then_removed_or_restored(self):
        import json
        from wsl_resource_guard.guard_install import snapshot_state, restore_state
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, backup = root / 'state', root / 'backup'
            state.mkdir()
            backup.mkdir()
            (state / 'weekly-report.json').write_text('old weekly')
            (state / 'weekly-report.json').chmod(0o640)
            (state / 'history-2026-10-07.jsonl').write_text('old history\n')
            with patch('wsl_resource_guard.config.load_daemon_settings', return_value=SimpleNamespace(state_path=state)):
                snapshot_state(backup)
            manifest = backup / 'state-manifest.json'
            self.assertTrue(manifest.exists(), 'recovery must record absent as well as present daemon outputs')
            saved = json.loads(manifest.read_text())['files']
            self.assertFalse(saved['state.json']['present'])
            self.assertFalse(saved['config-request-result.json']['present'])
            self.assertFalse(saved['push-expired.json']['present'])
            (state / 'state.json').write_text('new state')
            (state / 'config-request-result.json').write_text('new result')
            (state / 'push-expired.json').write_text('new push expiry')
            (state / 'weekly-report.json').write_text('new weekly')
            (state / 'weekly-report.json').chmod(0o600)
            (state / 'history-2026-10-07.jsonl').write_text('old history\nnew history\n')
            (state / 'history-2026-10-08.jsonl').write_text('next day\n')
            (state / 'disk-history-2026-10-08.jsonl').write_text('next day disk\n')
            (state / 'unrelated.jsonl').write_text('keep unrelated\n')
            restore_state(backup)
            for name in ('state.json', 'config-request-result.json', 'push-expired.json',
                         'history-2026-10-08.jsonl', 'disk-history-2026-10-08.jsonl'):
                self.assertFalse((state / name).exists(), name)
            self.assertEqual((state / 'weekly-report.json').read_text(), 'old weekly')
            self.assertEqual((state / 'weekly-report.json').stat().st_mode & 0o777, 0o640)
            self.assertEqual((state / 'history-2026-10-07.jsonl').read_text(), 'old history\n')
            self.assertEqual((state / 'unrelated.jsonl').read_text(), 'keep unrelated\n')
            recovered = next(backup.glob('state-restore-*'))
            entries = {Path(e['path']).name: e for e in json.loads((recovered / 'manifest.json').read_text())}
            for name, content in [('state.json', 'new state'), ('history-2026-10-08.jsonl', 'next day\n'),
                                  ('history-2026-10-07.jsonl', 'old history\nnew history\n')]:
                self.assertEqual((recovered / entries[name]['backup']).read_text(), content)

    def test_roll_back_to_initially_missing_state_directory(self):
        from wsl_resource_guard.guard_install import snapshot_state, restore_state
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, backup = root / 'state', root / 'backup'
            backup.mkdir()
            with patch('wsl_resource_guard.config.load_daemon_settings', return_value=SimpleNamespace(state_path=state)):
                snapshot_state(backup)
            state.mkdir()
            (state / 'state.json').write_text('new')
            (state / 'history-2026-10-08.jsonl').write_text('new history')
            restore_state(backup)
            self.assertEqual(list(state.iterdir()), [])
