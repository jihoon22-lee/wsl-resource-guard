# 운영 영향 확인과 전체 코드리뷰

## 범위와 결론

- 검토 기준: `fa92f35b5698bccf6301683a9558b5beaa9d4d5c`.
- 운영 배포 기준: `8130fa9f12066be2a52cc62436680a07996f991d`. 이후 차이는 배포 기록 문서이며 제품 코드가 같다.
- Python 구현, 웹 JS/HTML/CSS, 설치·복구, 의존성, CI·릴리스, 설정·사용·운영 문서를 검토했다. 권한/서비스, 감시/알림, 설치/릴리스를 독립 병렬 검토하고 UI도 별도로 검토했다.
- 이번 점검에서 최근 uv/의존성 변경으로 발생한 운영 장애는 확인되지 않았다. 다만 아래 P1 1건, P2 12건을 발견했으므로 결함이 없거나 영향 가능성이 전혀 없다고 판정하지 않는다.
- 문제 코드는 공개 최초 커밋 `14fb15d`에도 존재한다. 공개 이력 이후 uv 업데이트가 새로 만들었다는 근거는 없다. 공개 이전 코드 전체와의 비교는 수행하지 않았으므로 공개 준비 전체의 회귀 여부까지 단정하지 않는다.
- 이 작업은 리뷰와 기록이다. 제품 수정, 설치, 재배포, 서비스 재시작, 운영 프로세스 종료, 외부 알림 발송은 하지 않았다. 공개 준비에는 문서 외에도 권한 경계·설치·PSI 변경이 포함되므로 전체를 단순 문서 정리로 설명하면 부정확하다.

## 현재 운영에서 확인한 사실

- 웹·컨트롤러·시스템 guard 모두 active/running이며 배포 시각 이후 같은 PID, `NRestarts=0`이다.
- loopback health가 정상이다. 실제 Tailscale 웹에서 기존 smoke 검사와 추가 PC/모바일 10개 화면 조회를 통과했다. 설정·경보 화면도 포함하며 추가 검사에서 JS 오류 0, API 오류 0, 변경 HTTP 요청 0이었다.
- 웹 설치 파일 29개와 소스가 바이트 단위로 일치했다. 사용자 guard의 Python 파일 21개도 일치했다. 사용자 guard 설치에는 웹 정적 파일이 포함되지 않는 것이 정상이다.
- 두 설치 스탬프의 commit은 배포 기준과 같고 dirty=false다.
- state/metrics 갱신을 여러 시점에 확인했고 관측 시점의 데이터 나이는 약 4–15초였다. 현재 수집 주기는 15초, 이력 주기는 60초다.
- 점검 시 자원 상태는 warning이었다. 세션 및 MCP 메모리 조건에 따른 경보이며 웹 장애나 기록 중단은 아니었다. 기록된 채널 오류는 없었다. 이것만으로 실제 외부 알림 수신까지 확인한 것은 아니다.
- 전일 이력 약 8.35 MB, 당일 약 2.93 MB로 아래 16 MiB 한계에 아직 도달하지 않았다. 현재 state 경로는 기본값이다.
- 배포 이후 세 유닛의 error 우선순위 로그와 sample/config reload/weekly report 예외·traceback을 확인했으며 해당 오류는 없었다.
- 다른 앱/WSL을 재시작하지 않았다. 이전 배포 전후의 무관한 서비스 PID·Docker 목록 불변 검증은 배포 기록에 있으며, 이번 리뷰에서는 그 삭제된 임시 기준선을 재사용하지 않았다.

## 발견 사항

P1은 먼저 수정할 감시 안정성 문제, P2는 특정 조건에서 실제 기능이 잘못되는 문제다. 실제 운영 사고, fixture 재현, 정적 추론을 구분한다.

### R1 — P1: 알림 구성 오류가 감시 저장까지 중단

