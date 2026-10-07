# WSL Resource Guard

WSL2의 메모리·swap·PSI·디스크와 LLM/MCP 프로세스를 감시하고, 개인 Tailscale 화면에서 등록한 서비스를 관리합니다.
토큰 사용량·구독 인증·비용 통계는 이 프로젝트의 범위가 아닙니다.

## 주요 기능

- RAM·swap 입출력·PSI·OOM·Windows 볼륨·WSL 디스크 관측과 이력
- Codex, Claude, OpenCode, Gemini, Aider, Devin, Antigravity 세션 및 MCP 프로세스 분류
- 기존 systemd·Docker Compose 서비스 등록, 실행 상태와 자동 실행의 독립 제어
- 소유자만 접근하는 Tailscale 웹 대시보드, PC·모바일 화면
- Windows 알림, Gmail·Discord·범용 webhook, 선택적 Web Push 및 주간 보고서

## 시작하기

systemd가 활성화된 Ubuntu on WSL2와 Python 3.11 이상이 필요합니다.
웹은 선택 사항이며 Tailscale 로그인·HTTPS Serve와 관리자 설치가 필요합니다.

```bash
git clone https://github.com/jihoon22-lee/wsl-resource-guard.git
cd wsl-resource-guard
bash install-user.sh
export PATH="$HOME/.local/bin:$PATH"
wrg doctor
wrg status
```

설치기는 사용자 서비스를 시작합니다. 이미 시스템 guard가 설치되어 있으면 사용자 설치를 거부합니다.
부팅 서비스 승격과 웹 설치는 [설치 안내](docs/installation.md)를 먼저 읽으세요.
릴리스 설치에는 프로젝트가 업로드한 `wsl-resource-guard-<version>.tar.gz`를 사용합니다.

## 문서

- [설치·업데이트·제거](docs/installation.md)
- [웹과 CLI 사용법](docs/usage.md)
- [설정](docs/configuration.md)
- [구조와 권한 경계](docs/architecture.md)
- [운영·복구·문제 해결](docs/operations.md)
- [개발·검증·릴리스](CONTRIBUTING.md), [보안 제보](SECURITY.md), [변경 기록](CHANGELOG.md)

## 지원 조건과 제한

단일 Linux 소유자와 개인 Tailnet을 대상으로 합니다. 공개 인터넷용 관리 화면이 아니며 Funnel은 지원하지 않습니다.
태그 장치, 설치 중 소유자·Tailnet 주소 변경은 자동 마이그레이션하지 않습니다.
Windows interop가 없으면 Windows 관측·토스트를 사용할 수 없습니다. Docker·외부 알림은 선택 사항입니다.

PSI 미관측은 실제 0과 구분합니다. 이전 버전이 0으로 남긴 기록의 관측 여부는 복원할 수 없습니다.
설치는 완전한 원자적 전환이 아니며 복구 백업과 이전 가상환경을 보존합니다.
GitHub Actions의 fixture 검증과 실제 WSL 설치 검증은 별도입니다.
실제 검증 환경과 남은 제약은 [공개 준비 기록](workthrough/2026-10-07-public-release.md)에 기록합니다.

## English overview

WSL Resource Guard monitors memory pressure, swap, disks, and LLM/MCP process trees.
An optional private Tailscale dashboard controls explicitly registered systemd and Compose services.
Requires systemd-enabled Ubuntu on WSL2 and Python 3.11+. Designed for one Linux owner;
not an Internet-facing or multi-tenant administration service. Documentation is primarily Korean.

Licensed under [MIT](LICENSE).
