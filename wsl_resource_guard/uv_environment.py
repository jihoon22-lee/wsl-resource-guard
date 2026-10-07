"""Pinned uv bootstrap for explicit installation, isolated from the invoking user's tools."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import platform
import ssl
import subprocess
import tarfile
import tempfile
import urllib.request

from .installer import validate_root_path

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
    context = ssl.create_default_context(cafile='/etc/ssl/certs/ca-certificates.crt')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=context))
    with opener.open(url, timeout=30) as response:
        data = response.read(MAX_ARCHIVE + 1)
    executable = workspace / 'uv'
    extract_uv(data, executable, prefix, digest)
    return executable


def run_uv(executable: Path, workspace: Path, *args: str) -> str:
    config = workspace / 'uv.toml'
    if not config.exists():
        config.write_text('')
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(workspace), 'LANG': 'C.UTF-8',
           'XDG_CONFIG_HOME': str(workspace), 'XDG_CACHE_HOME': str(workspace / 'cache'),
           'XDG_DATA_HOME': str(workspace / 'data'), 'TMPDIR': str(workspace)}
    result = subprocess.run([str(executable), '--config-file', str(config), '--no-cache',
                             '--no-python-downloads', '--no-managed-python', *args],
                            cwd=workspace, env=env, umask=0o022, check=True, text=True,
                            stdout=subprocess.PIPE, timeout=600)
    return result.stdout


def install_environment(environment: Path, requirements: Path) -> None:
    """Prepare a new final-path environment; never mutate an existing runtime."""
    if environment.exists() or environment.is_symlink():
        raise FileExistsError(f'Environment already exists: {environment}')
    validate_root_path(environment)
    environment.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with tempfile.TemporaryDirectory(prefix='.uv-', dir=environment.parent) as directory:
        workspace = Path(directory)
        # Snapshot the reviewed lock into the private installer workspace.
        locked = workspace / 'requirements.txt'
        locked.write_bytes(requirements.read_bytes())
        executable = download_uv(workspace)
        version = run_uv(executable, workspace, '--version')
        if not version.startswith(f'uv {UV_VERSION} '):
            raise RuntimeError('Unexpected uv version')
        run_uv(executable, workspace, 'venv', '--python', '/usr/bin/python3', str(environment))
        python = str(environment / 'bin/python')
        run_uv(executable, workspace, 'pip', 'sync', '--python', python, '--require-hashes',
               '--no-build', '--link-mode', 'copy', '--index-url', 'https://pypi.org/simple', str(locked))
        run_uv(executable, workspace, 'pip', 'check', '--python', python)
