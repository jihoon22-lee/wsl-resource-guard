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

## 수집·이력·알림 진단

서비스가 active여도 state의 갱신 시각과 history의 새 기록을 함께 확인하세요.
설치기는 새 guard 프로세스의 첫 상태 기록을 확인한 뒤 성공을 반환합니다.
수집 주기가 길면 첫 확인에 시간이 걸릴 수 있으며 준비 제한 시간은 180초입니다.
설정한 시스템 상태 경로가 바뀌었다는 경고는 `install-root.sh`로 쓰기 허용 경로를 재생성해야 한다는 뜻입니다.

일별 이력은 순차 읽기를 사용하므로 16 MiB를 넘었다는 이유로 날짜 전체를 생략하지 않습니다.
다만 읽기 시간·한 줄 크기·응답 크기 한도를 넘으면 명시적인 오류가 납니다. 원시 조회의 기간을 줄이거나 집계 조회를 사용하세요.
권한·FIFO·잘린 기록 문제를 빈 정상 이력으로 간주하지 말고 해당 파일과 로그를 확인합니다.

알림 오류는 채널별로 확인합니다. '등록됨', '전송 시도', '전송 성공', 실제 단말 수신은 서로 다른 상태입니다.
외부 수신 여부는 사용자가 선택한 채널의 명시적 시험으로 확인하며, 자동 설치/CI는 실제 수신자에게 시험 메시지를 보내지 않습니다.

## 검증 수준

- 단위·fixture: 실패 경로와 PC·모바일 렌더링 검증. 실제 알림 수신·WSL 부팅 증거가 아님
- 격리 설치: 일회용 systemd VM에서 실제 유닛의 설치·전환·실패 복구·제거. WSL 고유 동작·재부팅 검증과 구분
- 실제 연결: 로그인된 Tailnet에서 소유자 접근 및 선택한 알림 수신 확인
- 운영 적용: 사용자가 명시적으로 선택한 릴리스 설치 후 별도 확인

민감한 진단 자료를 Issue에 올리기 전 사용자명·경로·프로세스 인자·호스트·로그인·알림 주소를 제거합니다.
