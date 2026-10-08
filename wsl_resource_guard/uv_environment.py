"""Pinned uv bootstrap for explicit installation, isolated from the invoking user's tools."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import platform
import re
import ssl
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from urllib.parse import urlsplit

from .installer import validate_root_path
from .safe_read import read_text

UV_VERSION = '0.12.23'
# Official release archive digests, reviewed with the version update.
UV_ARCHIVES = {
    'x86_64': ('x86_64-unknown-linux-gnu', '9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6'),
    'aarch64': ('aarch64-unknown-linux-gnu', '6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f'),
}
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_BINARY = 128 * 1024 * 1024


def extract_uv(data: bytes, target: Path, prefix: str, digest: str) -> None:
    if len(data) > MAX_ARCHIVE or hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError('uv archive checksum mismatch or size limit exceeded')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        candidates = [entry for entry in archive.getmembers() if entry.name == f'{prefix}/uv']
        if len(candidates) != 1 or not candidates[0].isreg() or not 0 < candidates[0].size <= MAX_BINARY:
            raise RuntimeError('uv archive must contain one bounded regular executable')
        stream = archive.extractfile(candidates[0])
        if stream is None:
            raise RuntimeError('uv executable missing')
        with stream:
            content = stream.read(MAX_BINARY + 1)
        if len(content) != candidates[0].size:
            raise RuntimeError('uv executable size mismatch')
    # Never extract archive paths or follow an existing destination link.
    with target.open('xb') as output:
        output.write(content)
    target.chmod(0o700)


def download_uv(workspace: Path) -> Path:
    if platform.system() != 'Linux' or platform.machine() not in UV_ARCHIVES:
        raise RuntimeError('Web installation supports Linux x86_64/aarch64 with glibc')
    triple, digest = UV_ARCHIVES[platform.machine()]
    prefix = f'uv-{triple}'
    url = f'https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{prefix}.tar.gz'
    # No inherited proxy, user CA bundle, uv config, PATH lookup or shell installer.
    # create_default_context also consumes SSLKEYLOGFILE from the caller.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile='/etc/ssl/certs/ca-certificates.crt')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=context))
    with opener.open(url, timeout=30) as response:
        data = response.read(MAX_ARCHIVE + 1)
    executable = workspace / 'uv'
    extract_uv(data, executable, prefix, digest)
    return executable


def run_uv(executable: Path, workspace: Path, *args: str, environment: Path | None = None) -> str:
    config = workspace / 'uv.toml'
    if not config.exists():
        config.write_text('')
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(workspace), 'LANG': 'C.UTF-8',
           'XDG_CONFIG_HOME': str(workspace), 'XDG_CACHE_HOME': str(workspace / 'cache'),
           'XDG_DATA_HOME': str(workspace / 'data'), 'TMPDIR': str(workspace),
           'SSL_CERT_FILE': '/etc/ssl/certs/ca-certificates.crt'}
    if environment is not None:
        env['UV_PROJECT_ENVIRONMENT'] = str(environment)
    try:
        result = subprocess.run([str(executable), '--config-file', str(config), '--no-cache',
                                 '--no-python-downloads', '--no-managed-python', *args],
                                cwd=workspace, env=env, umask=0o022, check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    except subprocess.CalledProcessError as exc:
        # Keep uv's diagnostic visible to an administrator and available to callers.
        if exc.stderr:
            print(exc.stderr, file=sys.stderr, end='')
        raise
    return result.stdout


def project_snapshot(project: Path) -> dict[str, str]:
    """Accept only the reviewed virtual project and hashed public registry inputs.

    uv's empty config file isolates user settings, but pyproject still controls
    sources and builds. Validate before running uv, and give it these exact bytes
    in a private directory so surrounding workspaces/.python-version cannot apply.
    uv --locked additionally checks dependency resolution against the manifest.
    """
    files = {name: read_text(project / name, max_bytes=limit) for name, limit in
             (('pyproject.toml', 1024 * 1024), ('uv.lock', 16 * 1024 * 1024))}
    manifest, lock = (tomllib.loads(files[name]) for name in ('pyproject.toml', 'uv.lock'))
    metadata = manifest.get('project', {})
    if (not isinstance(metadata, dict) or metadata.get('name') != 'wsl-resource-guard'
            or not isinstance(metadata.get('version'), str) or 'dynamic' in metadata
            or 'build-system' in manifest
            or manifest.get('tool', {}).get('uv') != {'package': False}):
        raise ValueError('Unsupported project metadata or build configuration')
    dependencies = metadata.get('dependencies', [])
    groups = manifest.get('dependency-groups', {})
    extras = metadata.get('optional-dependencies', {})
    if not isinstance(groups, dict) or not isinstance(extras, dict) or 'web' not in extras:
        raise ValueError('Invalid project dependency groups')
    for requirements in (dependencies, *groups.values(), *extras.values()):
        if not isinstance(requirements, list) or any(
                not isinstance(item, str) or not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]*', item)
                or '@' in item or '://' in item for item in requirements):
            raise ValueError('Only registry dependency declarations are supported')
    packages = lock.get('package')
    if lock.get('version') != 1 or not isinstance(packages, list) or not packages:
        raise ValueError('Invalid project lock')
    roots = 0
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError('Invalid lock package')
        source = package.get('source')
        if source == {'virtual': '.'}:
            roots += 1
            if package.get('name') != metadata['name'] or package.get('version') != metadata['version']:
                raise ValueError('Project lock identity differs')
            continue
        if source != {'registry': 'https://pypi.org/simple'}:
            raise ValueError('Only public PyPI lock sources are supported')
        artifacts = package.get('wheels', [])
        if not isinstance(artifacts, list):
            raise ValueError('Invalid lock artifact list')
        artifacts = artifacts + ([package['sdist']] if 'sdist' in package else [])
        if not artifacts:
            raise ValueError('Missing lock artifacts')
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError('Invalid lock artifact')
            url = urlsplit(artifact.get('url', ''))
            if (url.scheme != 'https' or url.netloc != 'files.pythonhosted.org'
                    or not url.path.startswith('/packages/') or url.query or url.fragment
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(artifact.get('hash', '')))
                    or type(artifact.get('size')) is not int or artifact['size'] <= 0):
                raise ValueError('Invalid lock artifact origin, hash or size')
    if roots != 1:
        raise ValueError('Project lock must contain one virtual root')
    return files


def install_environment(environment: Path, project: Path) -> None:
    """Prepare a new final-path environment; never mutate an existing runtime."""
    if environment.exists() or environment.is_symlink():
        raise FileExistsError(f'Environment already exists: {environment}')
    validate_root_path(environment)
    files = project_snapshot(project)
    environment.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with tempfile.TemporaryDirectory(prefix='.uv-', dir=environment.parent) as directory:
        workspace = Path(directory)
        for name, content in files.items():
            (workspace / name).write_text(content)
        executable = download_uv(workspace)
        version = run_uv(executable, workspace, '--version')
        if not version.startswith(f'uv {UV_VERSION} '):
            raise RuntimeError('Unexpected uv version')
        run_uv(executable, workspace, 'sync', '--locked', '--extra', 'web', '--no-dev',
               '--no-install-project', '--no-build', '--link-mode', 'copy',
               '--python', '/usr/bin/python3', environment=environment)
