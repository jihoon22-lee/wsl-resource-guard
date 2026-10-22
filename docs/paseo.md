# Paseo 관리

Paseo는 `paseo-daemon.service`라는 시스템 서비스를 설치한 뒤 기존 서비스 등록으로 관리합니다.
실행 계정은 등록 소유자이며 기존 `~/.paseo`의 서버 ID·키·에이전트 상태를 유지합니다.
WRG는 Paseo나 에이전트 CLI를 설치·업데이트하지 않습니다.

## 서비스 준비

`packaging/examples/paseo-daemon.service.in`은 `~/.local/bin/paseo`와 `~/.local/bin/node`의 안정적인 경로를 가정합니다.
다른 위치에 설치했다면 렌더링한 유닛의 `ExecStart`와 `PATH`를 실제 경로로 바꿉니다.
로그인 셸의 임시 경로나 버전 관리자 초기화에 의존하지 않습니다.

저장소에서 일반 사용자로 준비합니다. 이 명령은 실행 중인 서비스에 영향을 주지 않습니다.

```bash
python3 - <<'PY'
import os, pwd
from pathlib import Path
from wsl_resource_guard.guard_install import render_unit
owner = pwd.getpwuid(os.getuid())
destination = Path.home() / '.local/state/wsl-resource-guard/paseo-install'
destination.mkdir(parents=True, exist_ok=True)
unit = destination / 'paseo-daemon.service'
unit.write_text(render_unit(Path('packaging/examples/paseo-daemon.service.in'), owner))
print(unit)
PY
```

`Type=simple`과 `paseo daemon run`으로 foreground 실행을 추적합니다.
`Restart=always`는 종료를 복구하지만 명시적인 `systemctl stop`을 취소하지 않습니다.
5분에 5번 시작하면 재시도 제한에 걸리므로 로그 확인 후 `systemctl reset-failed`가 필요합니다.
`KillMode=control-group` 때문에 중지·재시작은 하위 에이전트와 터미널에도 영향을 줍니다.
메모리는 전체 cgroup 기준으로 측정하며 하드 제한은 초기값으로 설정하지 않습니다.

## 연결을 보존하는 적용 순서

1. 기존 데몬의 home, 서버 ID, supervisor PID와 실행 주체를 확인합니다. 데스크톱 관리 데몬은 이 절차로 전환하지 않습니다.
2. 검증한 WRG 소스에서 기존 `install-services.sh`로 웹·컨트롤러를 업데이트합니다. 이 작업은 Paseo를 중지하지 않습니다. 설치 권한과 복구 백업은 [설치 안내](installation.md)를 따릅니다.
3. 준비한 유닛을 검토하고 `/etc/systemd/system/paseo-daemon.service`에 설치한 뒤 `systemctl daemon-reload`만 실행합니다. 기존 동명 시스템 유닛이 있으면 덮어쓰기 전에 백업·비교합니다. **이 단계에서는 start 또는 enable --now를 실행하지 않습니다.**
4. 진행 중인 작업이 끝나고 재연결 가능한 시점에 기존 실행 주체를 중지하고 시스템 서비스를 시작합니다. 사용자 서비스에서 시스템 서비스로의 전환은 무중단이 아닙니다. 관리자 인증은 기존 데몬을 중지하기 전에 완료합니다.
5. 기존 서버 ID가 유지되고 로컬 및 릴레이 요청이 성공한 뒤 등록합니다.

```bash
wrg services register systemd:paseo-daemon.service --name Paseo
wrg services autostart-on paseo-daemon
wrg services status paseo-daemon
```

등록은 실행 상태를 바꾸지 않습니다. 부팅 시작은 systemd가 담당합니다.
`wrg services restore`는 기존 Compose 복구 기능이며 Paseo를 별도로 실행하지 않습니다.
시스템 서비스 시작 실패 시 먼저 시스템 유닛을 중지하고 이전 실행 방식으로 복구합니다.
서버 ID/키를 새로 만들거나 두 데몬을 동시에 실행해서 우회하지 않습니다.
모바일 재접속까지 확인한 후 기존 임시 서비스 자료를 정리합니다.

## 연결 검사

등록된 시스템 유닛이 active일 때 약 60초마다 별도 스레드에서 검사합니다.
검사는 서비스 소유자 권한으로 실행하며 다음 결과만 반환합니다.

| 표시 | 확인 대상 |
|---|---|
| 실행 중 | systemd 상태. 단독으로 연결 성공을 뜻하지 않음 |
| HTTP | 로컬 `/api/health` 응답 |
| 로컬 | 인증된 WebSocket 연결과 ping/pong |
| 릴레이 | 릴레이를 통한 암호화 연결과 ping/pong |

정상·실패·미확인·릴레이 꺼짐을 구분합니다. 릴레이 꺼짐은 원격 연결 성공이 아닙니다.
180초 넘게 갱신되지 않은 결과와 서비스 재시작 전 결과는 정상으로 표시하지 않습니다.
자동 재시작 횟수는 systemd의 현재 실행 이력이며 관리 창에서 확인합니다.
검사는 재시작·설정 변경·대화 전송·알림 전송을 하지 않으며 기존 CLI/모바일과 다른 클라이언트 ID를 사용합니다.
접속 제안 URL·인증정보·대화 내용·원본 SDK 오류는 웹이나 검사 출력으로 전달하지 않습니다.

검사 전체 제한은 15초이며 외부 실행 제한은 17초입니다. 로컬 연결은 최대 5초,
릴레이 연결은 남은 전체 시간 안에서 최대 10초를 사용합니다.
실패는 서비스 요약에 표시되며 자동 재시작이나 푸시 알림을 유발하지 않습니다.
Paseo의 네트워크 재연결과 systemd의 프로세스 재시작 정책을 사용합니다.

## 지원 범위와 검증

현재 검사는 npm Paseo **0.12.x**의 client API, 기본 `~/.paseo`, loopback TCP listen을 대상으로 합니다.
`~/.local/bin/node` 또는 시스템 Node와 그 설치 prefix의 Paseo CLI를 찾습니다.
별도 home·Unix socket·컨테이너·변경된 npm API는 지원이 확인되기 전까지 미확인으로 표시합니다.
내부 client API가 변경되면 어댑터 검증이 필요합니다.
릴레이 왕복 성공은 휴대폰 네트워크·앱 버전·저장된 접속 정보까지 검증하지 않습니다.
실제 모바일 재접속과 WSL 재부팅은 별도 운영 검증입니다.

```bash
.venv/bin/python -m unittest discover -s tests -q
node --check wsl_resource_guard/web/app.js
.venv/bin/python tests/browser_offline.py
.venv/bin/python tests/integration_paseo_service.py
```

마지막 검사는 임의 이름의 일회용 사용자 서비스를 사용합니다. 실제 Paseo나 WSL을 재시작하지 않습니다.
공식 실행 설정: https://paseo.sh/docs/configuration.md
