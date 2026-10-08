"""Build deterministic, allowlisted source releases and verify their contents."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsl_resource_guard.build_info import release_revision

ROOT_FILES = {'README.md', 'LICENSE', 'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md',
              'AGENTS.md', 'workthrough/2026-10-07-public-release.md',
              'workthrough/2026-10-08-uv-dependencies.md',
              'workthrough/2026-10-08-review-remediation.md', 'workthrough/2026-10-08-full-code-review.md',
              'pyproject.toml', 'uv.lock', 'install-user.sh',
              'install-root.sh', 'install-services.sh', '.github/workflows/checks.yml',
              '.github/workflows/release.yml', '.github/workflows/ci.yml',
              '.github/workflows/public-checks.yml', '.github/dependabot.yml'}
PREFIXES = ('bin/', 'wsl_resource_guard/', 'config/', 'packaging/', 'tests/', 'scripts/')
DOCS = {'docs/installation.md', 'docs/usage.md', 'docs/configuration.md',
        'docs/architecture.md', 'docs/operations.md'}


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True).strip()


def build(output: Path, ref='HEAD') -> Path:
    commit = git('rev-parse', ref + '^{commit}')
    entries = git('ls-tree', '-r', commit).splitlines()
    content = {}
    modes = {}
    for entry in entries:
        meta, name = entry.split('\t', 1)
        mode, kind, oid = meta.split()
        if name not in ROOT_FILES | DOCS and not name.startswith(PREFIXES):
            continue
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError('Unsupported archive entry: ' + name)
        content[name] = subprocess.check_output(['git', '-C', str(ROOT), 'cat-file', 'blob', oid])
        modes[name] = 0o755 if mode == '100755' else 0o644
    for required in ('pyproject.toml', 'LICENSE', 'bin/wrg', 'install-services.sh',
                     'wsl_resource_guard/web/app.js', 'uv.lock'):
        if required not in content:
            raise ValueError('Missing archive file: ' + required)
    version = tomllib.loads(content['pyproject.toml'].decode())['project']['version']
    manifest = {'schema': 1, 'version': version, 'commit': commit,
                'files': {name: hashlib.sha256(payload).hexdigest() for name, payload in content.items()}}
    content['SOURCE.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    epoch = int(git('show', '-s', '--format=%ct', commit))
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'wsl-resource-guard-{version}.tar.gz'
    with archive.open('wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as tar:
            for name, payload in sorted(content.items()):
                info = tarfile.TarInfo(f'wsl-resource-guard-{version}/{name}')
                info.size, info.mode, info.mtime = len(payload), modes.get(name, 0o644), epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                tar.addfile(info, io.BytesIO(payload))
    (output / 'SHA256SUMS').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
    return archive


def verify(archive: Path):
    with tempfile.TemporaryDirectory(prefix='wrg-release-') as directory:
        root = Path(directory)
        with tarfile.open(archive, 'r:gz') as tar:
            seen = set()
            size = 0
            for member in tar:
                path = PurePosixPath(member.name)
                size += member.size
                if (not member.isfile() or path.is_absolute() or '..' in path.parts
                        or len(path.parts) < 2 or member.name in seen or size > 64 * 1024 * 1024):
                    raise ValueError('Invalid archive member')
                seen.add(member.name)
                target = root / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source:
                    target.write_bytes(source.read())
                target.chmod(member.mode & 0o755)
        children = list(root.iterdir())
        if len(children) != 1 or not children[0].is_dir():
            raise ValueError('Archive must have a single root')
        source = children[0]
        revision = release_revision(source)
        manifest = json.loads((source / 'SOURCE.json').read_text())
        actual = {str(p.relative_to(source)) for p in source.rglob('*') if p.is_file()}
        if actual != set(manifest['files']) | {'SOURCE.json'}:
            raise ValueError('Archive inventory differs from manifest')
        subprocess.run([sys.executable, str(source / 'bin/wrg'), '--help'], cwd=source,
                       check=True, stdout=subprocess.DEVNULL)
        print('Verified archive:', revision['version'], revision['commit'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'verify'])
    parser.add_argument('path', type=Path)
    parser.add_argument('--ref', default='HEAD')
    args = parser.parse_args()
    if args.command == 'build':
        print(build(args.path, args.ref))
    else:
        verify(args.path)
