"""Root-owned, bounded previews and idempotent termination records."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time

from .identity import boot_id, describe_target
from .incidents import atomic_write, IDENTIFIER
from .safe_read import read_text

PREVIEW_SECONDS = 120
MAX_RECORDS = 128


def signature(members) -> str:
    data = sorted((p.pid, p.start_ticks, p.uid, p.cgroup, p.name, p.cwd,
                   hashlib.sha256(p.command.encode()).hexdigest()) for p in members)
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


class Actions:
    def __init__(self, path: Path):
        self.path = path

    def load(self):
        try:
            data = json.loads(read_text(self.path, max_bytes=16 * 1024 * 1024))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or len(data) > MAX_RECORDS:
            raise ValueError('Invalid action records')
        return data

    def save(self, rows):
        atomic_write(self.path, rows)

    @staticmethod
    def public(row):
        return {k: v for k, v in row.items() if k not in ('signature', 'members', 'actor', 'boot', 'execution_owner')}

    def preview(self, snapshot, pid, metadata, actor):
        from .processes import descendants_of, kill_block_reason
        target = describe_target(snapshot, pid, metadata)
        members = descendants_of(pid, snapshot.processes)
        if not target['killable']:
            raise ValueError(target['kill_block_reason'])
        if any(kill_block_reason(p) for p in members):
            raise ValueError('보호된 실행기 또는 서비스가 포함돼 있습니다. 원래 앱에서 중지하세요.')
        if len(members) > 256:
            raise ValueError('영향 범위가 너무 큽니다. 원래 앱에서 작업을 나눠 확인하세요.')
        now = time.time()
        rows = self.load()
        rows = {k: v for k, v in rows.items() if now - v['created_at'] < 86400
                and not (v['status'] == 'preview' and now > v['expires_at'])}
        if len(rows) >= MAX_RECORDS:
            raise ValueError('보관 중인 조치가 많습니다. 기존 결과를 먼저 확인하세요.')
        token = secrets.token_hex(16)
        row = {'id': token, 'actor': actor, 'created_at': now, 'expires_at': now + PREVIEW_SECONDS,
               'status': 'preview', 'target': target, 'signature': signature(members), 'boot': boot_id(),
               'members': [{'pid': p.pid, 'start_ticks': p.start_ticks} for p in members],
               'signalled': [], 'remaining': [], 'errors': [],
               'message': '영향 범위를 확인한 뒤 종료를 요청하세요. 저장 여부와 재개 가능성은 확인되지 않았습니다.'}
        rows[token] = row
        if len(json.dumps(rows, ensure_ascii=False).encode()) > 12 * 1024 * 1024:
            raise ValueError('조치 보관 크기 한도에 도달했습니다.')
        self.save(rows)
        return self.public(row)

    def execute(self, token, snapshot, metadata, actor, uid):
        from .processes import descendants_of, signal_verified_process, process_start_ticks
        rows = self.load()
        row = rows.get(token)
        if row is None or row.get('actor') != actor:
            raise ValueError('조치 기록을 찾을 수 없습니다.')
        if row['status'] != 'preview':
            return self.public(row)  # A token authorizes at most one attempt, even after a lost response.
        def reject(message):
            row.update(status='rejected', message=message)
            self.save(rows)
            return self.public(row)
        if time.time() > row['expires_at'] or row['boot'] != boot_id():
            return reject('미리보기가 만료되거나 시스템이 변경됐습니다. 영향 범위를 다시 확인하세요.')
        pid = row['target']['root_pid']
        try:
            target = describe_target(snapshot, pid, metadata)
        except ValueError:
            return reject('대상이 더 이상 관측되지 않습니다. 종료 신호를 보내지 않았습니다.')
        members = descendants_of(pid, snapshot.processes)
        if (target['id'] != row['target']['id'] or not target['killable']
                or signature(members) != row['signature']
                or target['tasks'] != row['target']['tasks']):
            return reject('대상 또는 영향 범위가 변경됐습니다. 종료 신호를 보내지 않았습니다. 다시 확인하세요.')
        row.update(status='executing', progress_at=time.time(), execution_owner=[os.getpid(), process_start_ticks(os.getpid())],
                   message='종료를 요청하고 있습니다. 다시 요청하지 말고 결과를 확인하세요.')
        self.save(rows)  # Durable before the first side effect; a restart never repeats the signals.
        for process in members:
            try:
                if signal_verified_process(process, uid, signal.SIGTERM):
                    row['signalled'].append(process.pid)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                row['errors'].append({'pid': process.pid, 'kind': type(exc).__name__})
                break
            row['progress_at'] = time.time()
            self.save(rows)
        row.update(status='partial' if row['errors'] else 'verifying',
                   message='일부 대상에 요청하지 못했습니다. 남은 프로세스를 확인하세요.' if row['errors'] else
                           '종료 신호를 보냈습니다. 실제 종료와 경보 해소를 확인 중입니다.')
        self.save(rows)
        return self.public(row)

    def status(self, token, snapshot, actor):
        from .processes import process_start_ticks
        rows = self.load()
        row = rows.get(token)
        if row is None or row.get('actor') != actor:
            raise ValueError('조치 기록이 없거나 보관 기간이 지났습니다.')
        if row['status'] in ('preview', 'rejected', 'expired'):
            if row['status'] == 'preview' and time.time() > row['expires_at']:
                row.update(status='expired', message='미리보기가 만료됐습니다. 다시 확인하세요.')
            return self.public(row)
        if row['boot'] != boot_id():
            row.update(status='unknown', message='시스템이 재시작돼 이전 조치 결과를 확인할 수 없습니다.')
        else:
            remaining, unknown = [], []
            for member in row['members']:
                process = snapshot.processes.get(member['pid'])
                if process and process.start_ticks == member['start_ticks'] and process.state not in ('Z', 'X'):
                    remaining.append(member['pid'])
                elif process is None and process_start_ticks(member['pid']) == member['start_ticks']:
                    unknown.append(member['pid'])
            row['remaining'] = remaining
            if unknown:
                row.update(status='unknown', message='일부 프로세스의 현재 상태를 관측하지 못했습니다.')
            elif remaining:
                row.update(status='partial' if row['errors'] else 'remaining',
                           message=f'{len(remaining)}개 프로세스가 아직 실행 중입니다. 원래 앱에서 상태를 확인하세요.')
            else:
                row.update(status='completed', message='확인한 프로세스들의 종료를 확인했습니다. 경보 해소 여부는 별도로 확인하세요.')
            # A new root or tool can be spawned by a supervisor. Never signal it using the old authorization.
            root = snapshot.processes.get(row['target']['root_pid'])
            row['replacement_observed'] = bool(root and root.start_ticks != row['target']['start_ticks'])
        row['checked_at'] = time.time()
        self.save(rows)
        return self.public(row)