- 위치: [notifications.py](../wsl_resource_guard/notifications.py) 92, 185, 248행; [daemon.py](../wsl_resource_guard/daemon.py) 772행.
- Gmail 헤더와 webhook Request 생성이 채널의 try 범위 밖이다. 잘못된 헤더/URL의 ValueError가 Notifier.send 전체를 빠져나가고, 알림 뒤에 수행하는 상태·이력 저장에 도달하지 않는다.
- 격리 재현: 외부 전송을 mock하고 잘못된 Gmail 헤더로 sample을 두 번 실행했다. 예외 2회, 앞선 toast 2회, 뒤의 Discord 0회, save_state 0회였다. 잘못된 IPv6 webhook URL도 요청 생성 단계에서 같은 종류의 예외를 냈다.
- 영향: 설정을 고칠 때까지 저장은 멈추고 앞선 채널의 경보는 반복될 수 있다. daemon 프로세스 자체는 살아 있어 active 표시만으로 놓친다.
- 권고: 메시지/요청 구성부터 채널별 실패 결과로 격리하고, 다른 채널과 수집 저장이 계속되게 한다. 실패 채널을 정상 발송으로 기록하지 않는 회귀 검사도 필요하다.

### R2 — P2: 이력 파일 16 MiB 초과 시 하루 전체 누락

- 위치: [history.py](../wsl_resource_guard/history.py) 55, 306행; [daemon.py](../wsl_resource_guard/daemon.py) 418행.
- writer는 일별 파일에 계속 추가하고 reader는 파일 전체를 16 MiB로 제한한다. 초과 오류는 해당 날짜를 조용히 건너뛰게 한다.
- 재현: 허용된 수집/이력 주기 10초, 표준 세션 5개, 정상 JSONL 8,640건으로 17,314,560 bytes를 만들었다. 원시 이력·집계 이력·세션 이력 모두 0건을 반환했다. 임의 패딩은 사용하지 않았다.
- 영향: 그래프, 세션 이력, 주간 보고서가 같은 날짜를 누락한다. 현재 운영 파일에는 아직 발생하지 않았다.
- 권고: 정규 파일·출력 한도를 유지한 점진적 읽기 또는 파일 분할을 적용하고, 크기 초과를 빈 정상 결과로 숨기지 않는다.

### R3 — P2: root 종료 경로가 PID 재사용을 구분하지 못함

- 위치: [service_control.py](../wsl_resource_guard/service_control.py) 708–724행. CLI의 stop/stop-mcp도 첫 SIGTERM 시점의 동일성 보장을 함께 검토해야 한다.
- UID와 종료 금지 서비스 분류는 snapshot에서 확인하지만 신호는 숫자 PID에 전달한다. snapshot과 os.kill 사이에 종료·PID 재사용이 일어나면 다른 프로세스가 대상이 될 수 있다.
- 합성 재현: snapshot 이후 같은 PID를 다른 시작 시각 및 UID/서비스로 교체한 mock에서 SIGTERM 요청과 성공 응답이 나왔다. snapshot에 이미 다른 UID/서비스인 대조군은 정상 차단됐다.
- 실제 프로세스 생성·신호·PID 재사용 사고를 재현한 것은 아니다. 경쟁 구간의 누락을 확인했으며 공격 가능성이나 발생 확률은 미확인이다.
- 권고: 시작 시각을 보존하고 pidfd로 대상을 고정하여 UID·시작 시각·보호 분류를 검증한 뒤 신호를 보낸다. 단순 조회 후 os.kill만으로는 마지막 경쟁 구간이 남는다.

### R4 — P2: PSI 외 경보가 수집 공백을 지속 관측으로 계산

- 위치: [daemon.py](../wsl_resource_guard/daemon.py) 299, 612, 666행.
- 공백 때 PSI 타이머만 초기화하고 RAM/swap/세션/MCP의 지속·회복 타이머는 유지한다.
- 재현: 낮은 RAM을 t=100과 t=1000에서만 관측해도 30초 연속 조건이 충족된 것으로 경보를 냈다. 정상 RAM을 t=1015와 t=2000에서만 관측한 뒤 120초 연속 정상 확인 없이 회복했다.
- 권고: 모든 지속 판정에서 수집 공백을 제외하고, 기존 활성 경보의 해제는 새 정상 관측 구간으로 확인한다.

### R5 — P2: 수집 주기가 heartbeat 발송 분을 계속 건너뜀

