"""Install stamps, so `wrg doctor` can spot code copies from different commits."""
from __future__ import annotations

import json
import hashlib
import re
import tomllib
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time
from .safe_read import read_text

STAMP = "BUILD.json"
WEB_PACKAGE = Path("/opt/wsl-resource-guard/wsl_resource_guard")


def release_revision(source: Path) -> dict:
    """Verify local archive integrity; this is not a signature/authenticity check."""
    try:
        data = json.loads(read_text(source / 'SOURCE.json', max_bytes=1024 * 1024))
        if (not isinstance(data, dict) or data.get('schema') != 1
                or not isinstance(data.get('commit'), str)
                or not re.fullmatch(r'[0-9a-f]{40}', data['commit'])
                or not isinstance(data.get('files'), dict) or not data['files']
                or len(data['files']) > 2000):
            raise ValueError('invalid release metadata')
        for name, digest in data['files'].items():
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or str(path) != name
                    or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
                raise ValueError('invalid release file entry')
            target = source / name
            if not target.resolve().is_relative_to(source.resolve()) or target.is_symlink():
                raise ValueError('release path escapes source')
            payload = read_text(target, max_bytes=16 * 1024 * 1024).encode('utf-8')
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError('release file checksum mismatch: ' + name)
        actual = {str(path.relative_to(source)) for path in source.rglob('*')
                  if (path.is_file() or path.is_symlink())
                  and '__pycache__' not in path.relative_to(source).parts}
        if actual != set(data['files']) | {'SOURCE.json'}:
            raise ValueError('release inventory differs from manifest')
        if 'pyproject.toml' not in data['files']:
            raise ValueError('release has no project metadata')
        version = tomllib.loads(read_text(source / 'pyproject.toml'))['project']['version']
        if data.get('version') != version:
            raise ValueError('release version mismatch')
        return {'commit': data['commit'], 'dirty': False, 'version': version}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid release metadata or content') from exc


def source_revision(source: Path) -> dict:
    def git(*args: str) -> str | None:
        # Installers run git as root inside the owner's checkout; trust only that path.
        result = subprocess.run(
            ["git", "-c", f"safe.directory={source}", "-C", str(source), *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    try:
        top = git('rev-parse', '--show-toplevel')
        commit = git("rev-parse", "HEAD") if top and Path(top).resolve() == source.resolve() else None
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, False
    if not commit and (source / 'SOURCE.json').exists():
        return release_revision(source)
    return {"commit": commit or "unknown", "dirty": dirty}


def write_stamp(package_dir: Path, source: Path) -> dict:
    stamp = {**source_revision(source), "installed_at": time.time()}
    (package_dir / STAMP).write_text(json.dumps(stamp) + "\n", encoding="utf-8")
    return stamp


def read_stamp(package_dir: Path) -> dict | None:
    try:
        data = json.loads(read_text(package_dir / STAMP, max_bytes=65536))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("commit"), str) else None


def compare_stamps(cli: dict | None, web: dict | None, web_installed: bool) -> tuple[str, str]:
    """(level, detail) for `wrg doctor`'s code-copy check."""
    if cli is None:
        return "WARN", "설치 스탬프 없음 — 저장소에서 직접 실행 중이거나 이전 방식 설치본"
    if cli.get('commit') == 'unknown' or (web_installed and (web or {}).get('commit') == 'unknown'):
        return 'WARN', '설치 커밋을 확인할 수 없습니다 (unknown). 공식 릴리스 또는 Git 체크아웃을 사용하세요.'
    label = cli["commit"][:12] + (" (dirty)" if cli.get("dirty") else "")
    if not web_installed:
        return "OK", f"{label} · 웹·서비스 관리 미설치"
    if web is None:
        return "WARN", f"CLI {label} · 웹 사본 스탬프 없음 — install-services.sh를 다시 실행하세요"
    if web["commit"] != cli["commit"] or bool(web.get("dirty")) != bool(cli.get("dirty")):
        return "WARN", (f"CLI {label} ≠ 웹 {web['commit'][:12]} — "
                        "install-root.sh와 install-services.sh를 같은 커밋에서 실행하세요")
    return "OK", label


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python3 -m wsl_resource_guard.build_info <package_dir> <source_dir>", file=sys.stderr)
        return 2
    stamp = write_stamp(Path(args[0]), Path(args[1]))
    print(f"build stamp {stamp['commit'][:12]}{' (dirty)' if stamp['dirty'] else ''} -> {args[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
