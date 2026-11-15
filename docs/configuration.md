# 설정

기본 파일은 `~/.config/wsl-resource-guard/config.toml`, 비밀값은 같은 디렉터리의 `secrets.json`입니다.
상태·이력은 기본 `~/.local/state/wsl-resource-guard`에 저장합니다.
`wrg --config <path>` 및 로컬 환경의 WRG_CONFIG/WRG_SECRETS는 CLI용 선택 사항입니다.
웹 작업 프로세스는 등록 소유자의 경로를 사용하며 호출자의 환경을 상속하지 않습니다.

```bash
wrg config
wrg config set warning_available_gib 4
wrg config set critical_available_gib 2
wrg configure-alerts
```

정확한 현재 기본값·단위·범위는 `wrg config`, 웹 설정 화면과 소스의 CONFIG_RULES를 기준으로 합니다.
파일 템플릿은 [config.toml](../config/config.toml)입니다. 경고 임계값과 심각 임계값 관계도 검증합니다.

| 항목 | 의미 |
|---|---|
| project_roots | 프로젝트 분류 기준. 기본 `~/projects`, 소유자 HOME 기준으로 해석 |
| disk_drives | 관측할 Windows 드라이브 목록. 연결되지 않은 드라이브는 미관측 표시 |
| wsl_vhd_path | 선택적 VHDX 경로. 비우면 자동 탐지. 표시를 위한 조회만 수행 |
| interval_seconds / history_interval_seconds | 수집·이력 기록 간격, 초 |
| retention_days | 이력과 해소된 사건의 보존 기간, 일. 사건에는 개수/크기 한도도 적용 |
| alert_quiet_hours | 예: `23-08`. warning 재알림 억제, 새 경보·critical·회복은 별도 |
| recovery_sustain_seconds | 정상 재관측 후 회복 확인 시간 |

웹에서는 제한된 숫자 설정만 요청할 수 있습니다. 텍스트·채널 스위치는 로컬 CLI에서 관리합니다.
시스템 guard의 `state_dir`는 소유자 HOME 아래 절대 경로 또는 `~/` 경로만 지원하며, 심볼릭 링크를 거치는 디렉터리는 거부합니다. 변경 후 `install-root.sh`로 쓰기 허용 경로를 재생성해야 합니다. 실행 중이거나 유닛만 재시작한 경우 기존 설치 경로를 유지하고 재설치 필요 경고를 남깁니다. 사용자 guard는 기존 사용자 권한의 경로를 지원하며 변경 시 재시작이 필요합니다.

외부 알림은 선택 사항입니다. `configure-alerts`는 비밀값을 숨김 입력으로 받고 0600 권한으로 보관합니다.
`wrg test-alert`는 실제 메시지를 보내므로 수신 계정을 확인한 뒤 명시적으로 실행하세요.
자격 정보가 없거나 관측할 수 없는 항목을 정상 수신·정상 0으로 표시하지 않습니다.
수집 간격이 설정 주기의 두 배를 넘거나 시계가 역행하면 지속·회복 판정 시간을 다시 시작합니다.
화면과 사건 API의 관측 신선도는 `max(60초, interval_seconds × 3)`를 사용하며, 미래 시각도 미확인으로 처리합니다. 지속·회복 판정에 필요한 연속 관측 기준과는 별개입니다.
기존 경보는 관측 실패만으로 해제하지 않고 새 정상 관측으로 회복을 확인합니다.
상시 Guard의 사건별 재알림은 `reminder_cooldown_seconds`/`critical_reminder_seconds`를 사용합니다. 사건별 보류는 해당 revision에만 적용되며 다른 사건과 위험도 상승을 막지 않습니다. 관련 상위 세션 목록의 변화나 위험도 하락은 revision을 올리지 않으며 기존 보류와 재알림 시각을 유지합니다. 지속 확인 전 다시 기준을 넘은 조건은 기존 사건을 유지하며 회복으로 처리하지 않습니다. 해소 후 정상 관측이 연속 30분 이어져야 회복 알림(`recovery_notifications`)을 보냅니다. 관측 실패·긴 수집 공백·시계 역행·지속 확인 중인 재초과는 확인 기간을 다시 시작하게 합니다. 회복 확정 전 같은 원인이 다시 확인되면 경과 시간과 관계없이 기존 사건과 보류를 이어 가며, 위험도 상승은 즉시 알립니다. 이미 확정된 과거 회복은 다시 알리지 않습니다. 기존 전역 보류도 반복 알림에만 적용됩니다.
heartbeat는 설정한 발송 분을 놓쳐도 알림 worker가 가장 최근 예정 슬롯을 한 번 처리합니다.
오래 중단된 동안의 모든 슬롯을 몰아서 발송하지 않으며 발송 시도와 실제 성공은 구분합니다.
정기 이메일의 실패·접수 미확인도 채널 상태에 표시합니다. 발송 조건 미충족은 시도로 세지 않고, 이후 실패하거나 건너뛰어도 마지막 성공 시각을 보존합니다. 접수 여부가 불명확한 슬롯은 자동 재전송하지 않습니다.