- 위치: [daemon.py](../wsl_resource_guard/daemon.py) 554행.
- 발송 조건이 현재 분과 설정 분의 정확한 일치다.
- 재현: 120초 주기, 매시 0분 발송, 00:01 시작으로 24시간 평가했을 때 예정 발송은 0회였다. 모두 허용 설정이다.
- 권고: 예정 시각 경과와 마지막 완료 슬롯으로 판정하고, 놓친 시각 이후 첫 샘플에서 한 번 처리한다.

### R6 — P2: 푸시 키 생성·구독 갱신 경쟁

- 위치: [service_control.py](../wsl_resource_guard/service_control.py) 293, 311, 318행.
- 키 생성과 구독 목록 read-modify-write 전체에 공유 잠금이 없다.
- 임시 디렉터리와 스레드 barrier 재현: 최초 키 요청 2개가 서로 다른 키를 반환했고 한 키는 저장값과 달랐다. 동시 구독 성공 응답은 2개지만 저장된 구독은 1개였다.
- 권고: 키 생성, 등록, 해제의 전체 작업에 공통 잠금을 적용한다. atomic replace만으로는 갱신 유실을 막지 못한다.

### R7 — P2: 잘못된 EC 키 하나가 나머지 푸시를 중단

- 위치: [webpush.py](../wsl_resource_guard/webpush.py) 77, 144, 195행.
- 키 길이/접두만 확인하고 실제 P-256 공개키 유효성은 등록 단계에서 확인하지 않는다. 발송 중 예외도 구독별로 격리하지 않는다.
- 재현: 길이/접두가 맞지만 곡선상 유효하지 않은 키를 정상 구독 앞에 두면 ValueError로 종료했다. 정상 구독의 전송 시도는 0회였다. 외부 통신은 mock했다.
- 권고: 등록 시 키 파싱 검증과 발송 시 구독별 예외 격리를 모두 적용한다.

### R8 — P2: 서버 푸시 등록 실패를 수신 중으로 표시

- 위치: [app.js](../wsl_resource_guard/web/app.js) 1775, 1781행.
- 브라우저 subscribe 성공 후 서버 등록이 실패하면 로컬 구독이 남는다. 다음 렌더링은 로컬 구독만 보고 수신 중 문구를 표시하며 등록 버튼을 숨긴다.
- 정확한 함수 소스의 격리 DOM mock에서 서버 등록 성공 0, 로컬 구독 존재, 수신 중 표시, 등록 버튼 없음이 동시에 확인됐다.
- 권고: 서버 등록 여부를 확인하고 등록 재시도 또는 로컬 구독 정리를 제공한다.

### R9 — P2: 끊어진 SSE의 옛 경보가 새 조회 결과보다 우선

- 위치: [app.js](../wsl_resource_guard/web/app.js) 1886, 1903, 2511, 2517행.
- 연결 종료/실패에서 liveSummary를 무효화하지 않고 제목·favicon·배지는 이를 우선한다.
- 정확한 함수 소스 mock에서 연결 종료 후 최신 monitor가 normal이어도 이전 critical 제목이 남았다. 이전 normal이 새 critical을 가리는 반대 상황도 가능하다.
- 권고: 연결 상실 시 요약을 무효화하거나 관측 시각으로 최신값을 선택한다. 정상 SSE 연결 상태만 검사하는 것으로는 부족하다.

### R10 — P2: OpenCode 로그 1000줄 옵션 실패

- 위치: [owner_worker.py](../wsl_resource_guard/owner_worker.py) 263행; [service_control.py](../wsl_resource_guard/service_control.py) 1108행; [app.js](../wsl_resource_guard/web/app.js) 2322행.
- UI는 1000줄, 컨트롤러는 2000줄까지 허용하지만 owner worker는 500줄까지만 허용한다.
- 임시 registry/owner와 mock journal 재현에서 500줄은 성공, 1000줄은 Invalid log target으로 전체 요청이 실패했다.
- 권고: 같은 상한을 공유하거나 보조 로그만 별도로 제한한다. OpenCode 통합 경로의 500/501/1000/2000 경계 검사가 필요하다.

### R11 — P2: 사용자/시스템 설치 충돌 검사와 잠금 순서

