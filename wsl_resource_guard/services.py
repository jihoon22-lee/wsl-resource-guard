"""Terminal client for the same controller used by the web dashboard."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import socket
import sys

SOCKET = '/run/wrg-services/control.sock'
ACTIONS = ('status', 'discover', 'register', 'enable', 'disable', 'autostart-on',
           'autostart-off', 'start', 'stop', 'restart', 'remove', 'logs', 'audit', 'restore')
LABELS = {'enable': '사용 (자동 실행 + 지금 시작)', 'disable': '미사용 (자동 실행 해제 + 지금 중지)',
          'autostart-on': '자동 실행만 켜기', 'autostart-off': '자동 실행만 끄기',
          'start': '지금만 시작', 'stop': '지금만 중지', 'restart': '지금 재시작',
          'remove': '등록 삭제', 'logs': '최근 로그'}
AUDIT_OP_LABELS = {'register': '등록', 'restore': '복구', 'logs': '로그 조회', 'action': '변경', 'kill': '세션 종료'}


class ServiceError(Exception):
    def __init__(self, message: str, *, code: str = ''):
        super().__init__(message)
        self.code = code


# Fixed protocol codes, never arbitrary HTTP status supplied by the caller.
ERROR_STATUSES = {'config_pending': 409, 'config_not_found': 404, 'config_unavailable': 503,
                  'incident_not_found': 404, 'target_not_found': 404, 'action_not_found': 404,
                  'action_unavailable': 409, 'preview_required': 409}


class ServiceUnavailable(ServiceError):
    """The controller could not be reached or returned an unusable reply."""


def request_control(payload: dict, socket_path: str = SOCKET) -> object:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(120)
            connection.connect(socket_path)
            connection.sendall(json.dumps(payload).encode() + b'\n')
            with connection.makefile('rb') as handle:
                raw = handle.readline(8 * 1024 * 1024)
        result = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ServiceUnavailable('서비스 관리자에 연결할 수 없습니다. wrg-service-control.service 상태를 확인하세요.') from exc
    if not result.get('ok'):
        code = result.get('code')
        raise ServiceError(result.get('error', '서비스 관리 실패'),
                           code=code if isinstance(code, str) and code in ERROR_STATUSES else '')
    return result['data']


def print_status(data: dict) -> None:
    print(f"{'ID':<25} {'자동 실행':<9} {'실행 상태':<12} 서비스")
    for row in data['services']:
        auto = '켜짐' if row['autostart'] else '꺼짐'
        print(f"{row['id']:<25} {auto:<9} {row.get('state', '?'):<12} {row['name']}")
        if row.get('error'):
            print('  ! ' + row['error'])
    print(f"\n웹 화면: {data['origin']}")


def print_audit(records: list) -> None:
    print(f"{'시각':<20} {'서비스':<25} {'작업':<14} 결과")
    for record in reversed(records):
        try:
            stamp = datetime.fromisoformat(str(record.get('time', ''))).astimezone().strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            stamp = '?'
        op = str(record.get('action') or record.get('op') or '')
        label = LABELS.get(op, AUDIT_OP_LABELS.get(op, op))
        result = '성공' if record.get('ok') else f"실패: {record.get('error', '')}"
        print(f"{stamp:<20} {str(record.get('id', ''))[:25]:<25} {label[:14]:<14} {result}")


def select_registration() -> None:
    candidates = request_control({'op': 'discover'})
    if not candidates:
        print('새로 등록할 서비스를 찾지 못했습니다.')
        return
    term = input('등록할 이름 검색 (Enter=전체): ').strip().lower()
    candidates = [c for c in candidates if term in (c['name'] + c['target']).lower()]
    for index, candidate in enumerate(candidates, 1):
        print(f"{index:>3}. {candidate['name']} [{candidate['kind']}] {candidate['target']}")
    choice = input('등록할 번호 (Enter=취소): ').strip()
    if choice.isdigit() and 0 < int(choice) <= len(candidates):
        result = request_control({'op': 'register', 'key': candidates[int(choice) - 1]['key']})
        print(result['message'])


def menu() -> int:
    while True:
        data = request_control({'op': 'list'})
        print_status(data)
        for index, row in enumerate(data['services'], 1):
            print(f"{index:>2}. {row['name']}", end='   ')
        print('\n번호=서비스 선택  a=등록  r=새로고침  0=나가기')
        choice = input('선택: ').strip().lower()
        if choice == '0':
            return 0
        try:
            if choice == 'a':
                select_registration()
                continue
            if not choice.isdigit() or not 0 < int(choice) <= len(data['services']):
                continue
            row = data['services'][int(choice) - 1]
            print(f"\n{row['name']} · {row.get('detail', '')}")
            actions = list(LABELS)
            for index, action in enumerate(actions, 1):
                print(f'{index}. {LABELS[action]}')
            selection = input('작업 번호 (Enter=취소): ').strip()
            if not selection.isdigit() or not 0 < int(selection) <= len(actions):
                continue
            action = actions[int(selection) - 1]
            if action == 'logs':
                print(request_control({'op': 'logs', 'id': row['id'], 'lines': 80})['text'])
                input('Enter=돌아가기')
                continue
            confirmed = False
            if row['category'] == 'foundation' or action == 'remove':
                print(row.get('notice', ''))
                if action == 'remove':
                    print('자동 실행 해제·중지 후 등록을 삭제합니다. 앱 데이터는 보존합니다.')
                confirmed = input('진행할까요? [y/N]: ').strip().lower() == 'y'
                if not confirmed:
                    continue
            result = request_control({'op': 'action', 'id': row['id'], 'action': action, 'confirmed': confirmed})
            print(result['message'])
        except ServiceError as exc:
            print(f'작업 실패: {exc}', file=sys.stderr)


def cmd_services(args: argparse.Namespace) -> int:
    try:
        action = args.action
        identifier = getattr(args, 'service', None)
        if not action and sys.stdin.isatty():
            return menu()
        if action in (None, 'status'):
            result = request_control({'op': 'list'})
            if identifier:
                result['services'] = [row for row in result['services'] if row['id'] == identifier]
                if not result['services']:
                    raise ServiceError('해당 서비스가 등록되어 있지 않습니다.')
            if getattr(args, 'json', False):
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print_status(result)
        elif action == 'discover':
            for candidate in request_control({'op': 'discover'}):
                print(f"{candidate['key']:<55} {candidate['name']}")
        elif action == 'register':
            if identifier:
                key = identifier if ':' in identifier else 'systemd:' + identifier.removesuffix('.service') + '.service'
                payload = {'op': 'register', 'key': key}
                for field in ('name', 'url'):
                    value = getattr(args, field, None)
                    if value is not None:
                        payload[field] = value
                print(request_control(payload)['message'])
            elif sys.stdin.isatty():
                select_registration()
            else:
                raise ServiceError('wrg services discover에서 등록 대상을 선택해 지정하세요.')
        elif action == 'audit':
            records = request_control({'op': 'audit'})
            if getattr(args, 'json', False):
                print(json.dumps(records, ensure_ascii=False, indent=2))
            else:
                print_audit(records)
        elif action == 'logs':
            payload = {'op': 'logs', 'id': identifier or 'opencode-web',
                       'lines': getattr(args, 'lines', 80) or 80}
            print(request_control(payload)['text'])
        elif action == 'restore':
            payload = {'op': 'restore', 'dry_run': getattr(args, 'dry_run', False),
                       'confirmed': getattr(args, 'confirm', False)}
            print(request_control(payload)['message'])
        else:
            result = request_control({'op': 'action', 'id': identifier or 'opencode-web', 'action': action,
                                      'confirmed': getattr(args, 'confirm', False)})
            print(result['message'])
        return 0
    except (EOFError, KeyboardInterrupt):
        print()
        return 130
    except ServiceError as exc:
        print(f'서비스 관리 실패: {exc}', file=sys.stderr)
        return 1


def cmd_web(args: argparse.Namespace) -> int:
    try:
        origin = request_control({'op': 'list'})['origin']
        print(origin)
        return 0
    except ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
