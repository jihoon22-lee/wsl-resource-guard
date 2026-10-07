"""Inspect publishable tracked text without echoing potentially sensitive values."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_HOME = {'demo', 'user', 'example', 'test', 'testuser', 'alice', 'bob', 'fixture', 'wrg-test', 'owner'}
issues = []
paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
for name in filter(None, paths):
    path = ROOT / name
    if not path.is_file():
        continue
    try:
        text = path.read_text()
    except UnicodeError:
        issues.append((name, 0, 'unexpected binary; review explicitly'))
        continue
    for number, line in enumerate(text.splitlines(), 1):
        if re.search(r'\b[a-z0-9-]+\.tail[0-9a-z]+\.ts\.net\b', line, re.I):
            issues.append((name, number, 'private Tailnet hostname'))
        for match in re.finditer(r'/home/([A-Za-z0-9_-]+)(?:/|\b)', line):
            if match[1] not in ALLOWED_HOME:
                issues.append((name, number, 'non-example home path'))
        for match in re.finditer(r'(?<![A-Za-z0-9_/:])[A-Za-z0-9_.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})', line):
            if match[1].lower() not in {'example.com', 'example.org', 'example.test', 'users.noreply.github.com'}:
                issues.append((name, number, 'non-example email address'))
        for match in re.finditer(r'(?:/mnt/[a-z]/Users/|[A-Z]:[\\/]Users[\\/])([^/\\\s]+)', line):
            if match[1] not in {'Example', 'USER', 'User', 'demo'}:
                issues.append((name, number, 'non-example Windows user path'))
    if path.suffix == '.md':
        for link in re.findall(r'\]\(([^)]+)\)', text):
            target = link.split('#')[0].strip('<>')
            if not target or ':' in target or target.startswith('/'):
                continue
            if not (path.parent / target).exists():
                issues.append((name, 0, 'missing relative documentation target'))
for name, line, kind in issues:
    print(f'{name}:{line}: {kind}')
raise SystemExit(bool(issues))
