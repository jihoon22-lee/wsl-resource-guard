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

    def test_download_never_opens_inherited_tls_key_log(self):
        module = self.module()
        data, digest = self.archive()
        from unittest.mock import Mock
        opener = Mock()
        opener.open.return_value = io.BytesIO(data)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            keylog = workspace / 'must-not-exist.log'
            with patch.dict(os.environ, {'SSLKEYLOGFILE': str(keylog)}), \
                    patch.object(module.platform, 'system', return_value='Linux'), \
                    patch.object(module.platform, 'machine', return_value='x86_64'), \
                    patch.dict(module.UV_ARCHIVES, {'x86_64': ('x86_64-unknown-linux-gnu', digest)}), \
                    patch.object(module.urllib.request, 'build_opener', return_value=opener):
                executable = module.download_uv(workspace)
            self.assertEqual(executable.read_bytes(), b'fixture executable')
            self.assertFalse(keylog.exists(), 'root download must never open an inherited TLS key log path')

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
                    'HTTPS_PROXY':'https://evil.example', 'UV_PROJECT_ENVIRONMENT':'/untrusted/env',
                    'SSL_CERT_FILE':'/untrusted/ca', 'SSLKEYLOGFILE':'/untrusted/keys'}):
                import json
                result = json.loads(module.run_uv(executable, workspace, '--version'))
            for variable in ('UV_INDEX_URL', 'PIP_INDEX_URL', 'PYTHONPATH', 'LD_PRELOAD', 'HTTPS_PROXY',
                             'UV_PROJECT_ENVIRONMENT', 'SSLKEYLOGFILE'):
                self.assertNotIn(variable, result['env'])
            self.assertEqual(result['env']['HOME'], directory)
            self.assertEqual(result['env']['SSL_CERT_FILE'], '/etc/ssl/certs/ca-certificates.crt')
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
                module.install_environment(old, root / 'project')
            self.assertEqual((old / 'marker').read_text(), 'keep')
            project = root / 'project'
            self.project(project)
            new = root / 'new'
            calls = []
            def failing_uv(uv, workspace, *args, **kwargs):
                calls.append(args)
                if args[:1] == ('sync',):
                    raise subprocess.CalledProcessError(1, 'uv')
                return f'uv {module.UV_VERSION} (fixture)' if args == ('--version',) else ''
            with patch.object(module, 'download_uv', return_value=root / 'verified-uv'), \
                    patch.object(module, 'run_uv', side_effect=failing_uv), \
                    patch.object(module, 'validate_root_path'):
                with self.assertRaises(subprocess.CalledProcessError):
                    module.install_environment(new, project)
            self.assertEqual((old / 'marker').read_text(), 'keep')
            self.assertFalse(any(args[:2] == ('pip', 'check') for args in calls))
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['old', 'project'])

    def test_prepare_venv_uses_verified_uv_then_web_uid_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            from types import SimpleNamespace
            web = SimpleNamespace(pw_uid=os.getuid())
            with patch.object(service_install, 'install_environment', create=True) as install, \
                    patch.object(service_install, 'run_as') as run_as:
                environment = service_install.prepare_venv(destination, destination / 'project', web)
            install.assert_called_once_with(environment, destination / 'project')
            self.assertEqual(run_as.call_count, 2)

    def project(self, path):
        path.mkdir()
        source = Path(__file__).resolve().parents[1]
        for name in ('pyproject.toml', 'uv.lock'):
            (path / name).write_bytes((source / name).read_bytes())

    def test_native_sync_uses_private_snapshot_and_final_environment(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            self.project(project)
            original = (project / 'uv.lock').read_bytes()
            environment = root / 'new'
            def run(uv, workspace, *args, **kwargs):
                if args == ('--version',):
                    (project / 'uv.lock').write_text('changed after snapshot')
                    return f'uv {module.UV_VERSION} (fixture)'
                self.assertEqual(args[0], 'sync')
                for flag in ('--locked', '--extra', '--no-dev', '--no-install-project', '--no-build',
                             '--link-mode', '--python'):
                    self.assertIn(flag, args)
                self.assertEqual(args[args.index('--extra')+1], 'web')
                self.assertEqual(args[args.index('--python')+1], '/usr/bin/python3')
                self.assertEqual(kwargs['environment'], environment)
                self.assertNotEqual(workspace, project)
                self.assertEqual((workspace / 'uv.lock').read_bytes(), original)
                return ''
            with patch.object(module, 'validate_root_path'), \
                 patch.object(module, 'download_uv', return_value=root/'uv'), \
                 patch.object(module, 'run_uv', side_effect=run):
                module.install_environment(environment, project)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['project'])

    def test_unsafe_project_or_lock_is_rejected_before_download(self):
        module = self.module()
        cases = [
            ('pyproject.toml', lambda s: s+'\n[tool.uv.sources]\nflask = {path = "/untrusted"}\n'),
            ('pyproject.toml', lambda s: s+'\n[tool.uv.workspace]\nmembers = ["../untrusted"]\n'),
            ('pyproject.toml', lambda s: s+'\n[build-system]\nrequires = ["evil"]\nbuild-backend = "evil"\n'),
            ('pyproject.toml', lambda s: s.replace('Flask>=3.1,<4', 'Flask @ https://invalid.example/f.whl')),
            ('pyproject.toml', lambda s: s+'\n[[tool.uv.index]]\nurl = "https://invalid.example"\n'),
            ('uv.lock', lambda s: s.replace('https://pypi.org/simple', 'https://invalid.example/simple')),
            ('uv.lock', lambda s: s.replace('https://files.pythonhosted.org/', 'http://files.pythonhosted.org/')),
            ('uv.lock', lambda s: s.replace('https://files.pythonhosted.org/', 'https://files.pythonhosted.org.evil.example/')),
            ('uv.lock', lambda s: s.replace('sha256:', 'md5:')),
            ('uv.lock', lambda s: s.replace('virtual = "."', 'editable = "/untrusted"')),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for i, (name, mutate) in enumerate(cases):
                with self.subTest(case=i):
                    project = root / f'project-{i}'
                    self.project(project)
                    (project/name).write_text(mutate((project/name).read_text()))
                    with patch.object(module, 'validate_root_path'), \
                         patch.object(module, 'download_uv') as download:
                        with self.assertRaisesRegex(ValueError, 'project|lock|source|artifact|dependency'):
                            module.install_environment(root/'new', project)
                        download.assert_not_called()
                    self.assertFalse((root/'new').exists())

    def test_project_file_types_and_sizes_are_bounded(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root/'project'
            self.project(project)
            manifest = project/'pyproject.toml'
            manifest.unlink()
            os.mkfifo(manifest)
            with patch.object(module, 'validate_root_path'), \
                 patch.object(module, 'download_uv') as download:
                with self.assertRaises(OSError):
                    module.install_environment(root/'new', project)
                download.assert_not_called()
                manifest.unlink()
                manifest.write_text('x' * (1024 * 1024 + 1))
                with self.assertRaises(OSError):
                    module.install_environment(root/'new', project)
                download.assert_not_called()
