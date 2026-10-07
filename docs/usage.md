# 사용법

```bash
wrg status
wrg top
wrg sessions --projects
wrg sessions --stale
wrg mcp
wrg disks
wrg history --hours 24
wrg history --episodes
wrg weekly-report  # 미리보기; --send는 실제 발송
```

웹의 개요는 최신 자원·경보·서비스 상태를 보여 줍니다. 세션·MCP·이력·디스크 화면은 같은 수집 데이터를 사용합니다.
수집 실패와 오래된 관측, 실제 숫자 0을 구분해서 확인하세요. PC의 그래프 hover와 모바일 tap으로 상세 값을 볼 수 있습니다.

## 서비스 관리

`wrg services` 대화형 메뉴 또는 웹에서 현재 발견되는 systemd·Compose 서비스를 등록합니다.
기존 서비스의 프로그램·환경 파일·DB를 가져와 재배포하지 않습니다.

- 시작·중지·재시작: 현재 실행 상태 변경
- 자동 실행 켜기·끄기: 부팅 시 동작 설정
- 사용·미사용: 실행과 자동 실행을 함께 조정
- 등록 삭제: Resource Guard 관리 목록에서 제거. 외부 앱 데이터는 보존

기반 서비스 변경은 다른 앱에 영향을 줄 수 있습니다. Tailscale 연결을 끊는 연산은 웹에서 제한합니다.
oneshot 서비스의 `active (exited)`는 실행 프로세스가 계속 존재한다는 뜻이 아닙니다.
추가 앱 유닛은 packaging/examples의 템플릿을 참고하되 설치 경로와 앱 자체 보안 정책을 직접 확인합니다.

## 세션 종료

```bash
wrg stop <pid> --confirm
wrg stop-mcp <pid> --confirm
```

PID·시작 시각·소유자·대상 분류를 다시 검증합니다. systemd가 관리하는 서비스는 세션 종료 대신 서비스 관리에서 조작합니다.
웹 일괄 종료도 실행 직전에 대상을 다시 확인합니다. 표시된 후보가 실제로 불필요한지 사용자가 판단해야 합니다.

## 알림과 진단

`wrg configure-alerts`로 선택한 외부 채널을 설정하고 `wrg test-alert --channels <channel>`로 명시적 수신 시험을 합니다.
Web Push는 해당 브라우저의 권한과 시스템 Python cryptography가 필요합니다.
`wrg doctor`는 설치 사본·의존성·WSL 조건 진단에 사용합니다.
