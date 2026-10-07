# 공개 준비 구현·검증 기록 — 2026-10-07

## 범위와 이력

새 공개 Git 이력, 한국어 문서, MIT, 선택적 개발 PR·CI, 필수 릴리스 검사를 적용합니다.
기존 비공개 체크아웃·bare 저장소는 보관하며 공개 저장소의 과거 이력에 포함하지 않습니다.
공개 이후 개발·push는 새 공개 체크아웃을 기준으로 합니다. 운영 WSL 자동 배포는 없습니다.

## 구현 및 교차 검토

- 소유자 UID·기본 GID로 고정 작업 프로세스를 실행하고 보조 그룹을 제거합니다. 사용자 파일은 권한을 낮춘 뒤 제한된 크기의 일반 파일로 읽습니다.
- 설정·상태·이력·리포트·로그·VHD 조회에 공통 권한 경계를 적용하고 응답 필드를 제한했습니다. 비객체 종료 요청은 400입니다.
- 설치 owner/origin 사전 검증, 사용자 권한 쓰기, 고정 경로 가상환경, 변경 journal·실행 상태 복구와 guard 중복 방지를 구현했습니다.
- PSI null/수집 상태를 CLI·웹·알림·이력·재시작에 연결했습니다. RAM 경보와 겹칠 때 PSI 회복 시간을 독립 추적하도록 교차 검토에서 보완했습니다.
- 런타임 자동 실행 복원과 실패한 guard의 신규 출력 보존을 보완했습니다.
- Python 3.12에서 접근 불가 symlink의 exists 검사가 worker 전체를 중단하던 문제를 실제 권한 시험으로 찾아 수정했습니다.
- 설치 시 릴리스 manifest에 없는 추가 코드도 거부합니다. 강제 디스크 갱신 후 이전 캐시가 돌아오던 문제를 수정했습니다.
- README·설치·사용·설정·구조·운영 문서를 통합했습니다. 과거 계획과 작업 기록은 비공개 보관본에만 남깁니다.

## 검증 증거

- 통합 Python 3.12 전체 CI profile: 336개 통과, 예상하지 못한 skip 없음. 실 root fixture는 별도 작업에서 실행합니다.
- 고정 Playwright의 PC·모바일 offline dashboard 검사를 통과했습니다.
- 폐기용 WSL의 Python 3.12 root: 권한 격리·소유자 데이터 검사 5개 통과, skip 없음.
- 폐기용 WSL: 사용자 신규 설치, 시스템 guard 전환, 역순 사용자 설치 거부, 시스템 guard 업데이트 확인.
- 단위 실패 주입: owner/origin 불일치, 의존성 준비·동기화 실패, rollback, nullable PSI·혼합 이력·재시작·중복 writer 경계 검증.
- runtime/dev 잠금 의존성 audit 통과. 발견된 Werkzeug·cryptography 취약 버전을 수정했고 포괄 예외를 넣지 않았습니다.
- JavaScript 구문, shellcheck, actionlint 검사 통과.

## 격리와 제한

테스트 배포판은 운영 WSL과 별도로 만들었습니다. WSL의 Windows 실행 등록·네트워크는 배포판 간 공유될 수 있습니다.
테스트 배포판 시작 시 공용 Windows 실행 등록이 사라지는 현상을 복구하고 해당 배포판의 binfmt 갱신을 차단했습니다.
운영 웹 포트와 충돌한 설치는 사전 검사에서 중단됐고 웹 수명주기 검증에는 별도 network namespace를 사용합니다.
운영 웹·guard 프로세스를 재시작하거나 새 코드로 배포하지 않았습니다.

실제 로그인된 Tailnet의 소유자 접근과 외부 알림 수신은 이번 격리 fixture 검증과 구분합니다.
격리 WSL의 실제 systemd 수명주기 10개 수용 항목이 통과했습니다.
중복 guard 거부, runtime 자동 실행·신규 JSONL을 포함한 guard 실패 복구, 시스템 업데이트,
웹 업데이트의 이전 가상환경·shebang 보존, 의존성 준비 실패 시 기존 웹 유지,
owner 불일치 무변경, root 전용 부모 symlink에 대한 사용자 UID 쓰기·chown 차단,
웹 재시작 후 실패 시 이전 환경·health 복구, 외부 서비스 PID·자동 실행 보존,
수동 제거 후 소유 유닛 제거와 설정·state·registry·백업 보존을 확인했습니다.
웹 복구 첫 시험은 worker 기동 전 단발성 요청으로 실패했고, 10초 제한 health polling으로 해당 항목을 재검증해 통과했습니다.
Tailscale 명령은 격리된 fixture이며 실제 Tailnet 연결 증거로 간주하지 않습니다.

