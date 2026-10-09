"""The controller must never open owner-selected files as root."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard import owner_worker
from wsl_resource_guard.safe_read import read_text


class SafeReadTests(unittest.TestCase):
    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fifo'
            os.mkfifo(path)
            with self.assertRaises(OSError):
                read_text(path)

    def test_oversize_and_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'large'
            path.write_text('x' * 1025)
            for target in (path, Path(directory)):
                with self.assertRaises(OSError):
                    read_text(target, max_bytes=1024)

    def test_weekly_report_has_only_public_schema(self):
        self.assertEqual(owner_worker.weekly_status({'slot': '2026-W40', 'sent_at': 12,
            'results': {'gmail': 'sent', 'password': 'private'},
            'password': 'private'}), {'slot': '2026-W40', 'sent_at': 12,
            'results': {'gmail': 'sent'}})
        self.assertEqual(owner_worker.weekly_status(['wrong']), {})

    def test_unknown_operation_and_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            owner_worker.dispatch('read', {'path': '/etc/shadow'})
        with self.assertRaises(ValueError):
            owner_worker.dispatch('logs', {'target': '/etc/shadow', 'lines': 2})


class OwnerDataTests(unittest.TestCase):
    def test_log_tail_supports_controller_limits_and_rejects_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / '.local/state/opencode-web.log'
            path.parent.mkdir(parents=True)
            path.write_text(''.join(f'line-{i}\n' for i in range(2100)))
            reader = owner_worker.OwnerData(home)
            for count in (500, 501, 1000, 2000):
                result = owner_worker.dispatch('logs', {'target': 'opencode-web.service', 'lines': count}, reader)
                self.assertEqual(result.splitlines()[1:], [f'line-{i}' for i in range(2100-count, 2100)])
            for count in (0, 2001, True, 1.5, '1000'):
                with self.subTest(count=count), self.assertRaises(ValueError):
                    owner_worker.dispatch('logs', {'target': 'opencode-web.service', 'lines': count}, reader)

    def test_auxiliary_log_failures_are_visible_and_do_not_expose_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = owner_worker.OwnerData(Path(directory))
            args = {'target': 'opencode-web.service', 'lines': 1000}
            for failure in (FileNotFoundError('/private/marker'), PermissionError('/private/marker'),
                            TimeoutError('/private/marker')):
                with patch.object(owner_worker, 'read_text', side_effect=failure):
                    result = owner_worker.dispatch('logs', args, reader)
                self.assertIn('보조 로그 읽기 실패', result)
                self.assertNotIn('/private/marker', result)
            path = reader.home / '.local/state/opencode-web.log'
            path.parent.mkdir(parents=True)
            os.mkfifo(path)
            self.assertIn('보조 로그 읽기 실패', owner_worker.dispatch('logs', args, reader))
            path.unlink()
            path.write_text('가'*100000)
            result = owner_worker.dispatch('logs', args, reader)
            self.assertLessEqual(len(result.encode()), 64100)

    def test_permission_denied_secret_is_handled_without_exists_probe(self):
        from wsl_resource_guard.config import Settings
        settings = Settings(secrets_path=Path('/fixture/denied-secret.json'))
        # Python 3.11-3.13 Path.exists raises for permission denied, unlike 3.14.
        with patch.object(Path, 'exists', side_effect=PermissionError('denied')), \
             patch('wsl_resource_guard.config.read_text', side_effect=PermissionError('denied')):
            self.assertEqual(settings.load_secrets(), {})

    def test_malformed_config_and_secret_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / '.config/wsl-resource-guard'
            config.mkdir(parents=True)
            (config / 'config.toml').write_text('invalid = [')
            (config / 'secrets.json').write_text('["invalid"]')
            result = owner_worker.OwnerData(home).context()
            self.assertTrue(result['settings']['load_warnings'])
            self.assertEqual(result['channels']['gmail'], 'not configured')
            self.assertEqual(result['settings']['project_roots'], [str(home / 'projects')])

    def test_config_fifo_and_large_secret_do_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / '.config/wsl-resource-guard'
            config.mkdir(parents=True)
            os.mkfifo(config / 'config.toml')
            (config / 'secrets.json').write_text('x' * (1024 * 1024 + 1))
            result = owner_worker.OwnerData(home).context()
            self.assertTrue(result['settings']['load_warnings'])
            self.assertEqual(result['channels']['gmail'], 'not configured')

    def test_worker_trusted_path_rejects_owner_writable_code(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PermissionError):
                owner_worker._trusted(Path(directory))


@unittest.skipUnless(os.geteuid() == 0, 'Requires isolated root credential test')
class RootIsolationTests(unittest.TestCase):
    """Run as root only; no accounts, service state, or installed files change."""
    def test_installer_queries_drop_supplementary_groups_and_cannot_read_root_files(self):
        import pwd
        from types import SimpleNamespace
        from wsl_resource_guard.installer import owner_query
        account = pwd.getpwnam('nobody')
        with tempfile.TemporaryDirectory(prefix='wrg-owner-query-', dir='/run') as directory:
            root = Path(directory)
            root.chmod(0o755)
            private = root / 'private.json'
            private.write_text('root-only fixture')
            private.chmod(0o600)
            home = root / 'home'
            home.mkdir()
            os.chown(home, account.pw_uid, account.pw_gid)
            (home / 'linked.json').symlink_to(private)
            owner = SimpleNamespace(pw_uid=account.pw_uid, pw_gid=account.pw_gid,
                                    pw_name=account.pw_name, pw_dir=str(home))
            def inspect():
                try:
                    (home / 'linked.json').read_text()
                    readable = True
                except PermissionError:
                    readable = False
                return {'uid': os.geteuid(), 'gid': os.getegid(), 'groups': os.getgroups(),
                        'home': os.environ['HOME'], 'readable': readable}
            result = owner_query(owner, inspect)
            self.assertEqual(result, dict(uid=account.pw_uid, gid=account.pw_gid, groups=[],
                                          home=str(home), readable=False))
            self.assertEqual(private.read_text(), 'root-only fixture')

    def test_actual_dropped_worker_cannot_follow_root_only_symlinks(self):
        import importlib
        import pwd
        import shutil
        import sys
        from types import SimpleNamespace
        # /run is root-owned; /tmp is intentionally refused as a code ancestor.
        account = pwd.getpwnam('nobody')
        with tempfile.TemporaryDirectory(prefix='wrg-owner-test-', dir='/run') as directory:
            root = Path(directory)
            root.chmod(0o755)
            installed = root / 'installed'
            installed.mkdir()
            package = installed / 'wsl_resource_guard'
            shutil.copytree(Path(owner_worker.__file__).parent, package,
                            ignore=shutil.ignore_patterns('__pycache__'))
            home = root / 'home'
            home.mkdir()
            home.chmod(0o755)
            config = home / '.config/wsl-resource-guard'
            state = home / '.local/state/wsl-resource-guard'
            cli = home / '.local/lib/wsl-resource-guard/wsl_resource_guard'
            for path in (config, state, cli):
                path.mkdir(parents=True, exist_ok=True)
            protected = root / 'root-only'
            protected.mkdir(mode=0o700)
            fixture_file = protected / 'fixture_file.json'
            marker = 'ROOT_ONLY_MARKER_7f120b'
            fixture_file.write_text(json.dumps({'slot': marker, 'commit': marker, 'metrics': {'marker': marker}}))
            fixture_file.chmod(0o600)
            for path in (state / 'state.json', state / 'weekly-report.json',
                         state / 'config-request-result.json', state / 'push-expired.json', cli / 'BUILD.json',
                         config / 'secrets.json', home / '.local/state/opencode-web.log'):
                path.symlink_to(fixture_file)
            (config / 'config.toml').write_text('wsl_vhd_path = ' + json.dumps(str(fixture_file)) + '\n')
            from datetime import datetime
            day = datetime.now().strftime('%Y-%m-%d')
            for filename in (f'history-{day}.jsonl', f'disk-history-{day}.jsonl'):
                (state / filename).symlink_to(fixture_file)
            owner = SimpleNamespace(pw_uid=account.pw_uid, pw_gid=account.pw_gid,
                                    pw_name=account.pw_name, pw_dir=str(home))
            # Import the copied trusted module without changing package imports.
            spec = importlib.util.spec_from_file_location('wsl_resource_guard.isolated_owner_worker',
                                                          package / 'owner_worker.py')
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
            for operation, args in (('context', {}), ('settings', {}), ('config-result', {}),
                                    ('history', {}), ('attribution', {}), ('session-history', {}),
                                    ('alerts', {}), ('gone-endpoints', {}),
                                    ('logs', {'target': 'opencode-web.service', 'lines': 10}),
                                    ('disks', {})):
                with self.subTest(operation=operation):
                    if operation in ('config-result', 'history', 'attribution', 'session-history', 'alerts'):
                        # Unreadable history and configuration results must
                        # fail explicitly rather than appear empty or pending.
                        with self.assertRaises(OSError):
                            worker.call_owner(owner, operation, args)
                        continue
                    result = worker.call_owner(owner, operation, args)
                    self.assertNotIn(marker, json.dumps(result))
            (config / 'config.toml').unlink()
            (config / 'config.toml').symlink_to(fixture_file)
            self.assertNotIn(marker, json.dumps(worker.call_owner(owner, 'context')))
            # The root-only target was readable by the test parent, making this
            # a real privilege-boundary assertion rather than a missing fixture.
            self.assertIn(marker, fixture_file.read_text())


class WorkerLaunchTests(unittest.TestCase):
    def test_root_launcher_uses_clean_environment_and_dropped_credentials(self):
        from types import SimpleNamespace
        owner = SimpleNamespace(pw_uid=45678, pw_gid=45679, pw_name='owner', pw_dir='/home/owner')
        with (patch.object(owner_worker.os, 'geteuid', return_value=0),
              patch.object(owner_worker, '_trusted'),
              patch.object(owner_worker.subprocess, 'Popen', side_effect=RuntimeError('observed')) as launch):
            with self.assertRaisesRegex(RuntimeError, 'observed'):
                owner_worker.call_owner(owner, 'context')
        args, options = launch.call_args
        self.assertEqual(args[0][1], '-I')
        self.assertEqual((options['user'], options['group'], options['extra_groups']), (45678, 45679, ()))
        self.assertFalse(options['shell'])
        self.assertEqual(options['cwd'], '/')
        self.assertTrue(options['close_fds'])
        self.assertEqual(set(options['env']), {'HOME', 'USER', 'LOGNAME', 'PATH', 'LANG'})
        self.assertEqual(options['env']['HOME'], '/home/owner')

    @unittest.skipIf(os.geteuid() == 0, 'Checkout is deliberately untrusted by root')
    def test_real_worker_uses_only_supplied_clean_owner_home(self):
        from types import SimpleNamespace
        import pwd
        account = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as directory:
            owner = SimpleNamespace(pw_uid=account.pw_uid, pw_gid=account.pw_gid,
                                    pw_name=account.pw_name, pw_dir=directory)
            with patch.dict(os.environ, {'WRG_CONFIG': '/root/untrusted.toml', 'PYTHONPATH': '/tmp/untrusted'}):
                result = owner_worker.call_owner(owner, 'context')
            self.assertEqual(result['settings']['config_path'], directory + '/.config/wsl-resource-guard/config.toml')
            self.assertEqual(result['settings']['project_roots'], [directory + '/projects'])
            self.assertEqual(result['state'], {})

    def test_real_worker_timeout_and_output_limit_are_explicit_failures(self):
        from types import SimpleNamespace
        import pwd
        account = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as directory:
            owner = SimpleNamespace(pw_uid=account.pw_uid, pw_gid=account.pw_gid,
                                    pw_name=account.pw_name, pw_dir=directory)
            with patch.object(owner_worker, 'TIMEOUT', 0), self.assertRaises(TimeoutError):
                owner_worker.call_owner(owner, 'context')
            with patch.object(owner_worker, 'MAX_OUTPUT', 8), self.assertRaises(ValueError):
                owner_worker.call_owner(owner, 'context')
