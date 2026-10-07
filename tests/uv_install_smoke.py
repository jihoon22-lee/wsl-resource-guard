"""Root-only installer acceptance in disposable /opt paths; never controls services."""
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
from unittest.mock import patch

from wsl_resource_guard.service_install import prepare_venv
from wsl_resource_guard.uv_environment import install_environment


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run only as root in an isolated CI runner/container')
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='wrg-uv-ci-', dir='/opt') as directory:
        root = Path(directory)
        root.chmod(0o755)
        web = pwd.getpwnam('nobody')
        with patch.dict(os.environ, {'UV_INDEX_URL': 'https://invalid.example',
                                     'PIP_INDEX_URL': 'https://invalid.example',
                                     'UV_CONFIG_FILE': '/missing-user-config'}):
            old = prepare_venv(root, source / 'requirements-web.txt', web, root / '.venvs/first')
            new = prepare_venv(root, source / 'requirements-web.txt', web, root / '.venvs/second')
        for environment in (old, new):
            assert (environment / 'bin/gunicorn').read_text().splitlines()[0] == f'#!{environment}/bin/python'
            subprocess.run([str(environment / 'bin/python'), '-c',
                            'import flask,gunicorn; assert gunicorn.__version__ == "26.2.0"'], check=True)
        invalid = root / 'invalid.txt'
        invalid.write_text('flask==3.1.3 --hash=sha256:' + '0' * 64 + '\n')
        try:
            install_environment(root / '.venvs/failed', invalid)
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError('invalid dependency hash accepted')
        # Both previously prepared runtimes survive a failed replacement.
        for environment in (old, new):
            subprocess.run([str(environment / 'bin/gunicorn'), '--version'], check=True)
        assert not list((root / '.venvs').glob('.uv-*'))
        print('uv root install: fresh, update, web UID, fixed shebang, invalid hash, old runtimes preserved')


if __name__ == '__main__':
    main()
