# 전체 코드리뷰 후속 개선 실행 계획

> **For agentic workers:** 구현 시 `superpowers:executing-plans`를 적용해 아래 단위를 순서대로 수행한다. 병렬 검토자는 읽기 전용으로 사용하고 같은 파일을 동시에 편집하지 않는다. 각 체크박스는 실행 증거가 있을 때만 완료 표시한다.

**Goal:** 리뷰 R1–R13, uv 선언·잠금 통합, VHD 문서 불일치를 해결하고 검증된 최종 코드를 기존 운영 구조에 적용한다.

**Architecture:** 기존 단일 개발 경로, 사용자 guard/CLI와 root 웹·컨트롤러 설치 구조를 유지한다. 감시 저장·프로세스 종료 경계를 먼저 고친 뒤 데이터·알림·UI·설치·보안 판정을 정리한다. 의존성 구조는 마지막에 단일 uv 잠금으로 통합하고 최종 검증 후 운영에 반영한다.

**Tech Stack:** Python 3.11–3.14, systemd/WSL2, Flask/Gunicorn, JavaScript, Playwright, uv 0.12.23, GitHub Actions/CodeQL.

**Spec:** [전체 리뷰와 재현 근거](2026-10-08-full-code-review.md). 계획 기준 HEAD는 `5afe11d`이며 운영 설치 기준은 `8130fa9`이다. 구현 시작 시 현재 상태를 다시 확인한다.

## 공통 제약

- 개발 경로는 `~/projects/wsl-resource-guard` 하나다. `-public`, 별도 clone, 상시 worktree, 과거 저장소 보관본을 만들지 않는다. 같은 체크아웃에서 편집은 한 작업자만 수행한다. 다른 세션의 변경이 있으면 그 소유를 확인하고 병합 순서를 조정한다.
- 현재 운영 설치 경로와 서비스 계정은 유지한다. 개발 중인 파일을 운영 경로에 수동 복사하지 않는다. 다른 앱, Docker 컨테이너, WSL 전체를 재시작하지 않는다.
- 코드는 작업 브랜치에서 수정한다. 단위별 재현 검사 → 최소 수정 → 관련 검사 → 독립 검토 → 커밋 순서다. 통합 검증 후 main에 `--no-ff` 병합하고 승인된 원격 push·운영 반영까지 별도로 확인한다.
- 테스트는 임시 fixture에서 수행한다. 실제 사용자 세션 종료, 외부 수신자 알림, 운영 데이터 손상·의도적 설치 실패를 검증 수단으로 사용하지 않는다.
- 사용자 설정·비밀값·이력과 root 등록 정보·접근 정책을 보존한다. owner 데이터 접근은 credential drop, 정규 파일 검사, 제한된 연산·출력·시간 정책을 유지한다.
- 실행 결과는 이 문서 하나에 누적한다. 이전 리뷰는 당시 사실의 기록으로 유지한다. 새 설명은 현재 사용 문서에 반영하고 오래된 문서를 계속 추가하지 않는다.
- 구현 단계마다 운영을 재배포하지 않는다. 최종 통합 검증 뒤 한 배포 작업으로 적용한다. 현재 구조의 자체 서비스 재시작에 따른 짧은 연결/수집 공백은 측정하며 무중단을 보증하지 않는다.
- GitHub Release 발행은 이번 수정의 필수 결과가 아니다. 태그를 새로 만들지 않아도 릴리스 차단·패키징 경로는 검증한다. 기존 태그 재지정·강제 push는 하지 않는다.

## 우선순위와 범위 추적

| 단계 | 대상 | 결과 |
| --- | --- | --- |
| 0 | 기준선 | 운영·Git·의존성·데이터 불변 조건 기록 |
| 1 | R1 | 알림 실패가 다른 채널·감시 저장으로 전파되지 않음 |
| 2 | R3 | 웹/CLI 종료가 검증한 프로세스에만 적용됨 |
| 3 | R2, R4, R5 | 큰 이력·수집 공백·예약 시각을 정확히 처리 |
| 4 | R6, R7, R8 | 푸시 키·구독·발송·화면 상태 일관성 |
| 5 | R9, R10 | 최신 경보 표시와 로그 상한 일치 |
| 6 | R11, R12 | 설치 경합·상태 경로·실행 준비·복구 보장 |
| 7 | R13 | 불완전 보안 분석과 차단 결과를 릴리스에서 거부 |
| 8 | uv | pyproject.toml + uv.lock, CI/설치/Dependabot 일괄 전환 |
| 9 | 문서·통합 | 모든 항목 교차 검증, 문서 정정, 공개 파일 검사 |
| 10 | 운영·정리 | 최종 코드 배포, 관측, 불필요한 작업 산출물 제거 |

## 추가 교차 검토 초점

