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
| wsl_vhd_path | 선택적 VHDX 경로. 표시를 위한 조회만 수행 |
| interval_seconds / history_interval_seconds | 수집·이력 기록 간격, 초 |
| retention_days | 보존 기간, 일 |
| alert_quiet_hours | 예: `23-08`. warning 재알림 억제, 새 경보·critical·회복은 별도 |
| recovery_sustain_seconds | 정상 재관측 후 회복 확인 시간 |

웹에서는 제한된 숫자 설정만 요청할 수 있습니다. 텍스트·채널 스위치는 로컬 CLI에서 관리합니다.
실행 중 state_dir 변경은 잠금·writer 일관성을 위해 재시작이 필요합니다.

외부 알림은 선택 사항입니다. `configure-alerts`는 비밀값을 숨김 입력으로 받고 0600 권한으로 보관합니다.
`wrg test-alert`는 실제 메시지를 보내므로 수신 계정을 확인한 뒤 명시적으로 실행하세요.
자격 정보가 없거나 관측할 수 없는 항목을 정상 수신·정상 0으로 표시하지 않습니다.

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
| `warning_disk_free_percent` | `20.0` | 1–95 | Warning: 디스크 여유 (%) |
| `critical_disk_free_percent` | `10.0` | 0.5–95 | Critical: 디스크 여유 (%) |
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
| `host_memory_refresh_seconds` | `300` | 60–3600 | 호스트 vmmem 수집 주기 (초) |
| `push_enabled` | `true` | true / false | 등록한 기기로 브라우저 푸시 알림 |
| `weekly_report_enabled` | `true` | true / false | 주간 리포트 발송 (Gmail·Discord·webhook) |
| `weekly_report_weekday` | `0` | 0–6 | 주간 리포트 요일 (0=월 … 6=일) |
| `weekly_report_hour` | `9` | 0–23 | 주간 리포트 발송 시각 (시) |

`project_roots`, `disk_drives`, `wsl_vhd_path`, `state_dir`, `toast_app_id`는 TOML에서 관리합니다. 기본 Windows 드라이브는 C·D·E, VHDX 경로는 빈 값으로 자동 탐색합니다. `email_heartbeat_interval_seconds`는 구형 설정 호환용이며 새 설정은 발송 분을 사용합니다.
