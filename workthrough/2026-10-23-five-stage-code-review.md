# 2026-10-23 상세 코드 리뷰 — 5단계 교차 검증

## 범위와 결론

- 기준: `e498e9083a8c9668a05515e692fe7f0c4b7415de`, 버전 `0.2.3`. 시작 시 main과 작업 트리는 깨끗했다.
- 감시·사건·알림, 웹/API·서비스 제어, 소유자 worker·프로세스 신원, 설정·이력, 설치·복구·릴리스 검증 경로를 검토했다. 특히 0.2.2의 재발 병합과 0.2.3의 Paseo 연결 검사를 기존 경로와 대조했다.
- 수정 권고는 기능 문제 **P2 6건**, 문서 패키징 문제 **P3 1건**, 별도의 검사 안정성 문제 **P3 1건**이다. P2는 재현 조건에서 기능 신뢰성에 영향을 주므로 수정이 필요하다는 뜻이다. 이번 검토에서 확정한 P0/P1은 없다.
- 이 작업은 리뷰다. 제품 코드와 테스트 파일을 수정하지 않았다. 재현에는 임시 데이터, 가짜 전송 함수, loopback 서버, 오프라인 브라우저를 사용했다. 운영 발생 여부와 수정 완료를 의미하지 않는다.
- 이전 리뷰에서 이미 수정한 사항과 문서화된 대규모 목록 성능·실기기 제약은 새 결함으로 중복 집계하지 않았다.

## 5단계와 교차 확인 방법

| 단계 | 검토 내용 | 대조한 증거 |
| --- | --- | --- |
| 1. 기준·설계 | Git 상태, 지침, 변경 이력, 문서상 동작과 권한 경계 | README·architecture·usage·configuration, 10월 18~22일 작업 기록, 현재 구현 |
| 2. 로직 | 수집 → 조건 지속 확인 → 사건 전이 → 전달, 설정과 이력 | 함수별 분기, 호출자·피호출자, 상태 파일의 생산자·소비자 |
| 3. 경계·실패 | 웹 인증·CSRF, 제한된 연산, owner UID, pidfd, 설치 복구 | 관련 단위 검사와 소스, 실제 격리 Gunicorn TCP/Unix socket 검사 |
| 4. 실행 | 전체 단위 검사, Chromium PC·모바일, JS/worker, 의존성 감사 | 실행 결과, 실패 지점, 설치 패키지와 잠금 파일 대조 |
| 5. 반증·재현 | 후보의 실제 진입 조건과 사용자 영향 재검증 | `daemon.sample → reconcile → Delivery.tick`, `reports → merge_delivery_summary`, 잘못된 HTTP 응답, 수집 정지 주입, DOM 연결 상태 |

단일 함수에 임의 조건을 넣어 발견한 현상을 그대로 확정하지 않았다. 예를 들어 `unknown → active`에서 revision이 바뀐다는 초기 후보는 PSI 재관측 시 지속 확인 과정까지 고려해야 하므로 일반적인 결함으로 보고하지 않았다. R4는 기본 설정으로 실제 수집 경로를 거친 위험도 하락 사례로 좁혀 확인했다.

## R1 — P2: 회복 후 관찰 기간의 미관측을 정상 지속으로 간주한다