- 위치: [guard_install.py](../wsl_resource_guard/guard_install.py) 108–115행.
- 시스템 guard 부재 검사 후 owner 잠금을 얻는다. 기다리는 동안 시스템 설치가 완료되어도 다시 검사하지 않는다.
- 임시 fixture에서 잠금 진입 시 시스템 유닛이 생기게 했고 사용자 설치가 계속 호출됨을 확인했다. 실제 설치는 하지 않았다.
- 권고: 잠금 획득 후 충돌 검사를 수행한다. daemon writer 잠금이 중복 기록을 막더라도 잘못된 유닛 활성화/재시작 상태를 정당화하지 않는다.

### R12 — P2: 사용자 지정 state_dir와 시스템 유닛 쓰기 예외 충돌

- 위치: [wsl-resource-guard.service](../packaging/wsl-resource-guard.service) 18–20행; [config.py](../wsl_resource_guard/config.py) 146행; [daemon.py](../wsl_resource_guard/daemon.py) 955행.
- 설정은 다른 state_dir를 허용하지만 ProtectSystem=strict/ProtectHome=read-only의 쓰기 예외는 기본 state/config로 고정된다. HOME 아래 다른 상태 경로는 쓰기 허용을 받지 못한다.
- 코드·유닛 설정으로 확인한 결함이다. 실제 systemd mount namespace/전환 실패 재현은 수행하지 않았다. 현재 운영은 기본 경로여서 이 조건에 해당하지 않는다.
- 권고: 설치 전에 지원 여부를 확인하고 거부하거나, 안전하게 검증한 경로를 유닛에 반영한다. 임의 문자열을 그대로 유닛에 삽입하면 안 된다. 첫 수집 완료까지 확인해야 한다.

### R13 — P2: 릴리스 보안 게이트가 불완전 분석을 허용

- 위치: [security_gate.py](../scripts/security_gate.py) 42–49행; [checks.yml](../.github/workflows/checks.yml) 162행.
- rules/results가 비고 invocation도 없는 SARIF가 통과한다. executionSuccessful=True와 error 수준 toolExecutionNotifications가 함께 있어도 통과한다.
- 두 격리 SARIF가 check_sarif를 통과함을 재현했다. 실행/설정 오류 알림과 긍정적인 분석 완료 증거 검증이 필요하다.
- 실제 직전 전체 CI의 Python/JavaScript SARIF도 내려받아 확인했다. 각각 extension 규칙 43/87개, invocation success=true, 결과 0개, 오류 알림 0개였다. **현재 CI 분석이 비었다는 문제는 확인되지 않았다.** 이 결함은 이후 불완전 결과를 차단하지 못할 가능성이다.
- 실제 CodeQL의 driver.rules는 비고 extension.rules에 규칙이 있으므로 driver.rules만 강제하는 수정은 잘못이다. 전체 tool component를 검증해야 한다.

## uv 구조에 대한 추가 질문