1. 이력의 파일 크기 문제를 고친 뒤 owner worker 출력 16 MiB/timeout에서 다시 실패하지 않는가: 단계 3에서 실제 worker 왕복을 검사한다.
2. 프로세스 종료 안전성을 SIGTERM만 고치고 CLI SIGKILL·자식 프로세스에 누락하지 않는가: 단계 2에서 모든 경로를 검사한다.
3. 푸시 잠금 도입이 `push_subscribe → push_key` 중첩 호출을 교착시키지 않는가: 단계 4에서 병렬 등록·해제·최초 생성까지 검사한다.
4. 사용자 지정 경로가 설치 준비·유닛·잠금·스냅샷·복구에서 같은 위치로 해석되는가: 단계 6에서 각각 다른 경로를 사용하도록 하는 회귀를 막는다.
5. uv 전환이 사용자 설정/인덱스를 다시 root 설치에 상속하거나 잠금을 묵시적으로 갱신하지 않는가: 단계 8에서 오염 환경·변조 잠금·소스 의존성으로 실패 전 상태 보존을 검사한다.

## 단계 0 — 기준선과 작업 준비

**파일:** 이 기록, 기존 `AGENTS.md`, 설치·검증 스크립트 읽기. 제품 수정 없음.

- [x] `git status --short`, 현재 브랜치·HEAD·origin/main·작업 지침을 확인하고 `fix/review-remediation` 브랜치를 만든다.
- [x] 실제 설치 스탬프, 코드 해시, 유닛 내용/환경, active/enabled/PID, guard state 갱신 시각, history 크기, 사용자 설정·registry·웹 접근 정책의 해시를 기록한다. 비밀값·개인 호스트명은 공개 기록에 넣지 않는다.
- [x] 무관한 서비스 PID·Docker 실행 목록을 임시 메모리 또는 권한 제한된 한 임시 디렉터리에 기록한다. 프로젝트 전체를 복사하지 않는다.
- [x] 기존 잠금 기반 테스트 환경을 검증한다. 운영 환경은 그대로 두고 이후 단위별 검사를 수행한다.

```bash
git status --short
git branch --show-current
git log -3 --oneline
git switch -c fix/review-remediation
```

**완료 조건:** 어떤 파일/서비스가 바뀌어도 되는지와 비교 기준이 명확하다. 기존 미커밋 변경을 포함시키지 않는다.

## 단계 1 — R1 알림 실패 격리

**수정:** `wsl_resource_guard/notifications.py`, `daemon.py`.
**검사:** `tests/test_notifications.py`, `test_daemon.py`, `test_reporting.py`.
**인터페이스:** `Notifier.send(...) -> list[NotificationResult]` 유지. 실패한 채널도 `sent=False`, `skipped=False` 결과를 남기고 다음 채널을 실행한다.

- [x] 잘못된 Gmail 헤더, webhook URL, 인코딩/메시지 생성 오류를 각각 넣고 두 연속 sample에서 다른 채널·state·history가 누락되는 기존 실패를 재현한다. 외부 전송은 전부 mock한다.
- [x] 메시지·Request 구성부터 전송까지 채널 경계 안으로 옮긴다. send의 채널별 방어 경계는 `Exception`을 실패 결과로 변환하되 종료 신호/KeyboardInterrupt 같은 `BaseException`은 삼키지 않는다. 상세 오류에 비밀 주소·토큰을 노출하지 않는다.
- [x] 실패/시도/성공/미설정 상태를 구분한다. 실패했다고 발송 성공으로 표시하지 않으며, 정상 수집은 계속 저장하고 매 샘플 중복 발송을 막는 기존 재시도 간격을 보존한다.
- [x] 회귀 검사 통과 후 `fix: Isolate notification failures from monitoring persistence`로 커밋한다.

핵심 검사 형태:
```python
results = notifier.send("fixture", "body", "warning")
assert any(r.channel == "gmail" and not r.sent and not r.skipped for r in results)
assert any(r.channel == "discord" and r.sent for r in results)
# 같은 mock 채널을 sample 두 번에 연결해 state/history 갱신과 중복 방지를 함께 단언한다.
```

## 단계 2 — R3 프로세스 동일성과 종료 경계

**수정:** `processes.py`, `service_control.py`, `cli.py`.
**검사:** `tests/test_processes.py`, `test_service_control.py`, `test_cli.py`.
**인터페이스:** `ProcessInfo`에 호환 기본값을 가진 시작 시각 식별자를 추가한다. 종료 공통 헬퍼 `signal_verified_process(process, expected_uid, sig) -> bool`을 `processes.py`에 두고 사라진 대상은 False, 불일치/미지원은 명확한 오류로 구분한다.

- [x] snapshot 뒤 PID·UID·시작 시각·cgroup이 바뀌는 경우, 자식 교체, 이미 종료, 자기 자신/보호 서비스, CLI 강제 종료를 각각 검사한다. syscall은 mock한다.
- [x] snapshot 생성에서 `/proc` 시작 시각을 보존하고 읽는 사이 프로세스가 바뀐 불일치 항목은 제외한다.
- [x] pidfd를 먼저 열고 현재 시작 시각·UID·보호 분류를 재검증한 후 `signal.pidfd_send_signal`로 보낸다. fd는 모든 종료 경로에서 닫는다. 지원하지 않는 환경에서 숫자 PID 신호로 조용히 후퇴하지 않고 종료 기능만 명확히 거부한다.
- [x] 단일 세션/MCP, 오래된 세션 일괄 종료, CLI `stop`, `stop-mcp`, `--kill`의 TERM/KILL 모두 같은 경계를 적용한다. 화면/CLI의 안전성 설명을 실제 보장과 맞춘다.
- [x] 실제 신호 검증은 자신이 생성한 폐기 가능한 프로세스에만 적용한다. 자원 감시 및 무관한 프로세스 PID는 그대로인지 확인한다.
- [x] `fix: Bind process termination to verified process identities`로 커밋한다.

