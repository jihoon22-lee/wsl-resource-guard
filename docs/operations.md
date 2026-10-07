# 운영과 복구

## 먼저 확인할 항목

```bash
wrg doctor
systemctl status wsl-resource-guard.service wrg-web.service wrg-service-control.service
journalctl -u wsl-resource-guard.service -n 80 --no-pager
tailscale serve status
```

사용자 guard는 systemctl/journalctl에 `--user`를 사용합니다.
웹 loopback health 성공과 실제 Tailscale 소유자 접근 성공은 다른 검증입니다.
소켓·로그인·Origin·CSRF 문제를 해결하려고 관리 API를 TCP 전체에 공개하거나 Funnel을 켜지 마세요.

## 설치 복구

설치기는 root 복구 자료를 `/var/backups/wrg-services` 또는 `/var/backups/wrg-guard`에 기록합니다.
사용자 복구 자료는 `<owner HOME>/.local/state/wsl-resource-guard/install-backups` 아래에 있습니다.
`manifest.json`, `state-manifest.json`에 기록된 파일과 이전 enabled/active 상태를 확인해 해당 설치의 변경만 복원합니다.
이전 가상환경은 원래 위치에 남겨 두며 unit이 참조하는 환경을 옮기지 않습니다.
실패한 새 guard의 출력은 `state-restore-<id>` 복구 journal에 먼저 보존합니다.
실패하면 코드·유닛·실행 상태의 자동 복구 결과를 확인하고 남은 수동 조치를 처리합니다.

구형 PSI reader로 돌아갈 때에는 새 state/history를 복구 보관본으로 먼저 보존한 뒤 업그레이드 직전 스냅샷을 복원합니다.
null을 0으로 치환하지 않습니다. 새 기록이 보관 위치에 남아 있음을 확인합니다.
실행 중 서비스를 둔 채 코드 디렉터리를 수동 덮어쓰거나 관련 없는 앱을 재시작하지 마세요.

## 검증 수준

- 단위·fixture: 실패 경로와 PC·모바일 렌더링 검증. 실제 알림 수신·WSL 부팅 증거가 아님
- 격리 설치: 폐기 가능한 WSL의 설치·전환·실패 복구·제거. 운영 WSL에 영향 없음
- 실제 연결: 로그인된 Tailnet에서 소유자 접근 및 선택한 알림 수신 확인
- 운영 적용: 사용자가 명시적으로 선택한 릴리스 설치 후 별도 확인

민감한 진단 자료를 Issue에 올리기 전 사용자명·경로·프로세스 인자·호스트·로그인·알림 주소를 제거합니다.
