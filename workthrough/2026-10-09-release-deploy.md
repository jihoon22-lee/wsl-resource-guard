# 2026-10-09 PR 검증·0.1.2 릴리스·운영 배포

## 범위와 진행 상태

사용자 요청에 따라 사용성 개선을 원격 push하고 PR/CI/병합, 문서와 버전 갱신, 태그/Release 게시, 운영 배포까지 진행한다. 운영 배포는 GitHub Actions의 자동 배포가 아니라 검증 후 수동 실행이다. PR 병합 후 0.1.1 태그 검증이 추가 보안 결과로 게시를 차단했다. 공개 태그를 변경하지 않고 수정한 0.1.2의 전체 검증·Release 게시·운영 적용을 완료했다.

## PR 검증에서 발견한 문제

- [PR #8](https://github.com/jihoon22-lee/wsl-resource-guard/pull/8)의 [첫 CI](https://github.com/jihoon22-lee/wsl-resource-guard/actions/runs/37940708021)에서 Python 3.11–3.14, 브라우저, guard lifecycle, 의존성 감사, JS CodeQL, artifact 왕복 검사는 통과했다. 권한 경계와 Python CodeQL은 실패했다.
- 실제 권한을 낮춘 worker의 `config-result` 읽기는 권한 거부를 빈 결과로 숨기지 않고 OSError로 보고한다. 기존 root 전용 테스트의 기대값을 이 동작에 맞췄다. root-only 파일이 노출되지 않는 검증은 유지한다.
- CodeQL은 상태 snapshot의 `ControlError`/`TimeoutExpired` 문자열이 API까지 전달되는 경로를 발견했다. Compose와 systemd 조회 실패에 고정된 사용자 메시지를 사용하며 상태는 `unknown`, 메모리는 미관측으로 유지한다. 두 종류 예외의 내부 인수가 반환 JSON에 나타나지 않는 회귀 검사를 추가했다.
- 로컬 전체 단위 검사: 440개 중 438개 통과, root 전용 2개 skip. WSL root로 격리 fixture의 권한 전환 검사를 별도 실행하여 2개 모두 통과했다. fixture 실패 주입의 traceback과 기존 ResourceWarning은 테스트 실패로 집계하지 않는다.

## PR 병합과 버전 준비

- 보완 커밋 `6e340cf`의 [CI 37941611492](https://github.com/jihoon22-lee/wsl-resource-guard/actions/runs/37941611492)는 13개 job 모두 success다. Python 3.11–3.14, 실제 root 권한 경계, guard lifecycle, Chromium·WebKit/규모 회귀, 의존성 감사, 두 언어 CodeQL, 압축본 왕복 검사를 포함한다.
- PR #8을 GitHub merge commit `bcdd55f`로 병합하고 로컬 main에도 no-ff 통합했다.
- 프로젝트·모듈 버전을 0.1.1로 갱신하고 uv 0.12.23으로 잠금의 프로젝트 버전을 맞췄다. 잠금 diff는 프로젝트 버전 한 줄뿐이며 의존성 버전은 그대로다. `uv lock --check`, 릴리스 메타데이터 단위 5개와 공개정보·문서 링크 검사가 통과했다. CHANGELOG에 0.1.0 이후 변경을 모으고 이 기록을 압축본 allowlist에 포함했다.
- 운영 직전 설정·등록·프로세스 기준선을 다시 기록하고, 아래 최종 릴리스와 같은 clean 커밋을 기존 설치기로 수동 적용했다.

## 전체 릴리스 검사에서 추가 발견

- `854972d`의 `v0.1.1` [Release 실행 37942369131](https://github.com/jihoon22-lee/wsl-resource-guard/actions/runs/37942369131)은 Python CodeQL의 추가 오류 노출 결과로 게시가 차단됐다. PR의 성공을 전체 릴리스 보안 검증 완료로 취급하지 않는다. `v0.1.1` 태그는 그대로 남기고 수정본을 0.1.2로 구분한다.
- 전체 SARIF에서 설정 검증/파일 읽기 경고와 디스크 용량 오류의 예외 문자열이 API까지 전달됨을 확인했다. 고정된 경고·오류로 바꾸고 설정 읽기 실패는 예외 종류만 남긴다. 같은 구조의 VHD 오류도 고정 메시지로 처리한다. 기본값 사용, unmounted/error 상태, 이전 정상 표본의 보존은 유지한다.
- 내부 문자열을 주입해 설정·디스크·VHD 반환값에 포함되지 않는 회귀 검사를 추가했다. 전체 단위 442개 중 440개 통과, 기존 root 전용 2개 skip이다. 수정하지 않은 권한 경계는 앞선 실제 root 검사에서 2개 통과했다.
- 제품 수정과 버전 갱신 후 main의 전체 CI 성공을 확인하고 v0.1.2 태그를 생성했다. v0.1.1 실행의 package/publish는 모두 skipped이며 Release는 생성되지 않았다.

## 최종 릴리스 검증과 게시

- 제품 수정 `9fbc563`을 no-ff 병합한 `f4d971dbb3ed1cdc1483580adb1442424d051021`이 최종 릴리스·배포 기준이다. [main CI 37942899715](https://github.com/jihoon22-lee/wsl-resource-guard/actions/runs/37942899715)의 13개 job이 모두 success인 것을 확인한 후 `v0.1.2` 태그를 push했다.
- [Release 37943559926](https://github.com/jihoon22-lee/wsl-resource-guard/actions/runs/37943559926)의 15개 job 모두 success다. 전체 검사·의존성 감사·두 언어 CodeQL·실제 권한 경계·설치 수명주기·Chromium/WebKit·규모 회귀·압축본 왕복·최종 게시 입력 검증을 통과했다.
- [v0.1.2 Release](https://github.com/jihoon22-lee/wsl-resource-guard/releases/tag/v0.1.2)는 정식 공개 상태이며 `wsl-resource-guard-0.1.2.tar.gz`와 `SHA256SUMS`를 제공한다. CHANGELOG와 제약을 릴리스 설명에도 반영했다.
- 로컬에서 동일 커밋의 압축본을 두 번 생성해 바이트 일치를 확인했다. 게시 파일을 다시 내려받아 SHA256SUMS·release verify·manifest의 커밋/버전, 로컬 검증본과의 바이트 일치를 확인했다. README가 참조하는 새 작업 기록도 allowlist에 포함돼 있다.

## 실제 운영 적용과 확인

- 2026-10-09 23:27 KST에 직전 기준선을 기록하고 clean `f4d971d`에서 `install-services.sh` → `install-root.sh` 순서로 수동 적용했다. 웹·컨트롤러·시스템 guard만 갱신하고 새 guard의 첫 표본 기록을 설치기가 확인했다.
- 웹/CLI의 BUILD 스탬프는 모두 위 커밋·dirty=false이며, 스탬프 외 제품 파일 각각 29개/21개가 소스와 바이트 단위로 일치한다. 의존성은 기존 잠금 버전을 유지했다.
- 세 서비스 active/running, NRestarts=0이며 새 guard의 PID/start_ticks와 실제 state 작성자가 일치한다. 서로 다른 시점의 새 표본과 추가 history를 확인했고 최종 표본 나이는 11.0초였다. 배포 이후 세 유닛의 error 우선순위 journal은 0건이다.
- 실제 Tailscale HTTPS에서 browser_smoke를 실행해 전체 화면 이동·필터·PC/390px 모바일·수동/자동 갱신·숨김 탭 중지·동시 요청 방지를 확인했다. JavaScript 오류는 없고 가로 넘침 검사도 통과했다. PC/모바일 화면 이미지를 직접 확인했다. viewport 자동화이며 실제 휴대전화 검증이 아니다.
- 실제 인증 API의 설정·현재 설정 요청·서비스·monitor·summary가 200을 반환했다. 없는 설정 요청 ID는 404/config_not_found로 구분했다. 운영 설정 변경이나 실제 알림 발송은 시험하지 않았다.
- 배포 직전과 비교하여 사용자 설정·비밀값·registry·웹 접근 설정의 해시 및 Tailscale serve 설정이 동일하다. 무관한 실행 서비스 20개의 PID/시작 정보와 Docker 컨테이너 6개의 실행 정보도 동일하다.
- 화면에 Windows 토스트 전달 오류가 남아 있다. 이전 guard가 작성한 두 설치 복구 상태에도 같은 오류가 있고 마지막 알림은 배포 전이다. PowerShell 호출의 WSL 연결 오류이며 이번 배포의 수집/웹 성공과 별개다. 실제 토스트 재수신을 확인하지 않았으므로 복구됐다고 주장하지 않는다. 후속으로 Windows/WSL 연결 상태와 지정한 기기의 실제 수신을 확인해야 한다.

## 정리와 보존

- 이번 PR/버전/보완 작업 브랜치는 main 포함 여부를 확인하고 삭제했으며 원격 PR 브랜치도 원격 main에 포함됨을 확인 후 삭제했다. 별도 worktree는 만들지 않았다.
- 브라우저와 검증 프로세스는 종료했다. 임시 압축본·SARIF·배포/검사 로그·기준선·화면 이미지·검사용 uv와 이번 검사에서 만든 캐시를 정리했다. 개인 운영 자료나 비밀값을 공개 기록에 포함하지 않는다.
- 복구용 백업을 임시 파일로 취급하지 않는 AGENTS.md 기준에 따라 이번 설치가 만든 복구 디렉터리 4개와 rollback에 필요한 이전 웹 가상환경 1개를 보존한다. 현재 웹 환경·기존 복구 자료·사용자 설정·운영 이력·개발 가상환경도 유지한다.
- 이 최종 결과 기록은 제품 배포 후 문서 변경이다. 문서/공개정보 검사 후 작업 브랜치에 커밋하고 main에 no-ff 병합·push하며 마지막 문서 브랜치도 정리한다. 문서 커밋 때문에 제품을 다시 배포하지 않는다.

## 알려진 제약

대규모 목록의 렌더링/정렬 성능 목표 미달과 실제 휴대전화·OS Push 수신 미검증은 [사용성 검증 기록](2026-10-09-usability-remediation.md)을 따른다. 릴리스 게시나 운영 적용이 이 제약의 해소를 뜻하지 않는다.
