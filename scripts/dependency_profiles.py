"""Check and optionally audit web/dev exports from the same committed uv lock."""
import argparse
from importlib import metadata
from pathlib import Path
import subprocess
import sys
import tempfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def export_versions(content: str) -> dict[str, str]:
    versions = {}
    for line in content.splitlines():
        line = line.strip().removesuffix('\\').strip()
        if not line or line.startswith(('#', '--hash=')):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        pins = list(requirement.specifier)
        if requirement.url or len(pins) != 1 or pins[0].operator != '==' or '*' in pins[0].version:
            raise ValueError('Export must contain exact registry versions')
        name, version = canonicalize_name(requirement.name), pins[0].version
        if name in versions and versions[name] != version:
            raise ValueError(f'Conflicting exported versions: {name}')
        versions[name] = version
    if not versions:
        raise ValueError('Empty dependency profile')
    return versions


def check_versions(web: dict[str, str], dev: dict[str, str], installed: dict[str, str]) -> None:
    for name, version in web.items():
        if dev.get(name) != version:
            raise ValueError(f'Web/development version mismatch: {name}')
    for name, version in dev.items():
        if installed.get(name) != version:
            raise ValueError(f'Installed development version mismatch: {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--uv', default='uv')
    args = parser.parse_args()
    before = (ROOT/'uv.lock').read_bytes()
    with tempfile.TemporaryDirectory(prefix='wrg-dependencies-') as directory:
        exports = {}
        versions = {}
        for profile, flags in (('web', ['--no-dev']), ('dev', ['--group', 'dev'])):
            path = Path(directory)/f'{profile}.txt'
            subprocess.run([args.uv, '--no-python-downloads', 'export', '--locked', '--extra', 'web',
                            *flags, '--no-emit-project', '--format', 'requirements-txt',
                            '--output-file', str(path)], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
            exports[profile] = path
            versions[profile] = export_versions(path.read_text())
        installed = {canonicalize_name(dist.metadata['Name']): dist.version for dist in metadata.distributions()}
        check_versions(versions['web'], versions['dev'], installed)
        print(f"Shared uv lock: web {len(versions['web'])}, development {len(versions['dev'])}; installed versions match")
        if args.audit:
            for profile, path in exports.items():
                print(f'Auditing {profile} from uv.lock', flush=True)
                subprocess.run([sys.executable, '-m', 'pip_audit', '--require-hashes', '--disable-pip',
                                '--cache-dir', str(Path(directory)/'audit-cache'), '-r', str(path)], cwd=ROOT, check=True)
    if (ROOT/'uv.lock').read_bytes() != before:
        raise ValueError('Verification changed uv.lock')


if __name__ == '__main__':
    main()