같은 커밋으로 두 번 만든 tar.gz가 byte-for-byte 일치하고 추출·파일 목록·해시·CLI 실행 검증이 통과했습니다.
공개 대상 101개 파일의 Gitleaks 검사도 통과했습니다. 공개 RFC 8291의 정확한 시험값과 두 fixture 경로만 예외이며 운영 비밀값의 예외는 없습니다.
최종 GitHub 검증 결과는 아래에 기록합니다.

## GitHub 초기 실행 보완

새 공개 이력의 첫 CI에서 Python 3.11–3.14·root 경계·브라우저·의존성·공개정보 검사가 통과했습니다.
실제 CodeQL SARIF는 확장 component에 규칙을 담아 최초 gate가 차단했습니다. component/index/id를 검증하는 reader를 보완했습니다.
웹 화면의 동적 함수 선택은 고정 switch로 바꿨고 비밀이 아닌 fixture 변수명도 실제 역할에 맞췄습니다.
원본 high/error SARIF가 수정된 판정기에서도 차단되는 것을 확인했으며 취약점 무시 목록은 추가하지 않았습니다.
수정 후 전체 단위 337개 및 PC·모바일 브라우저 검사 통과, 추가 gate 경계 검사 7개 통과.
잘못된 버전 태그는 실제 Release workflow에서 사전 차단됐고 Release 미생성을 확인한 뒤 시험 태그를 제거했습니다.

## 최종 공개·릴리스 검증

- 새 공개 저장소: [jihoon22-lee/wsl-resource-guard](https://github.com/jihoon22-lee/wsl-resource-guard). 최초 공개 이력은 이전 저장소와 연결되지 않습니다.
- [전체 CI](https://github.com/jihoon22-lee/wsl-resource-guard/actions/runs/37639459886) 통과. Python 3.11·3.12·3.13·3.14에서 각각 339개 단위 검사, 별도 root 권한 fixture, PC·모바일 브라우저, 문서·비밀값·의존성·셸·워크플로 검사가 통과했습니다.
- Python·JavaScript CodeQL SARIF 결과 각각 0건. 분석 성공 뒤 결과 판정기도 통과했습니다.
- [릴리스 실행](https://github.com/jihoon22-lee/wsl-resource-guard/actions/runs/37639805472)이 태그 커밋 `dca23ffc3ca8e8a93d456676ae4623286cfc2436`에서 전체 검사를 다시 실행하고 동일 산출물을 게시했습니다.
- [v0.1.0](https://github.com/jihoon22-lee/wsl-resource-guard/releases/tag/v0.1.0)에서 다시 다운로드한 압축본의 SHA-256은 `08c89ec7dd72500747d2e0d5d51d06e6dd3fa0e0e91972ce0443e8429ef732ca`입니다. 체크섬·파일 목록·manifest·커밋·CLI 버전 0.1.0을 확인했습니다.
- 같은 커밋을 로컬에서 재빌드한 압축본과 게시된 파일이 byte-for-byte 일치했습니다.
- 잘못된 버전의 시험 태그는 [릴리스 사전 검사](https://github.com/jihoon22-lee/wsl-resource-guard/actions/runs/37638678699)에서 차단됐습니다. Release가 없음을 확인하고 시험 태그를 삭제했습니다.
- 비공개 취약점 제보, secret scanning·push protection을 활성화했습니다. main의 필수 승인·필수 CI 규칙은 설정하지 않았습니다.

## 정리 및 운영 상태

작업용 worktree 3개와 병합된 구현 브랜치, 폐기 WSL 배포판, 전용 Docker container/image,
테스트 서버·프로세스와 로컬 테스트 산출물을 정리했습니다. 마지막 검증 기록 브랜치도 main 병합 후 제거합니다.
기존 비공개 보관본, 운영 데이터·복구 백업·기존 가상환경, 이번 작업 이전의 사용자 산출물은 보존합니다.
운영 웹·컨트롤러·guard는 이전 PID·시작 시각 그대로이며 재시작·새 버전 배포하지 않았습니다.
폐기 WSL 제거 후 Windows 실행 등록을 다시 복구하고 PowerShell 실행을 확인했습니다.

이후 개발은 새 공개 체크아웃에서 이어갑니다. 운영 적용은 이 릴리스의 별도 설치 작업입니다.
실제 로그인된 Tailnet 접근과 외부 알림 수신은 이번 변경으로 다시 검증하지 않았으며,
격리 WSL의 실제 systemd·파일 권한 검증 및 fixture 기반 인증 시험과 구분합니다.