핵심 호출 형태:
```python
fd = os.pidfd_open(process.pid)
try:
    # 여기서 현재 UID/start_ticks/보호 분류를 snapshot과 대조한 뒤에만 신호를 보낸다.
    signal.pidfd_send_signal(fd, sig)
finally:
    os.close(fd)
```

## 단계 3 — R2/R4/R5 이력과 시간 판정

**수정:** `safe_read.py`, `history.py`, `daemon.py`, `reporting.py`, `cli.py`, 필요 시 `owner_worker.py`의 오류 전달.
**검사:** `tests/test_history.py`, `test_daemon.py`, `test_schedule.py`, `test_psi.py`, `test_reporting.py`, `test_owner_worker.py`, `test_cli.py`.

- [x] R2: 기존 일별 JSONL 형식을 유지한다. `iter_text_lines(path, *, max_line_bytes, deadline)`를 추가해 권한을 낮춘 뒤 O_NONBLOCK으로 열고 fstat 정규 파일 확인 후 제한된 청크로 읽는다. 한 줄 한도는 1 MiB로 두며 초과/시간 초과는 명시적인 읽기 실패다. 파일 전체를 `read_text().splitlines()`로 메모리에 올리지 않는다.
- [x] 원시/집계 이력, 세션/프로젝트 귀속, 주간 보고서와 CLI history가 공통 iterator를 쓰게 한다. 집계는 읽는 중 누적하고 최종 응답 한도·worker 제한을 유지한다. 보고서·경보 집계는 전체 원시 목록을 만들지 않으며 CLI는 요청한 limit만 보관한다. 원시 목록 호환 호출에도 레코드/출력 예산을 두고 초과 시 명시적으로 실패한다. 불완전 읽기를 정상적인 빈 이력으로 반환하지 않는다. 캐시 갱신은 파일 교체·추가 기록을 구분한다.
- [x] 16 MiB 아래/같음/위, 리뷰의 8,640개 정상 레코드, 여러 날짜, 잘린 마지막 줄, 비객체 JSON, 큰 한 줄, FIFO/장치/심볼릭 링크, 읽는 중 추가 기록을 검사한다. 실제 owner worker를 거친 응답에서 날짜·집계·세션이 보존되는지 확인한다.
- [x] R4: 성공한 관측 간격이 `max(1, 2 * interval_seconds)`를 넘거나 시계가 역행하면 모든 지속 조건 타이머를 재설정한다. 이미 활성인 경보는 유지하고, 회복은 새 정상 관측 구간으로만 확인한다. PSI의 null/정상 0 구분과 독립 회복을 보존한다.
- [x] RAM/swap/세션/MCP/디스크 각각 수집 실패·재시작·설정 주기 변경·시계 역행을 시험한다. 실패 중 위험/정상 상태가 새로 입증된 것으로 처리하지 않는다.
- [x] R5: 현재 시각 이전의 가장 최근 예정 슬롯을 계산하고 마지막 처리 슬롯보다 새로울 때 한 번 보낸다. 장기 중단 후 과거 메일을 여러 통 한꺼번에 보내지 않는다. 발송 시도 기록과 성공 기록은 구분한다.
- [x] 5/15/60/120/600초 주기, 발송 분 건너뜀, 정확한 경계, 시간대/DST 변화, 미래 last-slot, 재시작과 실패 후 중복을 시험한다. 기존 저장 슬롯은 계속 읽고 새 비교는 예정 시각의 UTC epoch 기준으로 정규화한다. 미래로 잘못 저장된 슬롯은 경고와 함께 현재 스케줄에서 다시 판정하고, 이미 처리한 동일 epoch 슬롯은 중복 발송하지 않는다.
- [x] R2와 R4/R5는 검증된 의미 단위로 나눠 커밋한다: `fix: Stream history without dropping oversized days`, `fix: Exclude observation gaps and catch due heartbeat slots`.

핵심 수용 단언:
```python
assert len(read_history(state_path, since)) == 8640
assert email_heartbeat_due(now_after_scheduled_minute, previous_slot, settings)
assert not email_heartbeat_due(now_after_scheduled_minute, handled_slot, settings)
```

## 단계 4 — R6/R7/R8 푸시 일관성

**수정:** `service_control.py`, `webpush.py`, `webapp.py`, `web/app.js`.
**검사:** `tests/test_service_control.py`, `test_webpush.py`, `test_webapp.py`, `browser_offline.py`.

