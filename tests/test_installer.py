import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard import service_install


class InstallerSafetyTests(unittest.TestCase):
    def test_owner_requires_explicit_identity_when_root(self):
        resolve = getattr(service_install, 'resolve_owner', None)
        self.assertIsNotNone(resolve, 'installer must resolve the invoking owner explicitly')
        with patch.dict(os.environ, {}, clear=True), patch('os.geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'owner'):
                resolve(None)

    def test_owner_root_is_rejected(self):
        resolve = getattr(service_install, 'resolve_owner', None)
        self.assertIsNotNone(resolve)
        with self.assertRaisesRegex(ValueError, 'non-root'):
            resolve('root')

    def test_configuration_mismatch_has_no_mutations(self):
        preflight = getattr(service_install, 'preflight_identity', None)
        self.assertIsNotNone(preflight)
        with tempfile.TemporaryDirectory() as tmp:
            registry, web = Path(tmp) / 'registry.json', Path(tmp) / 'web.json'
            registry.write_text(json.dumps({'owner': 'alice', 'origin': 'https://host.ts.net:9443'}))
            web.write_text(json.dumps({'origin': 'https://host.ts.net:9443', 'allowed_logins': ['alice@example.com']}))
            before = [(p.read_bytes(), p.stat().st_mode) for p in (registry, web)]
            for owner, origin, login in [('bob', 'https://host.ts.net:9443', 'alice@example.com'),
                                        ('alice', 'https://other.ts.net:9443', 'alice@example.com'),
                                        ('alice', 'https://host.ts.net:9443', 'bob@example.com')]:
                with self.assertRaisesRegex(RuntimeError, 'existing|Existing'):
                    preflight(owner, origin, login, registry, web)
            self.assertEqual(before, [(p.read_bytes(), p.stat().st_mode) for p in (registry, web)])

    def test_root_destination_symlinks_rejected(self):
        check = getattr(service_install, 'validate_root_path', None)
        self.assertIsNotNone(check)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'target').mkdir()
            (root / 'link').symlink_to(root / 'target', target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                check(root / 'link' / 'new', trusted_base=root)

    def test_rollback_restores_content_mode_and_removes_only_new_files(self):
        from wsl_resource_guard import installer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / 'existing'
            existing.write_text('original')
            existing.chmod(0o640)
            unrelated = root / 'unrelated'
            unrelated.write_text('keep')
            tx = installer.FileTransaction(root / 'backup')
            tx.write(existing, b'new', 0o600)
            tx.write(root / 'created', b'created', 0o644)
            tx.rollback()
            self.assertEqual(existing.read_text(), 'original')
            self.assertEqual(existing.stat().st_mode & 0o777, 0o640)
            self.assertFalse((root / 'created').exists())
            self.assertEqual(unrelated.read_text(), 'keep')
            self.assertTrue((root / 'backup').exists())

    def test_venv_validation_uses_final_path_and_web_uid(self):
        stage = getattr(service_install, 'prepare_venv', None)
        self.assertIsNotNone(stage)
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            web = pwd.getpwuid(os.getuid())
            with patch('wsl_resource_guard.service_install.install_environment') as install, \
                 patch('wsl_resource_guard.service_install.run', side_effect=lambda *a, **k: calls.append(a) or ''), \
                 patch('wsl_resource_guard.service_install.run_as', side_effect=lambda user, *a, **k: calls.append(('user', user.pw_uid, *a)) or ''):
                result = stage(dest, dest / 'requirements.txt', web)
            self.assertEqual(result.parent, dest / '.venvs')
            install.assert_called_once_with(result, dest / 'requirements.txt')
            self.assertIn(('user', web.pw_uid, str(result / 'bin/gunicorn'), '--version'), calls)
            self.assertFalse((dest / '.venv').exists())

    def test_lock_prevents_concurrent_install(self):
        from wsl_resource_guard import installer
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'install.lock'
            with installer.install_lock(path):
                with self.assertRaisesRegex(RuntimeError, 'progress'):
                    with installer.install_lock(path):
                        pass


    def test_owner_lock_contends_across_processes_and_releases_on_failure(self):
        import subprocess
        import sys
        from types import SimpleNamespace
        from wsl_resource_guard.installer import owner_install_lock
        with tempfile.TemporaryDirectory() as directory:
            owner = SimpleNamespace(pw_dir=directory, pw_uid=os.getuid(), pw_gid=os.getgid(),
                                    pw_name=pwd.getpwuid(os.getuid()).pw_name)
            script = ("from types import SimpleNamespace; from wsl_resource_guard.installer import owner_install_lock; "
                      "import sys,os,pwd; owner=SimpleNamespace(pw_dir=sys.argv[1],pw_uid=os.getuid(),"
                      "pw_gid=os.getgid(),pw_name=pwd.getpwuid(os.getuid()).pw_name); "
                      "lock=owner_install_lock(owner); lock.__enter__(); lock.__exit__(None,None,None)")
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with owner_install_lock(owner):
                    result = subprocess.run([sys.executable, '-B', '-c', script, directory],
                                            capture_output=True, text=True, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('installation is in progress', result.stderr)
                    raise RuntimeError('fixture')
            result = subprocess.run([sys.executable, '-B', '-c', script, directory],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)


class ServiceRollbackTests(unittest.TestCase):
    def test_restart_failure_restores_files_and_states_without_changing_registry(self):
        self.check_failure(dependency_failure=False)

    def test_dependency_failure_preserves_runtime_without_service_commands(self):
        self.check_failure(dependency_failure=True)

    def check_failure(self, dependency_failure):
        from contextlib import ExitStack
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            (source / 'wsl_resource_guard/web').mkdir(parents=True)
            (source / 'wsl_resource_guard/__init__.py').write_text('# new\n')
            (source / 'wsl_resource_guard/web/index.html').write_text('new web')
            (source / 'packaging').mkdir()
            for name in (*service_install.UNITS, 'opencode-web.service'):
                (source / 'packaging' / name).write_text('ExecStart=@WEB_VENV@/bin/gunicorn\n')
            (source / 'requirements-web.txt').write_text('')
            dest = root / 'opt'
            (dest / 'wsl_resource_guard').mkdir(parents=True)
            (dest / 'wsl_resource_guard/__init__.py').write_text('# old\n')
            units = root / 'units'
            units.mkdir()
            for name in service_install.UNITS:
                (units / name).write_text('old unit\n')
            registry, web = root / 'registry.json', root / 'web.json'
            registry_data = {'owner': 'alice', 'origin': 'https://host.ts.net:9443', 'services': [{'key': 'external', 'autostart': False}]}
            registry.write_text(json.dumps(registry_data))
            web.write_text(json.dumps({'origin': registry_data['origin'], 'allowed_logins': ['alice@example.com'], 'secret_key': 'keep'}))
            owner = SimpleNamespace(pw_name='alice', pw_dir=str(root / 'home'), pw_uid=os.getuid(), pw_gid=os.getgid())
            calls, restored = [], []
            def run(*args, **kwargs):
                calls.append(args)
                if args == ('systemctl', 'restart', 'wrg-web.service'):
                    raise RuntimeError('injected restart failure')
                return ''
            original_state = {'enabled': 'disabled', 'active': False}
            with ExitStack() as stack:
                for key, value in [('SOURCE', source), ('DEST', dest), ('REGISTRY', registry), ('WEB_CONFIG', web), ('UNIT_DIR', units), ('SHARED', root / 'shared'), ('BACKUP_ROOT', root / 'backups')]:
                    stack.enter_context(patch.object(service_install, key, value))
                stack.enter_context(patch.object(service_install, 'validate_root_path'))
                stack.enter_context(patch.object(service_install, 'owner_install_lock'))
                stack.enter_context(patch('wsl_resource_guard.guard_install.validate_system_owner'))
                stack.enter_context(patch.object(service_install, 'tailscale_preflight', return_value=(registry_data['origin'], 'alice@example.com', {}, None)))
                stack.enter_context(patch.object(service_install, 'unit_state', return_value=original_state))
                stack.enter_context(patch.object(service_install, 'prepare_venv', return_value=dest / '.venvs/new',
                                                side_effect=RuntimeError('injected dependency failure') if dependency_failure else None))
                stack.enter_context(patch.object(service_install, 'prepare_shared_dir'))
                stack.enter_context(patch.object(service_install.pwd, 'getpwnam', return_value=owner))
                stack.enter_context(patch.object(service_install, 'run_as'))
                stack.enter_context(patch.object(service_install, 'run', side_effect=run))
                stack.enter_context(patch.object(service_install, 'restore_unit', side_effect=lambda name, state: restored.append((name, state))))
                with self.assertRaisesRegex(RuntimeError, 'injected dependency' if dependency_failure else 'injected restart'):
                    service_install.install(owner)
            self.assertEqual((dest / 'wsl_resource_guard/__init__.py').read_text(), '# old\n')
            self.assertFalse((dest / 'wsl_resource_guard/web/index.html').exists())
            self.assertEqual(json.loads(registry.read_text()), registry_data)
            self.assertTrue(all((units / name).read_text() == 'old unit\n' for name in service_install.UNITS))
            self.assertEqual(restored, [] if dependency_failure else [(name, original_state) for name in service_install.UNITS])
            if dependency_failure:
                self.assertFalse(any(args[0] == 'systemctl' for args in calls))
            self.assertFalse(any(args[:2] == ('tailscale', 'serve') for args in calls))


class UnitRuntimeRecoveryTests(unittest.TestCase):
    def test_runtime_enablement_removes_installers_permanent_enablement(self):
        from wsl_resource_guard import installer
        # Model the two independent systemd link stores at the command boundary.
        links = {'persistent': True, 'runtime': True}
        active = []
        def systemctl(*args, **kwargs):
            verb = args[1]
            if verb == 'disable':
                links['persistent'] = False
                links['runtime'] = False
            elif verb == 'enable':
                links['runtime' if '--runtime' in args else 'persistent'] = True
            elif verb in ('restart', 'stop'):
                active.append(verb)
        with patch.object(installer, 'run', side_effect=systemctl):
            installer.restore_unit('guard.service', {'enabled': 'enabled-runtime', 'active': True})
        self.assertEqual(links, {'persistent': False, 'runtime': True})
        self.assertEqual(active, ['restart'])


if __name__ == '__main__':
    unittest.main()