- 현재 전환은 uv pip compile/sync를 사용한 도구 교체다. uv project/uv.lock 전환은 하지 않았다. [기존 범위 기록](2026-10-08-uv-dependencies.md)에 requirements 입력과 해시 잠금을 유지한다고 명시되어 있다.
- web.in/dev.in은 운영/개발 선언, web.txt/dev.txt는 각각 생성한 버전·해시 잠금이다. uv가 이 네 파일을 필수로 요구하는 것은 아니다.
- 현재 독립된 잠금에서 MarkupSafe가 운영 3.0.3, 개발 3.0.4로 다르다. 의존성 충돌이나 실제 런타임 실패는 확인되지 않았지만 개발 검사와 운영 조합이 완전히 같지 않다.
- 장기적으로 pyproject.toml에 용도별 선언을 두고 uv.lock을 단일 잠금 기준으로 쓰는 구성이 더 단순하다. 필요한 requirements는 같은 잠금에서 생성하는 배포/검사 입력으로 취급하고 독립적으로 관리하지 않는 방향을 권장한다.
- 변경 시 설치기의 UID/환경 격리, 최종 경로 venv, wheel/hash 검증, Python 자동 다운로드 금지와 실패 복구를 보존하고 CI·Dependabot을 함께 전환해야 한다. 이번 리뷰에서 파일을 제거하거나 전환을 실행하지 않았다.
- 근거: [uv 프로젝트 구조](https://docs.astral.sh/uv/concepts/projects/layout/), [requirements 잠금 지원](https://docs.astral.sh/uv/pip/compile/).

작은 문서 불일치: [configuration.md](../docs/configuration.md) 82행은 빈 VHD 경로를 자동 탐색한다고 설명하지만 [disks.py](../wsl_resource_guard/disks.py) 87행은 not-configured로 처리한다. 자동 탐색 구현은 찾지 못했다.

## 이번 실행 검증

| 검증 | 결과와 한계 |
| --- | --- |
| 임시 uv 0.12.23/Python 3.14.4, dev 잠금 44개 | 해시 설치·호환성 검사 통과 |
| 전체 단위 CI profile | 345개 통과, 예상 밖 skip 없음 |
| 실제 root owner 격리 | 1개 통과. root-only 비밀 아닌 fixture를 사용했고 자동 삭제 |
| 운영 잠금 8개로 별도 임시 환경 | Gunicorn 26.2.0 TCP/Unix/인증·CSRF 등 transport 3개 통과 |
| 오프라인 실제 브라우저 | 기존 PC/모바일 UI suite 통과, 동작 요청은 fixture 대상 |
| 운영 실제 브라우저 | 기존 smoke 통과 및 PC/모바일 10개 화면 추가 확인, 실제 변경 요청 없음 |
| JS/shell 문법 | app/sw/theme node --check 및 shell bash -n 통과 |
| 의존성 취약점 | web/dev 잠금을 각각 pip-audit로 재검사, 알려진 취약점 발견 없음 |
| 공개 파일/문서 링크 | check_public.py 통과. 정규식 검사로 모든 비밀 부재가 증명되는 것은 아님 |
| 패키징 | 검토 기준 커밋을 두 번 빌드, 바이트 일치·압축본 검증·추출본 CLI help 통과 |
| GitHub 기존 전체 CI | 2a94ba3의 run 37643715613, 12개 job 성공을 현재 API로 재확인. 이번에 다른 SHA의 CI를 새로 실행한 것은 아님 |
| 실제 CodeQL 결과 | 같은 run의 Python 43/JS 87 extension 규칙, 성공 invocation, 결과/오류 알림 0 |

하위 검토자의 39/119/163개 관련 검사는 범위가 겹치므로 345개에 더해 고유 테스트 수로 계산하지 않는다. 최초 pip-audit 실행은 두 잠금을 한 번에 넣어 중복 버전 오류로 중단됐고, CI와 동일하게 각 파일을 독립 실행하여 모두 통과했다.

이번에 수행하지 않은 검증: WSL 신규 설치·재부팅·실제 제거·설치 실패 복구, 실제 서비스/세션 종료, 실제 외부 알림 수신, ARM64 실행, 새 Release 게시. Python 3.11–3.13은 이번 로컬 실행 대상이 아니며 기존 GitHub CI 증거만 확인했다. 통과한 테스트만으로 위 재현된 결함을 해소됐다고 판단하지 않는다.

## 권장 수정 순서와 정리

1. R1 알림 실패 격리와 R3 종료 대상 동일성 보장.
2. R2/R4/R5 이력·연속 관측·예약 발송 정확성.
3. R6–R9 푸시 등록/발송/표시와 SSE 최신성, R10 로그 상한.
4. R11/R12 설치 경합·사용자 지정 경로, R13 보안 게이트.
5. uv 선언/잠금 통합과 문서 정정. 단계별 fixture 검증 후 명시적으로 운영 반영을 구분한다.

리뷰 기록만 작업 브랜치에서 커밋하고 로컬 main에 병합한다. 이 검토에서 제품 코드·운영 설치본을 변경하거나 원격에 리뷰를 게시하지 않는다. 임시 환경, SARIF 다운로드, 스크린샷, 감사 캐시, fixture 서버와 작업 브랜치는 검증 후 정리하며 기존 개발/운영 환경과 사용자 데이터는 보존한다.
