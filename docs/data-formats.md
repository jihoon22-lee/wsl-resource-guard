# 저장 형식과 컨트롤러 요청

구성요소 사이의 계약입니다. Guard, 컨트롤러, 웹, CLI는 아래 파일과 소켓 요청으로만 정보를 주고받습니다. 구성요소를 따로 업데이트하거나 다른 구현으로 바꿀 때 이 문서를 기준으로 합니다.

## 형식을 바꾸는 규칙

- 필드 추가는 호환 변경입니다. 읽는 쪽은 모르는 필드를 무시하고, 없는 필드는 "미제공"으로 다룹니다. 없는 값을 0이나 정상으로 채우지 않습니다.
- 필드 삭제·이름 변경·의미 변경은 비호환 변경입니다. 해당 파일의 `version`을 올리고, 읽을 때 이전 버전을 이전하는 코드를 함께 넣습니다.
- `incidents.json`과 `delivery.json`에서 모르는 버전을 만나면 덮어쓰지 않고 오류로 처리합니다. 그동안 사건 갱신과 알림 전달이 멈추고 저널에 사유가 남습니다. `state.json`은 최근 표본일 뿐이므로 이후 버전의 파일은 이전 표본이 없는 것으로 보고 새로 씁니다.
- JSON 파일은 임시 파일에 쓴 뒤 이름을 바꿔 교체합니다(`atomic.py`). 읽는 쪽이 쓰다 만 파일을 보는 일은 없습니다. 사건·전달 기록·설정·공유 파일은 교체 전에 디스크에 반영하고, 표본마다 다시 쓰는 `state.json`은 반영을 기다리지 않습니다. 이력(`*.jsonl`)과 감사 기록은 한 줄씩 덧붙입니다.
- 시각은 epoch 초(float)입니다. 용량 단위는 필드 이름의 접미사(`_kib`, `_bytes`, `_gib`)를 따릅니다.

## 소유자 상태 디렉터리

기본 경로는 `~/.local/state/wsl-resource-guard`입니다. Guard만 씁니다. root 컨트롤러는 이 경로를 직접 열지 않고 소유자 권한의 작업 프로세스로 읽습니다.

### `state.json` — 최근 표본

`version`은 1입니다. 수집기만 쓰며 표본마다 전체를 교체합니다.

| 필드 | 내용 |
|---|---|
| `version`, `updated_at`, `writer_pid`, `writer_start_ticks` | 형식 버전, 표본 시각, 기록한 프로세스의 신원 |
| `severity`, `reasons`, `observations`, `pending_reasons` | 현재 판정과 그 근거, 관찰 항목, 판정 대기 사유 |
| `condition_since`, `normal_since`, `abnormal_since` | 조건별 지속 시작 시각, 정상·비정상 구간의 시작 |
| `psi_alert`, `psi_normal_since` | PSI 경보의 독립 유지 상태 |
| `metrics` | 아래 "자원 값" |
| `sessions`, `mcp_rss_kib`, `mcp_count`, `mcp_process_count`, `cpu_jiffies` | LLM 세션·MCP 트리 집계와 CPU 사용률 계산용 누적값 |
| `notified_severity`, `notified_reasons`, `notified_disk_conditions`, `disk_normal_since`, `last_alert`, `last_email_status`, `last_email_success`, `last_email_hour_slot`, `last_channel_errors` | `wrg once --notify`의 구형 알림 경로가 쓰는 상태. 상시 Guard의 알림은 `incidents.json`과 `delivery.json`을 씁니다 |

자원 값(`metrics`)의 필드:

| 필드 | 내용 |
|---|---|
| `timestamp` | 수집 시각 |
| `mem_total_kib`, `mem_available_kib`, `swap_total_kib`, `swap_free_kib` | `/proc/meminfo` |
| `pswpin_pages`, `pswpout_pages`, `swap_in_mib_per_minute`, `swap_out_mib_per_minute`, `oom_kills` | `/proc/vmstat` 누적값과 직전 표본 대비 속도 |
| `psi_some_avg60`, `psi_full_avg60`, `psi_status` | 메모리 PSI. 값은 숫자 또는 null이고 상태는 `normal`·`missing`·`error`·`partial`·`legacy` |
| `disks` | 드라이브별 행: `id`, `kind`(`windows`·`wsl`), `status`(`ok`·`unmounted`·`error`), `checked_at`, `observed_at`, 용량 필드. 조회 실패 시 마지막 관측값을 유지하고 `status`로 구분합니다. 이력 파일에는 VHDX 경로를 뺀 행을 씁니다 |
| `vmmem_bytes`, `vmmem_observed_at`, `vmmem_attempted_at` | Windows가 본 WSL VM 작업 집합과 관측·시도 시각. `host`의 값에서 채웁니다 |
| `host` | 아래 "Windows 호스트". 수집을 한 번도 시도하지 않았으면 빈 객체입니다 |