## 검증되는 기본값과 범위

단위는 설명 열에 표시합니다. bool은 `true`/`false`, 시간대의 빈 문자열은 기능 해제입니다.

| 키 | 기본값 | 허용 범위 | 설명 |
|---|---|---|---|
| `warning_available_gib` | `4.0` | 0.1–1024 | Warning: 가용 RAM 임계값 (GiB) |
| `critical_available_gib` | `2.0` | 0.1–1024 | Critical: 가용 RAM 임계값 (GiB) |
| `warning_psi_some_avg60` | `2.0` | 0.1–100 | Warning: PSI some 60초 평균 (%) |
| `critical_psi_full_avg60` | `20.0` | 0.1–100 | Critical: PSI full 60초 평균 (%) |
| `critical_psi_full_low_ram_avg60` | `1.0` | 0.1–100 | Critical: 낮은 RAM에서의 PSI full (%) |
| `critical_psi_full_low_ram_available_gib` | `4.0` | 0.1–1024 | PSI 복합 판정의 낮은 RAM 기준 (GiB) |
| `warning_swap_out_mib_per_minute` | `256.0` | 1–102400 | Warning: swap-out 속도 (MiB/분) |
| `warning_swap_out_max_available_gib` | `6.0` | 0.1–1024 | swap-out 경보의 가용 RAM 상한 (GiB) |
| `warning_host_available_gib` | `2.0` | 0–1024 | Warning: Windows 가용 RAM (GiB, 0=사용 안 함) |
| `critical_host_available_gib` | `1.0` | 0–1024 | Critical: Windows 가용 RAM (GiB, 0=사용 안 함) |
| `warning_host_commit_percent` | `90.0` | 0–100 | Warning: Windows 커밋 사용률 (%, 0=사용 안 함) |
| `critical_host_commit_percent` | `97.0` | 0–100 | Critical: Windows 커밋 사용률 (%, 0=사용 안 함) |
| `host_alert_sustain_seconds` | `120` | 30–3600 | Windows 호스트 경보 지속 시간 (초) |
| `warning_disk_free_percent` | `20.0` | 1–95 | Warning: 디스크 여유 (%) |
| `critical_disk_free_percent` | `10.0` | 0.5–95 | Critical: 디스크 여유 (%) |
| `warning_disk_free_gib` | `0.0` | 0–100000 | Warning 예외: 여유가 이 용량(GiB) 이상이면 경보 안 함 (0=사용 안 함) |
| `critical_disk_free_gib` | `0.0` | 0–100000 | Critical 예외: 여유가 이 용량(GiB) 이상이면 경보 안 함 (0=사용 안 함) |
| `session_observe_rss_gib` | `4.0` | 0.5–1024 | 세션 관찰 시작 RSS (GiB) |
| `session_warning_rss_gib` | `6.0` | 0.5–1024 | Warning: 세션 RSS (GiB) |
| `mcp_observe_rss_gib` | `3.0` | 0.5–1024 | MCP 관찰 시작 RSS (GiB) |
| `mcp_warning_rss_gib` | `5.0` | 0.5–1024 | Warning: MCP RSS (GiB) |
| `stale_session_hours` | `48.0` | 1–720 | 오래된 세션 표시 기준 (시간) |
| `interval_seconds` | `15` | 5–600 | 샘플링 주기 (초) |
| `history_interval_seconds` | `60` | 10–86400 | 이력 기록 주기 (초) |
| `retention_days` | `14` | 1–365 | 이력 보존 일수 |
| `reminder_cooldown_seconds` | `21600` | 60–604800 | Warning 재알림 간격 (초) |
| `critical_reminder_seconds` | `900` | 60–86400 | Critical 재알림 간격 (초) |
| `recovery_sustain_seconds` | `120` | 10–3600 | 회복 판정 유지 시간 (초) |
| `recovery_notifications` | `true` | true / false | 회복 알림 사용 |
| `disk_refresh_seconds` | `60` | 15–3600 | 디스크 측정 주기 (초) |
| `process_warning_sustain_seconds` | `120` | 10–3600 | 세션/MCP 경보 지속 시간 (초) |
| `warning_available_sustain_seconds` | `30` | 5–600 | 가용 RAM 경보 지속 시간 (초) |
| `warning_psi_sustain_seconds` | `300` | 5–600 | PSI 경보 지속 시간 (초) |
| `critical_psi_sustain_seconds` | `30` | 5–600 | Critical PSI 지속 시간 (초) |
| `warning_swap_out_sustain_seconds` | `30` | 5–600 | swap-out 경보 지속 시간 (초) |
| `alert_quiet_hours` | `""` | HH[:MM]-HH[:MM] 또는 빈 값 | Warning 재알림 억제 시간대 HH[:MM]-HH[:MM] (빈 값=해제) |
| `email_heartbeat_enabled` | `false` | true / false | 정기 이메일 heartbeat 사용 |
| `email_heartbeat_minute` | `0` | 0–59 | heartbeat 발송 분 |
| `windows_toast_enabled` | `true` | true / false | Windows 토스트 알림 사용 |
| `gmail_enabled` | `true` | true / false | Gmail 알림 사용 |
| `discord_enabled` | `true` | true / false | Discord 알림 사용 |
| `webhook_enabled` | `false` | true / false | 범용 webhook 알림 사용 (URL은 secrets.json의 webhook_url) |
| `host_memory_refresh_seconds` | `60` | 60–3600 | Windows 호스트 상태 수집 주기 (초). 주기마다 PowerShell을 한 번 실행하며 실패가 이어지면 간격을 최대 15분까지 늘립니다 |
| `push_enabled` | `true` | true / false | 등록한 기기로 브라우저 푸시 알림 |
| `weekly_report_enabled` | `true` | true / false | 주간 리포트 발송 (Gmail·Discord·webhook) |
| `weekly_report_weekday` | `0` | 0–6 | 주간 리포트 요일 (0=월 … 6=일) |
| `weekly_report_hour` | `9` | 0–23 | 주간 리포트 발송 시각 (시) |

