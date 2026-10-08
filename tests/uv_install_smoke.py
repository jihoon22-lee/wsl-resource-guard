"""Root-only dependency acceptance in disposable paths; never controls services."""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile
import tomllib
from unittest.mock import patch

from wsl_resource_guard.service_install import prepare_venv
from wsl_resource_guard.uv_environment import install_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    source = parser.parse_args().project.resolve()
    if os.geteuid() != 0:
        raise RuntimeError('Run only as root in an isolated CI runner/container')
    locked = tomllib.loads((source / 'uv.lock').read_text())
    versions = {p['name']: p['version'] for p in locked['package']}
    with tempfile.TemporaryDirectory(prefix='wrg-uv-ci-', dir='/var/lib') as directory:
        root = Path(directory)
        root.chmod(0o755)
        web = pwd.getpwnam('nobody')
        with patch.dict(os.environ, {'UV_INDEX_URL': 'https://invalid.example',
                                     'PIP_INDEX_URL': 'https://invalid.example',
                                     'UV_PROJECT_ENVIRONMENT': '/must-not-use',
                                     'UV_CONFIG_FILE': '/missing-user-config',
                                     'SSL_CERT_FILE': '/missing-user-ca',
                                     'SSLKEYLOGFILE': str(root / 'must-not-exist.log')}):
            old = prepare_venv(root, source, web, root / '.venvs/first')
            new = prepare_venv(root, source, web, root / '.venvs/second')
        assert not (root / 'must-not-exist.log').exists()
        for environment in (old, new):
            assert (environment / 'bin/gunicorn').read_text().splitlines()[0] == f'#!{environment}/bin/python'
            packages = json.loads(subprocess.check_output([str(environment/'bin/python'), '-c',
                'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"].lower():d.version for d in m.distributions()}))'], text=True))
            assert packages['flask'] == versions['flask']
            assert packages['gunicorn'] == versions['gunicorn']
            assert 'playwright' not in packages and 'pip-audit' not in packages
            assert all(versions[name] == version for name, version in packages.items()), packages
        original = (source/'uv.lock').read_text()
        flask = re.search(r'(?ms)^\[\[package\]\]\nname = "flask"\n.*?(?=^\[\[package\]\]|\Z)', original)
        assert flask is not None
        corrupted = original[:flask.start()] + re.sub(r'sha256:[a-f0-9]{64}', 'sha256:'+'0'*64, flask.group()) + original[flask.end():]
        cases = {
            'bad-hash': ((source/'pyproject.toml').read_text(), corrupted, 'Hash mismatch'),
            'stale-lock': ((source/'pyproject.toml').read_text().replace('Flask>=3.1,<4', 'Flask>=3.1,<3.2'), original, 'lockfile'),
            'missing-wheel': ((source/'pyproject.toml').read_text(), original[:flask.start()] + re.sub(r'(?s)wheels = \[.*?\]\n', '', flask.group()) + original[flask.end():], 'no-build'),
        }
        for name, (manifest, lock, expected) in cases.items():
            project = root/name
            project.mkdir()
            (project/'pyproject.toml').write_text(manifest)
            (project/'uv.lock').write_text(lock)
            try:
                install_environment(root/'.venvs'/name, project)
            except subprocess.CalledProcessError as exc:
                assert expected.lower() in exc.stderr.lower(), exc.stderr
                print(f'uv rejected {name}')
            else:
                raise AssertionError(f'{name} accepted')
        for environment in (old, new):
            subprocess.run([str(environment / 'bin/gunicorn'), '--version'], check=True)
        assert not list((root / '.venvs').glob('.uv-*'))
        print('uv root install: fresh, update, web UID, fixed shebang, corrupt/stale/no-wheel locks, prior runtimes preserved')


if __name__ == '__main__':
    main()