- [ ] 키 생성·등록·해제 전체에 같은 root 공유 잠금을 적용한다. public 메서드가 잠금을 잡고 내부 `_locked` 헬퍼가 재획득 없이 실행하도록 하여 중첩 호출 교착을 피한다. 키를 요청마다 재생성하지 않는다.
- [ ] barrier 기반 동시 최초 키 요청은 모두 같은 키, N개 동시 등록은 N개 보존, 등록/해제 경합과 실패 후 잠금 해제까지 검사한다.
- [ ] 등록 시 `EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)`로 유효성을 검사한다. 기존 저장 데이터에 잘못된 항목이 있어도 구독별 실패 결과를 남기고 뒤의 정상 구독 발송을 계속한다. 기존 정상 VAPID 키·구독은 보존한다.
- [ ] 브라우저 로컬 구독만으로 수신 중이라 표시하지 않는다. 기존 allowlist와 CSRF를 유지한 고정 연산으로 해당 endpoint의 서버 등록 상태를 확인하고 다른 구독 목록/비밀은 반환하지 않는다. endpoint는 query string/접근 로그에 넣지 않는다.
- [ ] 서버 등록 실패 시 '등록 미완료/다시 등록' 상태로 전환한다. 기존 로컬 구독을 재사용해 등록을 재시도하며 자동 외부 재등록/발송은 하지 않는다. 실제 Push 수신 성공과 등록됨 문구도 구분한다.
- [ ] PC/모바일에서 서버 실패→새로고침→재등록, 서버만 삭제된 구독, 로컬만 삭제된 구독, 권한 거부·지원 불가·키 변경을 fixture로 검사한다.
- [ ] `fix: Preserve push subscriptions and report registration truthfully`로 커밋한다.

```python
ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key_bytes)
assert len(saved_subscriptions) == successful_distinct_registrations
assert normal_endpoint_attempts == 1  # 앞선 잘못된 구독이 막지 못함
```

## 단계 5 — R9/R10 화면 최신성과 로그 계약

**수정:** `web/app.js`, `owner_worker.py`, `service_control.py`, 필요 시 `webapp.py`.
**검사:** `tests/browser_offline.py`, `test_owner_worker.py`, `test_service_control.py`, `test_webapp.py`.

- [ ] SSE 오류/종료/탭 숨김 때 기존 liveSummary를 무효화한다. 모든 연결 상태에서 수신 순서만 믿지 않고 관측 시각으로 polling/SSE의 최신성을 판단한다. 연결 실패 시 새 polling 값으로 제목·favicon·배지가 회복되어야 한다.
- [ ] critical→연결 실패→normal과 반대 순서, polling/SSE 응답 역전, 재연결, 오래된 데이터, 숨김 탭 복귀를 검사한다. 오래된 정상값을 현재 정상으로 표시하지 않는다.
- [ ] 로그 최대 줄 수를 2000으로 통일한다. 컨트롤러의 고정 연산과 worker가 같은 상수를 사용하며 정규 파일·byte/output/time 상한은 유지한다. UI의 50/200/500/1000 옵션은 그대로 동작한다.
- [ ] OpenCode의 500/501/1000/2000, API 상한 초과·비정수, 보조 로그 부재/권한 거부와 journal 정상 결과 조합을 검사한다. 보조 로그 실패를 숨기지 않고 부분 실패 표시와 정상 journal 내용을 함께 전달한다.
- [ ] 독립된 두 커밋으로 정리한다: `fix: Prefer fresh observations after stream failures`, `fix: Align service and owner log limits`.

```javascript
// error/disconnect 공통 처리 후 polling 결과를 다시 표시한다.
liveSummary = null;
updateAlertBadge();
updateBadges();
```

## 단계 6 — R11/R12 설치·복구와 상태 경로

**수정:** `guard_install.py`, `installer.py`, `packaging/wsl-resource-guard.service`, 필요한 owner 고정 연산 및 설정 검사.
**검사:** `tests/test_guard_install.py`, `test_installer.py`, `test_owner_worker.py`와 새 격리 systemd 수명주기 시험.

- [ ] owner 잠금 획득 후 owner/origin/설치 모드 충돌을 다시 검사한다. root/owner 잠금 획득 순서를 모든 설치 경로에서 통일하고 교착·대기 timeout을 검사한다.
- [ ] 사용자 권한에서 유효 설정의 state_dir를 읽고 기존 상대 경로 해석은 명확히 거부하거나 기존 HOME 기준 절대 경로로 정규화한다. 시스템 모드의 새 지원 범위는 소유자 HOME 안의 정규 디렉터리로 제한하고, HOME 밖·보호 경로·링크로 이탈하는 값은 서비스 변경 전에 명시적으로 거부한다. 사용자 guard의 기존 경로 동작은 보존한다.
- [ ] 사용자 경로 생성/쓰기 검사도 소유자 UID·기본 GID로 수행한다. systemd unit의 경로 quoting/escaping을 중앙화하고 제어문자·개행·specifier 주입을 거부한다. root가 사용자 경로를 따라 chown/덮어쓰지 않는다.
- [ ] 사용자 지정 상태 경로를 daemon lock, snapshot_state/restore_state, 첫 실행 확인 모두에 전달한다. 설치 중 설정 파일 변경을 감지하면 전환 전에 중단한다. guard의 실행 중 state_dir 변경은 자동 적용하지 않고 설치 재생성 필요 상태로 명시한다.
- [ ] 단순 is-active 뒤 성공을 반환하지 않는다. 새 프로세스 시작 이후 올바른 state 경로의 첫 성공 수집을 최대 180초 내 확인하고, 실패 시 이번 작업의 코드·유닛·active/enabled·데이터만 복구한다. 경보 severity가 normal이어야 한다는 조건은 두지 않는다.
- [ ] 격리된 systemd 환경에서 기본/사용자 지정 경로 신규 설치, 사용자→시스템 전환, 반대 순서 거부, 설치 경쟁, 권한 오류, 최초 수집 실패, 롤백 후 기존 guard 수집을 시험한다. 운영 WSL에 fixture 유닛을 설치하거나 별도 WSL 배포판을 기동하지 않는다. GitHub-hosted disposable VM 등 독립된 환경에서 실제 namespace 시험을 수행한다.
- [ ] `fix: Serialize guard installation and validate writable state paths`로 커밋한다.