Windows 호스트 경보는 Windows가 보고한 가용 RAM이 기준 이하이거나 커밋 사용률이 기준 이상인 상태가 `host_alert_sustain_seconds` 동안 이어질 때 발생합니다. WSL 안의 가용 RAM과는 별개의 값입니다.
지속 시간은 Windows를 실제로 관측한 시각 사이로 잽니다. 호스트는 `host_memory_refresh_seconds`마다 한 번 관측하므로 기본값에서는 연속 세 번의 관측이 필요하고, 한 번 읽은 값을 다시 읽는 것만으로는 경보가 나지 않습니다. 수집 주기를 길게 잡으면 필요한 관측 횟수가 줄어 두 번까지 내려갑니다.
호스트를 한 번도 관측하지 못했으면 경보하지 않습니다. 이미 난 경보는 관측이 끊겨도 마지막 확인값으로 유지하고 회복으로 처리하지 않으며, 그동안에는 정기 재알림을 보내지 않습니다. 관측이 끊긴 것 자체를 알리는 경보는 없습니다.
값을 0으로 두면 해당 단계를 끕니다. 끄는 것은 `wrg config set`으로만 할 수 있고 웹에서는 거부합니다. 단계를 끄면 진행 중이던 경보는 회복 알림 없이 "감시 종료 · 회복 미확인"으로 닫습니다.

디스크 경보는 여유 비율이 기준 미만일 때 발생합니다. 용량이 큰 드라이브는 비율만으로는 너무 일찍 경보가 나므로, 절대 용량 하한(`*_disk_free_gib`)을 설정하면 여유가 그 용량 미만일 때만 경보합니다. 예를 들어 Warning 20%·50 GiB이면 여유가 20% 미만이면서 50 GiB 미만일 때 Warning입니다. Warning 하한만 설정하면 Critical에도 같은 하한이 적용되어, Warning이 나지 않는 상태에서 Critical이 나지 않습니다.
드라이브마다 다른 기준이 필요하면 `config.toml` 맨 끝에 드라이브별 표를 둡니다. 적지 않은 값은 공통 기준을 따릅니다. 형식이 틀리거나 Warning이 Critical보다 크지 않은 표는 그 드라이브만 무시하고 사유를 `wrg config`에 표시합니다. 공통 기준을 바꿀 때 드라이브별 표와 맞지 않게 되는 값은 거부합니다. 드라이브별 표는 웹에서 바꿀 수 없습니다.
표는 반드시 다른 모든 설정 아래에 두세요. TOML에서는 `[표]` 줄 뒤에 적은 키가 모두 그 표에 속하므로, 중간에 두면 뒤의 설정이 적용되지 않습니다.

```toml
[disk_thresholds.E]
warning_free_percent = 5
critical_free_percent = 3
warning_free_gib = 50
critical_free_gib = 20
```

`disk_drives`에서 뺀 드라이브의 경보는 회복 알림 없이 "감시 종료 · 회복 미확인"으로 닫습니다. 설정 파일을 제대로 읽지 못한 상태에서는 닫지 않습니다.

`project_roots`, `disk_drives`, `wsl_vhd_path`, `state_dir`, `toast_app_id`는 TOML에서 관리합니다. 기본 Windows 드라이브는 C·D·E, VHDX 경로를 비워 두면 Windows가 알려 준 현재 배포판 위치에서 `ext4.vhdx`를 찾아 사용하고, 그 파일이 없으면 미설정(`not-configured`)으로 표시합니다. `email_heartbeat_interval_seconds`는 구형 설정 호환용이며 새 설정은 발송 분을 사용합니다.
