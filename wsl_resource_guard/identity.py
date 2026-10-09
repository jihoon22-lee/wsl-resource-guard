"""Evidence for human-readable process identity; never infer a conversation from cwd."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re

from .safe_read import read_text

UUID = re.compile(r'^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$')


def boot_id() -> str:
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return ''


def process_identity(process, boot: str | None = None) -> str:
    boot = boot_id() if boot is None else boot
    if not boot or not process.start_ticks:
        return ''
    return hashlib.sha256(f'{boot}:{process.uid}:{process.pid}:{process.start_ticks}'.encode()).hexdigest()[:32]


def tool_label(members) -> str:
    commands = ' '.join(p.command.lower() for p in members)
    if '@playwright/mcp' in commands or 'playwright-mcp' in commands:
        return 'Playwright 브라우저 도구'
    if 'chrome-devtools' in commands:
        return 'Chrome DevTools 도구'
    return 'MCP 도구 · ' + (members[-1].name if members else '프로그램 미확인')


def task_metadata(home: Path, pids: list[int]) -> dict:
    """Runs only as the owner. Read explicit IDs, never prompts or arbitrary environment values."""
    from .processes import process_start_ticks
    if len(pids) > 512 or any(type(pid) is not int or pid <= 1 for pid in pids):
        raise ValueError('Invalid process identities')
    rows = {}
    for pid in pids:
        proc = Path('/proc') / str(pid)
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            start = process_start_ticks(pid)
            with (proc / 'environ').open('rb') as stream:
                raw = stream.read(262145)
            if len(raw) > 262144:
                continue
            fields = dict(item.split(b'=', 1) for item in raw.split(b'\0')
                          if item.startswith((b'CODEX_THREAD_ID=', b'CODEX_SESSION_ID=')))
            value = fields.get(b'CODEX_THREAD_ID', fields.get(b'CODEX_SESSION_ID', b'')).decode('ascii')
            source, provider = 'explicit_environment', 'codex'
            if not value:
                name = (proc / 'comm').read_text().strip().lower()
                if name in ('claude', 'codex'):
                    with (proc / 'cmdline').open('rb') as stream:
                        command = stream.read(262145)
                    if len(command) <= 262144:
                        argv = command.decode(errors='replace').split('\0')
                        for index, argument in enumerate(argv[:-1]):
                            explicit = argument in ('--session-id', '--resume') or (argument == 'resume' and index == 1 and name == 'codex')
                            if explicit and UUID.fullmatch(argv[index + 1]):
                                value = argv[index + 1]
                                source, provider = 'explicit_argument', name
                                break
            if UUID.fullmatch(value) and start and start == process_start_ticks(pid):
                rows[str(pid)] = {'thread_id': value, 'start_ticks': start, 'source': 'explicit_environment',
                                  'title': '', 'title_status': 'unavailable', 'provider': provider}
                rows[str(pid)]['source'] = source
        except (OSError, ValueError):
            continue
    if not rows:
        return rows
    wanted = {row['thread_id'] for row in rows.values() if row['provider'] == 'codex'}
    names = {}
    try:
        content = read_text(home / '.codex/session_index.jsonl', max_bytes=2 * 1024 * 1024, tail=True)
        for line in content.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (isinstance(entry, dict) and isinstance(entry.get('id'), str)
                    and entry['id'] in wanted and isinstance(entry.get('thread_name'), str)):
                names[entry['id']] = ''.join(c for c in entry['thread_name'] if c.isprintable())[:200]
    except (OSError, ValueError):
        pass
    for row in rows.values():
        if row['provider'] == 'codex' and names.get(row['thread_id']):
            row.update(title=names[row['thread_id']], title_status='matched_local_index')
    return rows


def describe_target(snapshot, pid: int, metadata: dict | None = None) -> dict:
    from .processes import descendants_of, kill_block_reason, _persistent_provider
    root = snapshot.processes.get(pid)
    if root is None:
        raise ValueError('Target is no longer observable')
    session = next((s for s in snapshot.sessions if s.root_pid == pid), None)
    mcp = next((s for s in snapshot.mcp_groups if s.root_pid == pid), None)
    if session is None and mcp is None:
        raise ValueError('Target is not an agent or tool root')
    members = descendants_of(pid, snapshot.processes)
    shared = bool(_persistent_provider(root.name.lower(), root.command.lower(), root.argv))
    kind = 'shared_runtime' if shared else 'tool' if mcp and not session else 'agent_process'
    projects = ([p.to_dict() for p in session.projects] if session else
                [{'project': mcp.project, 'rss_kib': mcp.rss_kib, 'process_count': mcp.process_count}])
    tasks = {}
    for process in members:
        row = (metadata or {}).get(str(process.pid), {})
        if row.get('start_ticks') == process.start_ticks and row.get('thread_id'):
            tasks[row['thread_id']] = {k: row.get(k) for k in ('thread_id', 'title', 'title_status', 'source')}
    reason = kill_block_reason(root)
    if not reason and len(tasks) > 1:
        reason = '서로 다른 작업 ID가 연결돼 있습니다. 원래 앱에서 작업별로 중지하세요.'
    if not reason and (len(members) > 256 or not all(p.start_ticks for p in members)):
        reason = '전체 영향 범위의 신원을 확인하지 못했습니다.'
    ident = process_identity(root)
    if not reason and not ident:
        reason = '현재 프로세스 신원을 확인하지 못했습니다.'
    usage = session or mcp
    display_name = tool_label(members) if kind == 'tool' else 'Codex 앱 서버' if shared and usage.provider == 'codex' else root.name
    return {'id': ident, 'root_pid': pid, 'start_ticks': root.start_ticks, 'kind': kind,
            'provider': usage.provider, 'name': display_name, 'cwd': root.cwd[:500],
            'project': usage.project, 'projects': projects[:64], 'projects_complete': len(projects) <= 64,
            'tasks': list(tasks.values())[:32], 'tasks_complete': len(tasks) <= 32,
            'task_identity': 'explicit' if tasks else 'unavailable',
            'identity_note': '명시된 작업 ID와 로컬 제목을 연결했습니다.' if tasks else
                             '대화 제목과 진행 여부를 확인할 수 없습니다. 프로젝트 위치만으로 대화를 추정하지 않습니다.',
            'rss_kib': usage.rss_kib, 'swap_kib': usage.swap_kib, 'age_seconds': usage.age_seconds,
            'process_count': len(members), 'mcp_tree_count': sum(g.root_pid in {p.pid for p in members} for g in snapshot.mcp_groups),
            'member_names': sorted({p.name for p in members})[:20],
            'killable': not reason, 'kill_block_reason': reason,
            'impact': '연결된 여러 프로젝트의 도구와 진행 중 작업이 함께 중단될 수 있습니다.' if shared else
                      '이 대상과 자식 프로세스에 종료를 요청합니다. 진행 중 작업과 도구 호출이 중단될 수 있습니다.',
            'recovery_note': '저장·작업 재개 여부와 외부 앱의 자동 재시작은 확인되지 않았습니다.',
            'memory_note': '현재 RSS 합계입니다. 공유 메모리가 중복될 수 있으며 실제 회수량을 보장하지 않습니다.'}