Windows 호스트(`metrics.host`)의 필드. `schema`는 1이고, 이력 파일에는 기록하지 않습니다.

| 필드 | 내용 |
|---|---|
| `schema`, `status`, `error`, `attempted_at`, `observed_at`, `failures` | 형식 버전, `ok`·`unavailable`·`timeout`·`busy`·`error`, 표시 문구, 마지막 시도와 마지막 성공 시각, 연속 실패 횟수. 실패하면 아래 값은 마지막 관측값이며 `status`로 구분합니다 |
| `memory` | `total_bytes`, `available_bytes`, `commit_used_bytes`, `commit_limit_bytes`, `pagefile_allocated_bytes`, `pagefile_used_bytes`, `pagefile_peak_bytes`. 얻지 못한 값은 null |
| `vmmem` | `working_set_bytes`, `private_bytes`. 프로세스가 없으면 null |
| `volumes` | 고정 볼륨별 `id`, `total_bytes`, `free_bytes`, `filesystem`, `mounted`(WSL 마운트 여부). WSL에 마운트되지 않은 볼륨도 포함합니다 |
| `distros`, `distro`, `vhd_path` | 배포판별 `name`, `state`, `version`, `default`, `vhd_dir`, 현재 배포판 이름, 존재를 확인한 현재 배포판의 VHDX 경로(없으면 빈 문자열) |
| `wslconfig` | `present`(파일을 확인하지 못하면 null), `memory`, `swap`, `processors`, `auto_memory_reclaim`, `sparse_vhd`. 알려진 키만 읽습니다 |
| `processes` | 작업 집합 상위 8개의 `name`, `working_set_bytes`. 화면 표시 전용입니다 |
| `windows` | `build`, `boot_at` |

### `incidents.json` — 경보 사건

`version`은 1입니다. 최대 256건, 약 3 MiB입니다.

| 필드 | 내용 |
|---|---|
| `version`, `updated_at`, `retention_days`, `truncated`, `clock_adjusted` | 형식 버전, 마지막 갱신, 보존 일수, 생략 여부, 시계 역행 감지 |
| `incidents` | 사건 행 목록 |

사건 행의 필드:

| 필드 | 내용 |
|---|---|
| `id` | 32자리 16진수. 알림 링크와 전달 기록의 키 |
| `key` | `<원인>:<범위>`. 디스크는 `disk.E::host`처럼 호스트 범위라 WSL 재시작을 넘어 이어집니다. RAM·PSI·swap·OOM·MCP는 boot ID, 세션은 프로세스 신원이 범위입니다 |
| `state` | `active`·`resolving`·`resolved`·`unknown`. 관측을 잃으면 `unknown`이며 해소로 바꾸지 않습니다 |
| `severity`, `revision`, `priority` | 위험도, 알림 단위(새 사건·위험도 상승·회복 확정 때 증가), `pressure`·`review` |
| `opened_at`, `observed_at`, `normal_since`, `resolved_at` | 시작, 마지막 관측, 정상 구간 시작, 해소 시각 |
| `recovery_pending`, `recovery_since`, `recovery_checked_at`, `recovery_paused`, `recovery_confirmed_at`, `recovery_unconfirmed` | 해소 뒤 회복 확정까지의 관찰 상태. `recovery_unconfirmed`는 관측으로 확정하지 못해 회복을 알리지 않기로 한 행입니다 |
| `reason`, `initial_reason`, `guidance`, `evidence`, `measurement`, `initial_value` | 표시 문구와 관측 근거 |
| `targets` | 관련 세션·도구의 요약(신원, 종류, 프로젝트, RSS, 종료 가능 여부) |
| `transitions`, `recurrences` | 최근 상태 전이 16개, 재발 횟수 |

### `delivery.json` — 사건별 전달 기록

`version`은 1입니다. 알림 worker만 씁니다.

| 필드 | 내용 |
|---|---|
| `version`, `updated_at` | 형식 버전, 마지막 기록 |
| `events` | 사건 `id`별 항목: `revision`, `state`, `next_reminder_at`, `recovery_keys`, `destinations` |

