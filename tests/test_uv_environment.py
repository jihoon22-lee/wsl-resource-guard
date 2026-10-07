import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard import service_install


class UvEnvironmentTests(unittest.TestCase):
    def module(self):
        from wsl_resource_guard import uv_environment
        return uv_environment

    def archive(self, name='uv-x86_64-unknown-linux-gnu/uv', kind=tarfile.REGTYPE, content=b'fixture executable'):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(content) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(content) if info.size else None)
        data = buffer.getvalue()
        return data, hashlib.sha256(data).hexdigest()

    def test_archive_integrity_and_exact_regular_member(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'uv'
            data, digest = self.archive()
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                module.extract_uv(data, target, 'uv-x86_64-unknown-linux-gnu', '0' * 64)
            self.assertFalse(target.exists())
            for name, kind in [('../uv', tarfile.REGTYPE), ('uv-x86_64-unknown-linux-gnu/uv', tarfile.SYMTYPE)]:
                data, digest = self.archive(name, kind)
                with self.assertRaises(RuntimeError):
                    module.extract_uv(data, target, 'uv-x86_64-unknown-linux-gnu', digest)
                self.assertFalse(target.exists())
            data, digest = self.archive()
            module.extract_uv(data, target, 'uv-x86_64-unknown-linux-gnu', digest)
            self.assertEqual(target.read_bytes(), b'fixture executable')
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                module.extract_uv(data, target, 'uv-x86_64-unknown-linux-gnu', digest)

    def test_uv_does_not_inherit_user_configuration_or_execution_environment(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            # A fake binary reports the actual child environment/cwd/arguments.
            executable = workspace / 'uv'
            executable.write_text('#!/usr/bin/python3\nimport json,os,sys\nprint(json.dumps({"env":dict(os.environ),"cwd":os.getcwd(),"args":sys.argv[1:]}))\n')
            executable.chmod(0o700)
            with patch.dict(os.environ, {'HOME':'/home/fixture', 'UV_INDEX_URL':'https://evil.example',
                    'PIP_INDEX_URL':'https://evil.example', 'PYTHONPATH':'/untrusted', 'LD_PRELOAD':'/untrusted',
                    'HTTPS_PROXY':'https://evil.example'}):
                import json
                result = json.loads(module.run_uv(executable, workspace, '--version'))
            for variable in ('UV_INDEX_URL', 'PIP_INDEX_URL', 'PYTHONPATH', 'LD_PRELOAD', 'HTTPS_PROXY'):
                self.assertNotIn(variable, result['env'])
            self.assertEqual(result['env']['HOME'], directory)
            self.assertEqual(result['cwd'], directory)
            self.assertIn('--no-python-downloads', result['args'])
            self.assertIn('--no-managed-python', result['args'])
            self.assertEqual(Path(result['args'][result['args'].index('--config-file') + 1]).read_text(), '')

    def test_dependency_failure_never_overwrites_existing_environment(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / 'old'
            old.mkdir()
            (old / 'marker').write_text('keep')
            with self.assertRaises(FileExistsError):
                module.install_environment(old, root / 'requirements.txt')
            self.assertEqual((old / 'marker').read_text(), 'keep')
            (root / 'requirements.txt').write_text('')
            new = root / 'new'
            calls = []
            def failing_uv(uv, workspace, *args):
                calls.append(args)
                if args[:2] == ('pip', 'sync'):
                    raise subprocess.CalledProcessError(1, 'uv')
                return f'uv {module.UV_VERSION} (fixture)' if args == ('--version',) else ''
            with patch.object(module, 'download_uv', return_value=root / 'verified-uv'), \
                    patch.object(module, 'run_uv', side_effect=failing_uv), \
                    patch.object(module, 'validate_root_path'):
                with self.assertRaises(subprocess.CalledProcessError):
                    module.install_environment(new, root / 'requirements.txt')
            self.assertEqual((old / 'marker').read_text(), 'keep')
            self.assertFalse(any(args[:2] == ('pip', 'check') for args in calls))
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['old', 'requirements.txt'])

    def test_prepare_venv_uses_verified_uv_then_web_uid_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            from types import SimpleNamespace
            web = SimpleNamespace(pw_uid=os.getuid())
            with patch.object(service_install, 'install_environment', create=True) as install, \
                    patch.object(service_install, 'run_as') as run_as:
                environment = service_install.prepare_venv(destination, destination / 'requirements.txt', web)
            install.assert_called_once_with(environment, destination / 'requirements.txt')
            self.assertEqual(run_as.call_count, 2)
