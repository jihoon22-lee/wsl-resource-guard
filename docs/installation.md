# 설치·업데이트·제거

## 사전 조건

- Ubuntu on WSL2, systemd 및 cgroup v2 활성화, Python 3.11 이상
- 사용자 guard: 사용자 systemd 세션과 `~/.local/bin` PATH
- 관리 웹: sudo, 시스템 Python 3.11–3.14, 로그인된 Tailscale, HTTPS Serve 사용 가능
- Windows 관측: WSL Windows interop·PowerShell. Docker는 선택 사항
- Web Push: **시스템 Python**의 cryptography (`sudo apt install python3-cryptography`). 웹 가상환경에만 설치해서는 충분하지 않음

배포본의 SHA256SUMS를 확인하고 프로젝트 전용 tar.gz를 풉니다. 체크섬은 파일 손상 확인이며 별도의 배포자 서명은 아닙니다.
GitHub 자동 생성 Source code 압축파일에는 프로젝트 릴리스 메타데이터가 없을 수 있습니다.

## 설치 유형

```bash
bash install-user.sh  # 현재 사용자 guard를 설치하고 시작
wrg doctor
wrg status
```

부팅 수준 감시는 일반 사용자 터미널에서 수행합니다. 필요한 시스템 변경만 sudo로 실행합니다.

```bash
bash install-root.sh
systemctl status wsl-resource-guard.service
```

사용자 guard가 실행 중이면 시스템 guard로 전환하며 실패 시 이전 상태를 복구합니다.
역방향 전환은 시스템 guard를 명시적으로 제거한 뒤 수행합니다. 두 guard를 동시에 설치·실행하지 마세요.

설치기는 owner 잠금을 얻은 뒤 충돌을 검사합니다. 시스템 guard는 설정된 상태 디렉터리를 소유자 권한으로 준비하고 유닛의 쓰기 허용 경로에 반영합니다. 경로의 제어문자와 링크 이탈은 거부하며, systemd 실행 파일 경로 제약 때문에 HOME에 따옴표·역슬래시 또는 끝 공백이 있는 경우 설치 전에 거부합니다. 공백·`%`·`$`는 해당 systemd 구문에 맞춰 리터럴로 처리합니다.

서비스 시작 뒤 최대 180초 동안 새 PID·시작 토큰과 일치하는 첫 상태 기록을 확인합니다. 단순 active 상태만으로 성공하지 않으며 수집 실패 시 코드·유닛·실행/자동 실행 상태와 기존 데이터를 복구합니다. 실패한 실행의 새 기록도 복구 자료 안에 보존합니다.

웹과 서비스 관리 설치:

```bash
bash install-services.sh
wrg web
wrg services
```

관리자 직접 실행 시 `--owner <linux-user>`로 대상을 지정합니다. 기존 설치와 owner/origin이 다르면 변경 전에 실패합니다.
9443의 다른 Serve 설정이나 Funnel, 태그가 지정된 장치는 사전 검사에서 거부합니다.
설치 성공 후 loopback `/healthz`와 소유자 기기의 실제 Tailscale 웹 접근을 각각 확인하세요.
loopback은 관리 API에 접근할 수 없습니다.

## 업데이트

같은 공개 커밋 또는 릴리스에서 웹·컨트롤러와 CLI reader를 먼저 업데이트하고 guard를 마지막에 재시작합니다.
웹이 설치된 경우 `install-services.sh` → 시스템 guard의 `install-root.sh` 순서입니다.
사용자 guard만 있다면 `install-user.sh`를 다시 실행합니다.

웹 설치기는 uv 0.12.23 공식 Linux glibc 바이너리(x86_64/aarch64)를 다운로드하고
코드에 고정된 SHA-256을 검증한 뒤 root 소유의 임시 디렉터리에서만 실행합니다.
GitHub Releases와 PyPI에 직접 HTTPS 접근할 수 있어야 하며 Ubuntu의 시스템 CA 번들을 사용합니다.
사용자 PATH·홈의 uv, 프록시·사설 인덱스·uv 설정을 상속하지 않습니다. Python 자동 다운로드도 하지 않습니다.
지원 아키텍처, 다운로드, 해시 또는 의존성 검증이 실패하면 서비스 전환 전에 중단합니다.
설치용 uv와 캐시는 작업 후 제거하며 서비스 실행 자체에는 uv가 필요하지 않습니다.
가상환경에는 잠금 파일의 wheel만 설치하고 소스 빌드는 허용하지 않습니다.

새 의존성 환경은 고정 경로 `.venvs/<install-id>`에 준비하고 서비스 유닛을 전환합니다.
기존 `.venv`와 복구 백업을 이동하거나 임의 삭제하지 마세요.
`wrg doctor`에서 설치본 커밋 일치와 서비스 상태를 확인합니다.
실행 중인 guard는 웹 설치만으로 새 코드로 재시작되지 않습니다.

## 수동 제거

별도 제거 프로그램은 없습니다. 설치 유형과 소유한 파일을 확인하고 다음 순서로 제거합니다.

1. 사용자 guard는 `systemctl --user disable --now wsl-resource-guard.service` 후 해당 사용자 유닛만 제거합니다.
2. 시스템 guard는 `sudo systemctl disable --now wsl-resource-guard.service` 후 해당 시스템 유닛만 제거합니다.
3. 웹 전체를 제거할 때는 `wrg-web`, `wrg-service-control`, `wrg-services-restore`를 중지·비활성화합니다. 부팅 복구가 중단됨을 확인합니다.
4. `tailscale serve status`로 Resource Guard 소유 handler인지 확인한 뒤 해당 9443 Serve만 해제합니다. 전체 Serve reset을 사용하지 않습니다.
5. 해당 유닛을 제거하고 `sudo systemctl daemon-reload`를 수행합니다. 사용자 유닛은 `--user` 범위에서 수행합니다.
6. 참조하는 프로세스·유닛이 없음을 확인한 뒤 프로그램 사본만 제거합니다.

등록된 외부 앱·Docker 컨테이너/볼륨, 사용자 config/secrets/state/history, registry, 복구 백업은 기본 보존합니다.
기존 호환 복구 유닛이 있다면 그 소유와 사용처를 별도로 확인합니다. 관련 없는 앱을 중지하지 않습니다.
