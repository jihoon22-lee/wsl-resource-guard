# WSL Resource Guard

WSL2의 메모리·swap·PSI·디스크와 LLM/MCP 프로세스를 감시하고, 개인 Tailscale 화면에서 등록한 서비스를 관리합니다.
토큰 사용량·구독 인증·비용 통계는 이 프로젝트의 범위가 아닙니다.

## 주요 기능

- RAM·swap 입출력·PSI·OOM·Windows 볼륨·WSL 디스크 관측과 이력, 드라이브별 디스크 경보 기준
- Windows 호스트의 물리 메모리·커밋·페이지 파일·WSL VM 점유·`.wslconfig` 한도 관측과 호스트 메모리 부족 경보
- CPU·IO 압력과 GPU 메모리 표시
- Codex, Claude, OpenCode, Gemini, Aider, Devin, Antigravity 세션 및 MCP 프로세스 분류
- 기존 systemd·Docker Compose 서비스 등록, 실행 상태와 자동 실행의 독립 제어
- 소유자만 접근하는 Tailscale 웹 대시보드, PC·모바일 화면
- 푸시·Gmail에서 경보별 원인·실제 작업·공유 범위로 이동하고 영향 확인 후 개별 조치
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
- [저장 형식과 컨트롤러 요청](docs/data-formats.md)
- [운영·복구·문제 해결](docs/operations.md)
- [Paseo 서비스 등록과 연결 검사](docs/paseo.md)
- [예정 작업과 미해결 항목](docs/roadmap.md)
- [개발·검증·릴리스](CONTRIBUTING.md), [보안 제보](SECURITY.md), [변경 기록](CHANGELOG.md)

## 지원 조건과 제한

단일 Linux 소유자와 개인 Tailnet을 대상으로 합니다. 공개 인터넷용 관리 화면이 아니며 Funnel은 지원하지 않습니다.
태그 장치, 설치 중 소유자·Tailnet 주소 변경은 자동 마이그레이션하지 않습니다.
Windows interop가 없으면 Windows 관측·토스트를 사용할 수 없습니다. Docker·외부 알림은 선택 사항입니다.

PSI 미관측은 실제 0과 구분합니다. 이전 버전이 0으로 남긴 기록의 관측 여부는 복원할 수 없습니다.
설치는 완전한 원자적 전환이 아니며 복구 백업과 이전 가상환경을 보존합니다.
GitHub Actions의 fixture 검증과 실제 WSL 설치 검증은 별도입니다.
Paseo 연결 검사는 npm 0.12.x·기본 home·loopback TCP를 대상으로 하며, 릴레이 왕복 성공과 모바일 단말 접속은 구분합니다. 기존 실행 주체에서 시스템 서비스로 전환할 때에는 작업 중단과 재연결이 필요합니다.
pidfd 미지원 환경에서는 세션 종료 기능을 사용할 수 없습니다. 시스템 guard의 사용자 지정 상태 경로는 소유자 HOME 아래 정규 디렉터리로 제한됩니다.
개발 경로는 하나를 사용하며 기존 실행 사본은 설치기가 관리합니다. 의존성은 `pyproject.toml`과 `uv.lock`에서 관리합니다.
설정은 한 건씩 적용하며 오래된 요청의 결과는 보관되지 않을 수 있습니다. 서비스 500개·세션 2,000개 규모에서는 정렬·렌더링이 느릴 수 있습니다.
PC나 WSL이 꺼져 있으면 알림을 보낼 수 없으며 외부 상시 감시는 없습니다.

Windows 호스트 상태는 WSL 안에서 PowerShell을 주기마다 한 번 실행해 얻습니다(기본 60초, 한 번에 1~3초). 호스트 메모리 경보는 모의 값으로 표본 → 사건 → 전달 경로만 확인했으며, 실제 Windows 메모리 부족 상황에서의 발생과 수신은 확인하지 못했습니다. interop이 PowerShell을 기동하지 못하는 일이 간헐적으로 있고(실측 9회 중 1회, 원인 미확인), 한 번 다시 시도한 뒤에도 실패하면 마지막 관측값과 사유를 표시합니다. 호스트를 관측하지 못하는 동안에는 호스트 경보를 새로 내거나 회복으로 닫지 않으며, 관측 불가 자체를 알리는 경보는 없습니다(화면과 `wrg status`에 표시). 기본 이름(`ext4.vhdx`)이 아닌 가상 디스크는 `wsl_vhd_path`로 지정해야 합니다. GPU 값은 `nvidia-smi`가 있을 때만 표시합니다.

부팅 복구는 `wrg-services-restore.service` 하나가 등록 정보의 자동 실행 설정대로 수행합니다. 외부 진입점(예: Windows 로그온 작업)은 앱 목록 없이 이 unit을 시작하며, 로그온 트리거는 로그인 전 무인 기동이 아닙니다. PC·WSL 전체 재부팅을 통한 복구는 실증하지 않았습니다.

Codex 앱 서버 같은 공용 실행기를 개별 대화로 취급하지 않습니다. 대화 제목은 명시적 작업 ID와 로컬 메타데이터가 연결될 때만 표시하며, 제공되지 않은 제목·활동·저장/재개 가능성은 미확인으로 남깁니다. 공용 서버 전체 종료는 일반 세션 조치에서 차단합니다. 0.2.4 운영 확인에서 Gmail과 Android Chrome의 실제 알림 수신을 확인했으며, Chrome은 휴대전화 홈 화면에서도 일반 우선순위 푸시를 받았습니다. 같은 휴대전화의 Samsung Internet은 알림 허용·재등록 후에도 미수신이어서 원인을 확정하지 못했습니다. 절전·네트워크 전환과 다른 단말 환경은 별도 확인이 필요합니다.

공개 릴리스는 [GitHub Releases](https://github.com/jihoon22-lee/wsl-resource-guard/releases/latest)에서 확인합니다.

## English overview

WSL Resource Guard monitors memory pressure, swap, disks, and LLM/MCP process trees.
An optional private Tailscale dashboard controls explicitly registered systemd and Compose services.
Requires systemd-enabled Ubuntu on WSL2 and Python 3.11+. Designed for one Linux owner;
not an Internet-facing or multi-tenant administration service. Documentation is primarily Korean.

Licensed under [MIT](LICENSE).