```python
with owner_install_lock(owner):
    if not system:
        ensure_no_system_guard()
    # 동일 잠금 안에서 경로/설정 사전 검사 후 기존 설치 적용 함수를 호출한다.
```

## 단계 7 — R13 CodeQL·릴리스 차단

**수정:** `scripts/security_gate.py`, `.github/workflows/checks.yml`, `release.yml`.
**검사:** `tests/test_release_gate.py`, `test_release.py`, 워크플로 정적 검사.

- [ ] Python/JavaScript 각각 예상 SARIF가 존재하고 올바른 버전/run/CodeQL 도구 식별/성공 invocation을 가진 경우만 허용한다. invocation 누락·실패·불명확 상태는 거부한다.
- [ ] driver와 extensions 전체에서 규칙 목록·결과의 rule reference를 검증한다. 규칙 0건, 실행/설정 알림의 error, 결과 error 및 High/Critical, 비정상 score를 차단한다. 실제 extension-only 결과는 허용한다.
- [ ] 분석의 예상 언어와 검사 commit을 워크플로에서 고정하고 생성 결과를 그 실행의 산출물로 검사한다. 다른 커밋이나 임의 이전 artifact를 가져와 통과시키지 않는다.
- [ ] 리뷰의 빈 SARIF, invocation 성공+오류 알림, 한 언어 누락, extension-only 정상 결과, 낮은 등급 결과, rule reference 손상을 모두 검사한다. 43/87처럼 특정 규칙 개수를 영구 상수로 고정하지 않는다.
- [ ] 실패 조건이면 publish job이 실행되지 않음을 disposable 검증으로 확인한다. 공개 Release를 일부러 생성/삭제하는 시험은 하지 않는다.
- [ ] `fix: Require complete CodeQL evidence before release`로 커밋한다.

```python
assert all(i.get("executionSuccessful") is True for i in invocations)
assert invocations and rules_across_all_components
assert not any(n.get("level") == "error" for n in analysis_notifications)
```

## 단계 8 — uv 프로젝트 방식으로 완전 통합

**수정:** `pyproject.toml`, 신규 `uv.lock`, `uv_environment.py`, `service_install.py`, CI 4개 workflow, `.github/dependabot.yml`, `scripts/release.py`, `scripts/ci_unit.py`, 관련 설치·패키징 시험과 `tests/uv_install_smoke.py`.
**제거:** `requirements-web.in`, `requirements-web.txt`, `requirements-dev.in`, `requirements-dev.txt`.

- [ ] CLI/guard의 기본 dependencies=[]는 유지하고 웹은 `project.optional-dependencies.web`, 검사 도구는 `dependency-groups.dev`에 선언한다. cryptography는 개발 시험에 포함하고 현재 시스템 Python을 사용하는 guard/컨트롤러의 선택적 의존성 설치 구조는 유지한다. 시스템 cryptography 검증은 웹 venv 검사와 별도로 남긴다.
- [ ] 현재 승인 버전을 제약으로 가져와 uv.lock을 생성한다. 운영/개발 공통 버전을 일치시키고 불필요한 일괄 업그레이드를 하지 않는다. MarkupSafe 차이는 운영 3.0.3을 초기 공통 기준으로 삼아 회귀·취약점 검사를 통과시킨다. 호환상 필요한 변경만 별도로 설명한다.
- [ ] 개발/CI는 `uv sync --locked --extra web --group dev --no-build --no-install-project`로 동기화한다. 실행에서 잠금을 묵시적으로 바꾸지 않게 한다. Python 3.11–3.14 각각 웹 공통 패키지 버전이 운영 profile과 같은지 검사한다.
- [ ] 설치기는 검증한 uv와 root 소유 임시 manifest/lock으로 `uv sync --locked --extra web --no-dev --no-install-project --no-build --python /usr/bin/python3`를 실행한다. `UV_PROJECT_ENVIRONMENT`는 설치기만 정한 기존 `.venvs/<install-id>` 최종 경로에 연결한다. 환경을 만든 뒤 이동하지 않는다.
- [ ] env whitelist, 명시적 CA/TLS, 사용자 인덱스·프록시·설정 무시, Python 자동 다운로드 금지, 고정 PyPI 출처와 artifact 해시 확인을 보존한다. 프로젝트 sources/workspace/path/VCS/build hook을 이 root 설치 경로에서 허용하지 않는다. corrupt lock/artifact가 반드시 실패하는 시험을 둔다.
- [ ] pip-audit에는 같은 uv.lock에서 web/dev profile을 각각 해시 포함 임시 requirements로 export한다. 출력은 임시 검사 입력이며 Git에 추적하거나 수동 관리하지 않는다. 각 profile을 독립 감사하고 공통 버전 일치를 검사한다.
- [ ] Dependabot의 pip 항목을 `package-ecosystem: uv`로 변경하고 Actions 항목을 유지한다. 실제 봇이 생성하는 lock 변경을 `uv lock --check`와 전체 CI로 검증한다. 봇 실행 전에는 실제 봇 갱신 확인 완료라고 쓰지 않는다.
- [ ] release allowlist/필수 파일을 pyproject+uv.lock 기준으로 갱신한다. Git 없는 압축본에서 uv 동기화·설치 준비가 동작하고 낡은 requirements 참조가 남지 않는지 확인한다.
- [ ] manifest/lock 불일치, wheel 부재, hash 변조, 다운로드 실패, 오염 환경, 웹 UID 접근 실패에서 기존 운영 설치·유닛이 바뀌지 않는지 검사한다.
- [ ] 개발 잠금 통합과 설치/CI/패키징 연결을 검증 가능한 커밋으로 나누되, main에는 모든 전환이 끝난 상태로 통합한다.