**위치:** [incidents.py](../wsl_resource_guard/incidents.py#L181) 181–194행, [delivery.py](../wsl_resource_guard/delivery.py#L111)의 저장소 신선도 검사.

`resolved + recovery_pending` 분기가 관측 공백과 `_unobserved()` 검사보다 먼저 실행된다. 해소 시각부터 1,800초가 지나면 해당 자원의 관측 여부와 관계없이 revision을 올린다. 저장소 전체의 `updated_at`은 다른 자원 수집으로 계속 최신이므로 전달 worker의 신선도 검사도 통과한다.

**재현:** 기본 지속 시간, 15초 표본, fixture PSI some/full을 사용해 `daemon.sample(incident_mode=True)`와 `Delivery.tick()`을 연결했다.

| 시각(가상 초) | 입력·결과 |
| --- | --- |
| 1000–1030 | PSI some 50%, full 25%. 1030에 critical 사건과 발생 알림 revision 1 |
| 1045–1165 | PSI 정상. 1165에 `resolved`, `recovery_pending=True` |
| 1180–2965 | 두 PSI 값 모두 `None`, `psi_status=error`로 계속 수집 실패 |
| 2965 | `recovery_confirmed_at=2965`, revision 2의 회복 알림 전송 함수 호출. 사건의 마지막 `observed_at`은 여전히 1165 |

**영향:** 최신 PSI 상태를 모르는 동안 회복 확인 알림을 보내며, 문서의 “30분 동안 재발이 없을 때”와 최신 관측 보장에 어긋난다. 디스크 미관측과 Guard 관측 공백도 같은 분기를 검토해야 한다.

**수정 방향:** 해소 시각과 이후 정상 관측 시각을 별도로 관리하고, 미관측·긴 공백은 회복 알림의 확인 기간을 진행시키지 않도록 한다. 이미 확정한 해소 이력을 삭제하는 것과는 구분한다.

**추가 회귀 검사:** 해소 직후 PSI 실패가 30분 지속될 때 회복 발송 없음, 수집 재개 후 충분한 관측에서 한 번 발송, Guard 중단·재개와 디스크 조회 실패.

## R2 — P2: Windows 호스트 메모리 사전 조회가 감시 루프 전체를 멈출 수 있다

**위치:** [metrics.py](../wsl_resource_guard/metrics.py#L153) 153–164행, [daemon.py](../wsl_resource_guard/daemon.py#L623) 623–629행.

Windows 드라이브 용량에는 `bounded_probe()`가 적용됐지만, `read_host_memory()`는 `/mnt/c/.../powershell.exe`의 `Path.exists()`를 주 수집 스레드에서 먼저 호출한다. 이 파일 조회에는 시간 제한이 없다. 아래의 `subprocess.run(timeout=15)`는 그 호출이 끝난 뒤에야 시작된다.

**재현 수준: 정지 메커니즘 재현.** 임시 설정과 정상 자원 fixture로 `sample()`을 실행하면서 PowerShell 경로의 `exists()`만 Event로 대기시켰다. 대기 중 표본은 완료되지 않았고 `state.json`도 생성되지 않았다. Event를 해제하자 정상 완료했다. 별도의 직접 호출에서는 대기 중 `subprocess.run`이 호출되지 않은 것도 확인했다. 실제 9p 장애를 운영 WSL에 유발하지는 않았다.

**영향:** C:의 파일 메타데이터 조회가 멈추면 선택적 vmmem 관측 때문에 RAM·PSI·프로세스·사건 저장까지 멈춘다. 0.2.2의 드라이브 시간 제한이 보호하는 범위 밖에 남은 경로다.

**수정 방향:** 실행 파일 사전 확인을 포함한 호스트 메모리 작업 전체를 제한된 보조 작업으로 옮기고, 중복 작업 생성을 막는다. 시간 초과 시 마지막 관측값·실패 시각을 구분해 유지하면서 표본 루프는 진행해야 한다.

**추가 회귀 검사:** `exists/stat` 정지 중에도 표본·사건 저장이 진행되는지, 반복 호출에 보조 작업이 누적되지 않는지, 해제 후 복구되는지 확인한다.

## R3 — P2: HTTP 프로토콜 오류가 이전 정상 배지를 계속 남긴다

**위치:** [service_control.py](../wsl_resource_guard/service_control.py#L83) 83–97행, 987–1003행, 614–616행; [app.js](../wsl_resource_guard/web/app.js#L721) 721–723행.

`probe_url()`은 `http.client.BadStatusLine` 같은 HTTP 프로토콜 예외를 잡지 않는다. 한 서비스에서 이 예외가 나면 `check_health()`가 `_health = fresh`에 도달하지 못하고 뒤의 서비스도 검사하지 못한다. 루프는 예외를 기록하고 재시도하지만 기존 캐시를 유지한다. 일반 HTTP 결과에는 Paseo에 있는 만료 판정도 없다.

**재현:** 일회용 loopback TCP 서버가 잘못된 HTTP 상태 줄을 반환하게 했다. 이전 HTTP 200 결과를 넣은 컨트롤러에서 실제 HTTP 검사를 두 번 호출하자 모두 `BadStatusLine`이 발생했다. 다음 `snapshot()`은 검사 실패에도 `health.ok=True`, 3,600초 전 `checked_at`을 반환했다. 화면은 이 값으로 `응답 정상`을 표시한다.

**영향:** 장애 난 서비스가 계속 정상으로 보이고, 그 뒤에 등록된 서비스들의 검사가 함께 중단된다. 오래된 실패 결과가 계속 남는 반대 방향의 오표시도 가능하다.

**수정 방향:** HTTP 프로토콜 예외를 실패 결과로 변환하고, 서비스별 검사 실패를 격리한다. 일반 HTTP 결과도 유효 시간을 넘으면 미확인으로 바꾸고, 서비스 재시작 후 이전 실행의 결과를 재사용하지 않도록 검토한다.

**추가 회귀 검사:** 잘못된 상태 줄을 반환하는 서비스 뒤의 정상 서비스까지 검사, 이전 정상 결과를 실패로 갱신, sampler 중단 시 결과 만료.

## R4 — P2: 위험도 하락이 사건 보류를 해제하고 즉시 재알림한다

**위치:** [incidents.py](../wsl_resource_guard/incidents.py#L149) 149–156행, [delivery.py](../wsl_resource_guard/delivery.py#L145) 145–171행.

주석과 사용 문서는 새 사건·위험도 상승이 보류를 넘는다고 설명하지만, 구현은 `old_severity != new_severity`이면 방향에 관계없이 revision을 올린다. 전달 worker는 revision 변경을 새 알림으로 보고 기존 revision의 보류를 무시한다.

**재현:** 기본 설정으로 가용 RAM 1 GiB를 1000초에 관측해 critical 알림을 만든 뒤 1시간 보류(`until=4600`)를 저장했다. 1015·1030·1045초에 3 GiB를 관측했다. 실제 `sample → reconcile → tick` 결과는 다음과 같다.

```text
1000: critical, revision 1, 발송
1015: critical, revision 1, 보류 유지
1030: critical, revision 1, 보류 유지
1045: warning,  revision 2, 다시 발송
```

**영향:** 상황이 개선됐는데도 45초 만에 알림이 오고, 사용자에게 약속한 보류가 풀린다. critical/warning 경계에서 반복하면 재발 병합을 적용해도 소음이 남는다.

**수정 방향:** 표시용 위험도 변경과 보류를 해제하는 알림 전이를 구분한다. 위험도 하락은 기존 보류와 재알림 주기를 유지하고 상승만 즉시 알리도록 한다.

**추가 회귀 검사:** 보류 중 critical→warning은 무발송, warning→critical은 발송, 보류 없이 하락할 때 기존 재알림 간격 유지.

## R5 — P2: 정기 이메일 실패와 발송 시각이 상태 요약에서 사라진다

**위치:** [delivery.py](../wsl_resource_guard/delivery.py#L27) 27–50행과 200–223행; 소비자는 [owner_worker.py](../wsl_resource_guard/owner_worker.py#L49)의 `context/settings_info`, [cli.py](../wsl_resource_guard/cli.py#L145) 145–164행.

`reports()`는 heartbeat 결과를 `scheduled-delivery.json`에 저장한다. `merge_delivery_summary()`는 그 파일에서 `slot`만 읽고 `status`, `attempted_at`, `finished_at`을 합치지 않는다. 채널 오류와 이메일 시각은 사건별 전달 기록만으로 계산된다. 사건 전달 파일이 없으면 정기 전달 기록을 읽기 전에 반환하기도 한다.

**재현:** 사건 전달 목록이 빈 상태에서 최신 표본과 heartbeat를 준비하고, 실제 `Delivery.reports()`의 전송 결과만 SMTP 인증 실패로 대체했다.

```text
scheduled-delivery.json: status=failed, attempted_at=5000
merge_delivery_summary:
  last_channel_errors={}
  last_email_status=0
  last_email_success=0
  last_email_hour_slot=epoch:3600
```

같은 조건의 성공 결과도 별도로 확인했다. 정기 기록은 `status=accepted`였지만 요약의 마지막 시도/성공 시각은 모두 0이었다.

**영향:** 정기 메일이 실패해도 웹·CLI의 채널 오류에 나타나지 않는다. 성공한 정기 메일도 마지막 시도/성공 시각에 반영되지 않아 “아직 기록 없음”으로 표시된다.

**수정 방향:** 사건 전달과 정기 전달의 시각·결과를 함께 집계하되 경보 재알림 시계와는 분리한다. 정기 전달의 실패·접수 미확인·발송 조건 미충족도 구분해 보관해야 한다.

**추가 회귀 검사:** 사건이 없는 상태의 heartbeat 성공·실패·timeout, 두 전달 경로 중 최신 결과 선택, 사건 전달 파일이 없는 경우.

## R6 — P2: 정상적인 긴 수집 주기를 화면이 수집 지연으로 판단한다

**위치:** [app.js](../wsl_resource_guard/web/app.js#L2086) 2086–2103행, 508–512행; [config.py](../wsl_resource_guard/config.py#L45) 45행, [service_control.py](../wsl_resource_guard/service_control.py#L1208) 1208행.

설정은 `interval_seconds=5..600`을 허용하지만 화면의 `observationIsStale()`은 90초를 고정 사용한다. 사건 API와 전달 worker는 수집 주기를 반영한 `max(60, interval_seconds * 3)` 기준을 사용한다.

**재현:** 300초 수집 설정과 120초 전 표본을 컨트롤러에 넣으면 사건 응답은 `stale=False`다. 동일한 나이의 정상 표본을 390px 오프라인 브라우저에 표시하면 개요는 `Guard의 최근 수집 상태를 확인해 주세요.`, 자원 제목/배지는 미확인으로 바뀐다.

**영향:** 지원되는 설정만 사용해도 수집 사이마다 장애처럼 보인다. critical 상태도 신선도 판정 때문에 미확인으로 바뀔 수 있어 화면 간 해석이 달라진다.

**수정 방향:** monitor/summary에도 서버가 계산한 신선도 기한 또는 수집 주기를 제공하고, 브라우저가 같은 기준을 사용하도록 한다.

**추가 회귀 검사:** 15·60·300·600초 설정의 정상 간격과 실제 지연, 미래 시각, SSE/폴링 결과 간 동일한 판정.

## R7 — P3: 배포 압축본에서 README가 가리키는 새 문서가 빠진다

**위치:** [scripts/release.py](../scripts/release.py#L19)의 `ROOT_FILES`·`DOCS` 허용 목록과 51–53행의 파일 선택, 81행 이후 `verify()`.

소스에는 `docs/paseo.md`와 `workthrough/2026-10-22-release-0.2.3.md`가 있고 README도 이 파일들을 링크하지만, 릴리스 빌더의 고정 허용 목록에는 없다. 압축본 검증은 manifest와 실제 파일의 일치·CLI 실행만 확인하므로 둘 다 빠진 상태를 성공으로 처리한다.

**재현:** 임시 디렉터리에서 기준 커밋 `e498e90`을 `release.build()`로 빌드하고 `release.verify()`를 실행했다. 검증은 통과했다. 그 압축본의 Markdown 상대 링크를 대조하자 다음 두 대상이 없었다.

```text
README.md -> docs/paseo.md
README.md -> workthrough/2026-10-22-release-0.2.3.md
```

**영향:** 권장 배포 압축본으로 설치한 사용자는 동봉 README에서 Paseo 설정·연결 검사 안내와 최신 적용 기록을 열 수 없다. 이번 결과는 기준 커밋의 로컬 재빌드로 확인했으며, GitHub에 게시된 파일을 다시 내려받아 검증한 결과는 아니다.

**수정 방향:** 필요한 문서를 허용 목록에 반영하고, 압축본 내부 Markdown의 상대 링크도 검증한다. 문서가 새로 추가될 때 소스 체크아웃의 링크 검사만 통과해서는 누락을 발견할 수 없다.

**추가 회귀 검사:** Paseo 안내 포함, README의 동봉 문서 링크 해소, 문서 누락을 주입한 압축본의 명시적 검증 실패.

## T1 — P3: 카드 크기 검사가 분리된 DOM을 측정해 간헐 실패한다

**위치:** [tests/browser_offline.py](../tests/browser_offline.py#L1154) 1154–1174행.

기준 `browser_offline.py` 실행은 태블릿 카드 검사에서 `textWidth=0`으로 실패했다. 320/390/768px에서 기존 Paseo 검사 묶음을 각 3회 호출한 추가 진단에서도 320px 1회, 768px 1회 같은 실패가 발생했다.

원인 구분을 위해 검사 결과에 `e.isConnected`를 런타임에서만 추가했다. 768px 6회 중 3회 실패했고 모두 `connected=False`, `viewHidden=False`, 현재 DOM의 서비스 행은 4개였다. 이는 화면 이름 잘림의 증거가 아니라 비동기 렌더 교체 중 분리된 요소를 측정한 증거다.

진단용으로 조회 완료를 기다린 뒤 전체 행을 한 번의 `evaluate()` 안에서 측정하자 320/390/768px 각각 3회 × 4개 연결 상태, 총 36개 상태가 통과했다. 추적되는 테스트 소스는 수정하지 않았으므로 기존 전체 오프라인 검사 통과로 기록하지 않는다.

**수정 방향:** DOM이 교체돼도 동일 프레임의 연결된 행을 측정하도록 검사 구조를 바꾼다. 실제 넘침 assertion은 유지한다. 무조건적인 sleep이나 실패 무시는 권하지 않는다.

## 실행한 검증

| 검사 | 실제 결과 |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests -q` | 502개: 500 통과, root 전용 2 skip. 12.770초 |
| `node --check` app.js / investigate.js / sw.js / theme.js | 4개 통과 |
| `node tests/worker_notifications.js` | 통과: 사건 식별·역순 알림·캐시 한도·같은 origin 링크 |
| `.venv/bin/python tests/browser_offline.py` | 실패: T1의 분리된 카드 요소 측정 |
| `.venv/bin/python tests/browser_actionable.py --browser chromium` | 1440/390px 각각 8개 시나리오 통과 |
| `.venv/bin/python tests/browser_review_scenarios.py --browser chromium` | 오류·재시도·응답 역전·설정 추적·모바일 표시 등 통과 |
| `.venv/bin/python tests/gunicorn_transport.py -q` | 3개 통과: 실제 TCP/Unix 신원 경계, CSRF/SSE, 모호한 HTTP framing 거부 |
| `.venv/bin/python scripts/dependency_profiles.py --audit` | web 8개/dev 44개 설치 버전이 잠금과 일치. 두 profile 모두 알려진 취약점 없음 |
| 셸 진입점 `bash -n` | install-user.sh / install-root.sh / install-services.sh / packaging/devbox-wsl-service-recovery 통과 |
| 기준 커밋의 임시 압축본 빌드·검증 | 기존 `release.verify()` 통과. 추가 상대 링크 검사에서 R7의 누락 2개 확인 |
| 리뷰 문서 검사 | `scripts/check_public.py`, `git diff --cached --check` 통과 |
| 추가 진단 | R1/R4 실제 표본→사건→가짜 전달, R2 정지 주입, R3 loopback HTTP 오류, R5 정기 전달→요약, R6 서버·Chromium 표시 대조, T1 DOM 연결 상태·동시 측정 |

검사는 `PYTHONDONTWRITEBYTECODE=1`로 실행했다. 단위 검사에서 설치 실패를 주입하는 로그와 기존 sw.js 파일 핸들의 `ResourceWarning`이 나왔으며 단위 검사 실패는 아니었다. 의존성 감사는 실행 시점의 알려진 취약점 조회 결과이며 코드 전체의 무결함을 뜻하지 않는다.

## 검증 한계와 후속 작업

- root 권한을 실제로 낮추는 2개 검사는 이번 환경에서 skip됐다. 해당 소스·mock 검토와 실제 root 경계 검증을 구분한다.
- WebKit, 실제 Tailscale 서비스, 운영 Paseo 릴레이, 휴대전화 알림 수신, WSL 부팅·운영 설치/복구는 이번에 실행하지 않았다.
- R2는 파일 조회 정지 주입으로 확인한 결함 경로다. 실제 Windows/9p 장애 발생 빈도는 측정하지 않았다.
- R1~R7과 T1은 미수정이다. 후속 구현 시 각 항목의 회귀 검사를 먼저 고정하고 관련 단위·브라우저 검증을 수행해야 한다.
- 우선 처리 순서는 수집 정지 방지(R2), 잘못된 정상/회복 표시 방지(R1/R3), 알림 정책·진단(R4/R5), 수집 주기 일관성(R6), 문서 패키징·검사 안정성(R7/T1)을 권한다.

## 기록·정리

- 작업 브랜치: `docs/five-stage-code-review`. 변경 파일은 이 리뷰 기록과 README의 미수정 제약 안내다. 이번 문서도 현행 압축본 허용 목록 밖에 있으므로 README에서는 개발 저장소의 경로로 안내해 새 상대 링크 누락을 만들지 않는다.
- 공개정보·상대 링크·공백 검사와 스테이징 diff 확인을 마쳤다. 완료 절차는 문서 커밋 → 로컬 main no-ff 병합 → 포함 여부 확인 → 작업 브랜치 삭제다. 제품 수정·운영 배포·원격 push는 이 리뷰에 포함하지 않는다.
- 재현용 상태·설정·가짜 인증값·압축본과 HTTP/Gunicorn 서버는 각 검사의 임시 디렉터리·종료 절차로 정리했다. 브라우저 context/process도 닫았다. 진단 스크립트는 표준 입력으로 실행해 파일을 남기지 않았다.
- 시작 전부터 있던 `.venv`와 ignored `__pycache__`는 이번 작업의 임시 파일이 아니므로 보존한다. 병합 포함 여부를 확인한 뒤 작업 브랜치를 삭제하고 기본 체크아웃의 상태·브랜치·worktree를 최종 확인한다.