수신처 항목(`destinations`의 값)의 필드:

| 필드 | 내용 |
|---|---|
| `channel`, `label` | 채널과 표시 이름. 수신 주소는 저장하지 않습니다 |
| `status` | `pending`·`sending`·`accepted`·`retry`·`failed`·`unknown`·`skipped` |
| `attempts`, `attempted_at`, `finished_at`, `next_at` | 시도 횟수와 시각, 다음 시도 가능 시각 |
| `detail`, `reason` | 상태 설명 문구, 실패·재시도·미확인일 때의 짧은 원인 |

### 그 밖의 파일

| 파일 | 내용 |
|---|---|
| `history-YYYY-MM-DD.jsonl` | 한 줄에 표본 하나: `timestamp`, `severity`, `reasons`, `observations`, `pending_reasons`, `metrics`, `sessions`, `mcp_rss_kib`, `mcp_count`, `mcp_process_count` |
| `disk-history-YYYY-MM-DD.jsonl` | 한 시간에 한 줄: `timestamp`, `disks` |
| `scheduled-delivery.json` | 정기 이메일의 `slot`, `status`, `attempted_at`, `finished_at`, `last_attempt_at`, `last_success_at` |
| `weekly-report.json` | 주간 리포트의 `slot`, `sent_at`, `status`(`sending`·`sent`·`failed`·`skipped`), `attempts`, `next_attempt_at`, `results` |
| `config-request-result.json` | 웹 설정 요청의 처리 결과: `id`, `key`, `value`, `ok`, `message`, `requested_at`, `applied_at` |
| `push-expired.json` | 푸시 서비스가 만료로 알린 구독 endpoint 목록 |
| `daemon.lock` | 상태 디렉터리당 Guard 하나를 보장하는 잠금 |

## root 공유 디렉터리

`/var/lib/wrg-shared`입니다. root만 쓰고 Guard는 읽기만 합니다. 컨트롤러가 쓰는 파일은 0640, root:소유자 그룹이고 `dashboard.json`은 설치기가 씁니다.

| 파일 | 내용 |
|---|---|
| `alert-snooze.json` | 재알림 일시 정지: `until`, `actor`, `created_at` |
| `config-request.json` | 웹의 숫자 설정 변경 요청 한 건: `id`, `key`, `value`, `actor`, `requested_at`, `interval_seconds` |
| `incident-decisions.json` | 사건 `id`별 대응: `choice`(`acknowledge`·`defer`·`resume`), `at`, `until`, `severity`, `revision`, `actor` |
| `push-vapid.json`, `push-subscriptions.json` | 웹 푸시 키와 구독 |
| `dashboard.json` | 알림 링크에 쓰는 대시보드 `origin` |
| `launch/` | 시작 절차가 연결된 Compose 프로젝트의 자동 실행 플래그 |

## 컨트롤러 소켓

`/run/wrg-services/control.sock`입니다. 요청은 JSON 한 줄(16 KiB 이하)이고 응답도 JSON 한 줄입니다. 접속자의 UID가 root·등록 소유자·웹 계정일 때만 처리합니다.

```text
요청  {"op": "<연산>", ...}
응답  {"ok": true, "data": ...}
      {"ok": false, "error": "<문구>"}
      {"ok": false, "error": "<문구>", "code": "<정해진 코드>"}   정해진 코드가 있는 오류만
```

요청에는 연산 이름과 범위가 정해진 값만 들어갑니다. 명령문·경로·유닛 내용은 받지 않습니다.

| 종류 | 연산 |
|---|---|
| 조회 | `list`, `discover`, `monitor`, `summary`, `history`, `attribution`, `session-history`, `alerts`, `incidents`, `target-detail`, `disks`, `docker-df`, `service-memory`, `settings`, `config-request`, `audit`, `logs`, `push-key`, `push-status`, `action-status` |
| 변경 | `register`, `action`, `restore`, `snooze`, `incident-decision`, `config-change`, `push-subscribe`, `push-unsubscribe`, `push-test`, `action-preview`, `action-execute`, `kill`, `kill-stale` |

변경 연산은 감사 기록(`/var/lib/wrg-services/audit.jsonl`)에 식별자와 결과만 남깁니다. `push-key`는 조회지만 첫 호출 때 푸시 키를 만듭니다. 웹 계정의 요청 중 `kill`·`kill-stale`·`restore`와, Tailscale 서비스를 끄거나 다시 시작하는 `action`은 거부합니다.