선언 구조:
```toml
[project.optional-dependencies]
web = ["Flask>=3.1,<4", "gunicorn>=26.2.0,<27"]

[dependency-groups]
dev = ["playwright==1.63.0", "cryptography>=50.0.2,<51", "pip-audit>=2.9,<3", "PyYAML>=6,<7"]

[tool.uv]
package = false
```

검사 명령의 기준(설치기에서는 검증된 uv 절대 경로·제한 환경을 사용):
```bash
uv lock --check
uv sync --locked --extra web --group dev --no-build --no-install-project
uv run --locked --no-sync python scripts/ci_unit.py
```

근거: [uv 전환 안내](https://docs.astral.sh/uv/guides/migration/pip-to-project/), [잠금과 sync](https://docs.astral.sh/uv/concepts/projects/sync/), [uv Dependabot](https://docs.astral.sh/uv/guides/integration/dependabot/). [GitHub 지원표](https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories)의 uv 버전 표기와 프로젝트의 0.12.23 간 호환성은 실제 봇 결과로 확인하며, 필요 없이 uv를 낮추거나 pip 잠금을 다시 이중 관리하지 않는다.

## 단계 9 — 문서와 최종 통합 검증

**수정:** README, CONTRIBUTING, CHANGELOG, `docs/installation.md`, `configuration.md`, `architecture.md`, `operations.md`, `usage.md`, 이 기록. 새 문서 묶음을 추가하지 않는다.

- [ ] VHD 경로 빈 값은 자동 탐색이 아니라 not-configured라고 정정한다. 이번 범위에서 자동 탐색 기능을 새로 만들지 않는다.
- [ ] pidfd 미지원 시 종료 제한, 수집 공백/heartbeat 의미, 큰 이력 오류, 푸시 등록/수신 차이, 시스템 state_dir 지원 범위·재설치 조건, uv 사용법과 복구 절차를 구현과 맞춘다.
- [ ] 기존 실패 재현을 모두 회귀 검사로 남기고 R1–R13 각각 구현 commit·관련 테스트·실제 확인/미확인 상태를 이 문서에 연결한다.
- [ ] 최종 main 후보에서 Python 3.11–3.14 전체 검사, JS/shell/workflow 검사, root fixture, 실제 systemd 격리 설치/복구, web-only transport, 고정 Playwright PC/모바일 오류 시나리오를 실행한다. 예상하지 못한 skip은 실패다.
- [ ] 최신 의존성 감사, 비밀값/공개 문자열 검사, Python/JS CodeQL SARIF 검사, 같은 commit의 재현 가능한 archive 생성·검증·추출본 설치 준비를 통과시킨다.
- [ ] 권한/종료, 감시/알림/UI, 설치/uv/릴리스를 독립 읽기 전용 교차 검토한다. 확정된 P1/P2를 남긴 채 완료·배포하지 않는다. 실제 환경 검증이 막히면 미검증 상태를 기록하고 관련 완료 표시를 하지 않는다.
- [ ] diff·staging과 main의 새 변경을 확인한 뒤 `--no-ff` 병합한다. 통합으로 코드가 달라지면 해당 검사를 다시 수행한다. 최종 SHA를 push하고 그 SHA의 CI 결과를 확인한다.

## 단계 10 — 기존 운영에 반영하고 정리

- [ ] 배포 직전 기준선을 새로 읽는다. 준비한 최종 SHA와 실제 설치할 소스가 같고 dirty=false인지 확인한다. 전체 project 디렉터리나 비공개 Git 이력을 백업 폴더로 복제하지 않는다.
- [ ] 새 웹 환경·import·의존성·웹 계정 접근 검사를 완료한 뒤 `install-services.sh`로 웹/컨트롤러/CLI reader를 적용하고 `install-root.sh`로 guard를 마지막에 적용한다. 두 설치 사이 운영 코드 혼합 상태가 안전한지 단계 9에서 검증한다. 호환되지 않는 조합은 이 순서로 강행하지 않고 한 배포 절차 안에서 guard 쓰기를 잠시 중지·복구하도록 한다.
- [ ] 복구 자료는 기존 설치기가 사용하는 경로에서 이번 배포에 필요한 것만 유지한다. 실패하면 이전 코드/유닛/환경/active/enabled 상태를 복원하고 실패 구간 새 이력을 보존한다. 잘 동작하는 이전 환경을 검증 전에 삭제하지 않는다.
- [ ] 배포 성공은 unit active만으로 판정하지 않는다. 코드 해시·BUILD stamp, 새 PID, 첫 state 기록, 최소 2회 연속 수집과 2회 history 기록, 오류 로그, 실제 Tailscale PC/모바일 10개 화면, auth/CSRF·SSE 표시를 확인한다.
- [ ] 등록 정보·접근 설정·사용자 설정의 불필요한 변경 0, 무관한 서비스 PID·Docker 실행 목록 변경 0을 확인한다. 현재 자원 경보가 warning이어도 정상 수집이면 배포 실패로 취급하지 않는다.
- [ ] 외부 알림은 fixture 성공과 실제 수신을 구분한다. 현재 요청만으로 외부 테스트 메시지를 보내지 않는다. 실제 수신 증거가 없으면 그 한계를 명시한다.
- [ ] 성공 확인 뒤 사용되지 않는 이전 웹 환경과 이번 배포의 복구 자료를 정리한다. 실패/미확인 상태에서 복구 자료를 임시 파일로 취급하지 않는다. 운영 데이터·현재 환경과 소유 불명 자료는 보존한다.
- [ ] 작업 브랜치의 main 포함을 확인하고 삭제한다. 자신이 만든 임시 서버·환경·archive·스크린샷·로그·캐시를 제거한다. worktree를 만들지 않았다면 없는 상태를 확인한다.
- [ ] 기본 경로의 clean main, 브랜치/main 포함, 단일 worktree, 원격 SHA, 설치 SHA, 서비스 상태를 마지막으로 확인한다. 문서만 추가된 배포 기록 commit은 제품 코드 재배포 사유로 삼지 않는다.

```bash
git merge-base --is-ancestor fix/review-remediation main
git branch -d fix/review-remediation
git status --short
git branch --list
git worktree list
```

## 실행 결과 기록

계획에 따라 작업 브랜치에서 구현 중이다. 체크된 항목만 해당 단계의 검증을 완료했으며, 운영 배포·최종 통합 완료는 단계 9–10의 별도 증거가 필요하다. 아래 실행 기록에 실제 결과를 누적한다.

### 실행 기록 — 준비와 R1

- 기준선: 실행 서비스 24개와 Docker 컨테이너 6개, 설정/registry/접근 정책 해시, 실제 설치 코드와 BUILD stamp를 권한 제한된 단일 임시 검증 디렉터리에 기록했다. 운영 3개 유닛은 기존 PID와 재시작 0을 유지했다.
- uv 0.12.23/Python 3.14.4로 기존 dev 해시 잠금 44개를 설치했고 의존성 호환 검사를 통과했다.
- Ruling: 사용자 요구에 따라 추가 worktree/프로젝트 사본/스킬별 기록 디렉터리를 만들지 않는다. 이 문서를 진행 기록으로 사용한다. 편집은 단일 작업자가 수행하고 최종 독립 검토를 실행한다.
- Pre-flight: R1→R4/R5는 저장된 실패/시도 상태를 보존해야 한다. R2→R12는 실제 state 경로를 동일하게 사용해야 한다. R6/R8은 서버 구독 상태 계약을 함께 바꾼다. R13→uv/CI 전환은 동일 커밋 검증을 유지한다. 충돌하는 외부 인터페이스는 발견하지 않았다.
- R1 RED: 잘못된 Gmail 헤더/URL, UnicodeError/RuntimeError와 실제 sample 저장 시험이 기존 코드에서 예외로 실패했다.
- R1 GREEN: 채널별 메시지 구성부터 전송까지 예외를 실패 결과로 격리했다. 오류 세부에는 비밀값을 포함하지 않으며 KeyboardInterrupt는 전파한다.
- 실제 Notifier를 사용하는 sample 2회에서 상태 timestamp 100/115 및 이력 2건을 보존하고 Discord는 재알림 간격 안에서 한 번만 전송 시도했다. 외부 전송은 mock했다.
- 관련 notification/daemon/reporting 54개 통과. 전체 CI 단위 profile 348개 통과, 예상 밖 skip 없음. 제품 운영 적용은 아직 하지 않았다.

### 실행 기록 — R3

- R3 RED: snapshot 후 PID의 시작 시각/UID/서비스가 교체된 재현에서 기존 컨트롤러가 종료를 허용했다. 새 helper 시험은 토큰/함수 부재로 실패했고, 추가 CLI 시험은 timeout=0에서 잘못된 성공 및 이미 종료된 MCP에 신호 전송을 주장하는 실패를 확인했다.
- R3 GREEN: snapshot에 시작 토큰을 보존하고 일관되지 않은 /proc 읽기를 제외한다. 웹/CLI TERM/KILL은 pidfd를 연 뒤 현재 신원과 서비스 분류를 재검증한다. 미지원 환경은 숫자 PID 방식으로 후퇴하지 않는다.
- Ruling: root는 고정된 표준 라이브러리 subprocess에 열린 pidfd를 전달하고 owner UID/기본 GID/빈 보조 그룹으로 신호를 보낸다. pidfd만으로는 검사 이후 setuid exec의 권한 변경을 막지 못하므로 실제 신호 시점의 커널 권한 검사도 owner로 제한한다. 추가 프로세스 생성 비용이 발생한다.
- 서비스 하위 cgroup도 보호하고, 대기 시간 0에도 생존 여부를 확인한다. 이미 사라진 MCP에는 전송 성공 대신 종료된 상태를 알린다.
- 실제 커널 시험에서는 직접 생성한 /bin/sleep 자식만 pidfd로 종료했다. 시스템 서비스나 사용자 세션에는 신호를 보내지 않았다.
- 전체 CI 단위 profile 359개 통과, 예상 밖 skip 없음. 기존 dispatch 확인 시험은 새 공통 신호 경계로 mock을 갱신했으며 신원 검사는 별도 실제 helper 시험이 담당한다. 운영 설치본은 변경하지 않았다.

### 실행 기록 — R2

- R2 RED: 16 MiB를 넘는 정상 세션 5개 × 8,640행에서 원시 이력이 0건이었다. FIFO 읽기가 정상 빈 결과로 반환됐고 동일 크기/mtime의 파일 교체가 캐시를 갱신하지 않았다. 새 제한 iterator/원시 예산 인터페이스도 기존 코드에서는 없었다.
- R2 GREEN: 소유자 권한으로 연 정규 파일을 최초 크기까지 청크로 읽고 줄당 1 MiB/전체 조회 10초를 제한한다. 추가 기록은 다음 조회에서 읽으며 조회 중 파일 축소는 실패다. 잘린 JSON·비객체·비유한 timestamp는 건너뛰고 읽기 실패는 숨기지 않는다.
- Ruling: 원시/집계 응답은 8 MiB, 원시 레코드는 100,000개를 상한으로 두어 worker의 기존 16 MiB보다 먼저 명시적으로 실패한다. 웹 최근 1,800개와 CLI 요청 limit은 최소 heap으로 유지하며 전체 입력을 목록으로 만들지 않는다. 이 제한은 미관측을 빈 정상 값으로 반환하지 않는 대신 큰 원시 요청에 오류를 낸다.
- 집계/프로젝트 귀속/세션/경보는 점진적으로 누적한다. 캐시는 inode/ctime/mtime/크기를 확인하고 16항목으로 제한한다. 스트리밍 도입 후 역행 시각이 최신 외부 관측을 덮어쓰는 회귀도 시험 후 수정했다.
- 16 MiB 미만/동일/초과, 큰 한 줄, FIFO/장치/디렉터리, 읽을 수 있는 링크, 부분 줄, 추가 기록, 파일 교체·축소, 원시 출력 제한을 검증했다. 실제 worker의 큰 날짜 이력·세션·귀속·경보 왕복, 주간 보고서, 출력 초과 및 timeout 오류를 확인했다.
- 전체 CI 단위 profile 372개 통과. 실제 root fixture 1개 통과: 소유자 자격으로 root 전용 fixture 링크를 읽지 못하며 이력 조회는 명시적 실패다. 기존 CLI 날짜 fixture는 실제 timestamp와 같은 날짜로 정정했고 lock 시험은 스트리밍 호출 경계를 추가했다.
- 운영 설치본·설정·서비스는 변경하지 않았다. R3 구현 커밋은 ec0b47b다.

### 실행 기록 — R4/R5

- R4 RED: RAM/swap/세션/MCP의 t=100→1000 관측 공백에서 즉시 경보가 성숙하고, 정상 관측 공백에서 기존 경보가 회복했다. 디스크 회복 타이머·시계 역행·수집 주기 축소에도 이전 시각이 남았다.
- R4 GREEN: 현재 주기의 두 배 초과·시계 역행·유효한 이전 metrics 부재 시 지속/일반 회복/PSI 회복/디스크 회복 시각을 초기화한다. 기존 경보는 새 정상 구간까지 유지한다. 디스크 오류만 반복돼도 회복하지 않으며 설정에서 제거한 드라이브와 관측 실패를 구분한다.
- Ruling: 이전 정상 관측 근거 없이 타이머만 저장된 상태로 즉시 회복할 수 없다. 회복 메시지 기존 fixture 2개에 실제 직전 정상 metrics를 추가했다. 디스크 회복 fixture 2개도 기존의 119/120초 공백 대신 주기적인 정상 표본을 넣어 같은 회복 요구를 검증한다.
- R5 RED: 120초 주기로 예정 분을 건너뛰면 발송이 없었고, 늦게 재시작한 sample도 예정 메일을 처리하지 않았다. 시도/성공 분리 표시는 없었다.
- R5 GREEN: 가장 최근 예정 시각의 epoch 슬롯만 처리한다. 기존 지역 시간 문자열을 읽고 새 기록은 epoch로 쓴다. 미래 슬롯은 관측 경고와 함께 재평가한다. 처리 슬롯, 실제 시도 last_email_status, 확인된 성공 last_email_success를 분리하고 CLI에서도 구분한다. 같은 sample의 Gmail 경보와 heartbeat를 합친다.
- 5/15/60/120/600초 주기, 실패 후 재시작·동일 슬롯·미래 기록·미설정 채널, 미국 DST 반복/누락과 호주 30분 반복을 검증했다. SMTP 실제 발송 없이 채널 경계 fixture만 사용했다.
- 전체 CI 단위 profile 386개 통과. R2 커밋은 a275cd3이며 운영 설치본은 변경하지 않았다.
