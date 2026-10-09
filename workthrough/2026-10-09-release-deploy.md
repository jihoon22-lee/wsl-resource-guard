# 2026-10-09 PR 검증·0.1.1 릴리스·운영 배포

## 범위와 진행 상태

사용자 요청에 따라 사용성 개선을 원격 push하고 PR/CI/병합, 문서와 버전 갱신, 태그/Release 게시, 운영 배포까지 진행한다. 운영 배포는 GitHub Actions의 자동 배포가 아니라 검증 후 수동 실행이다. 현재 PR 검증 중이며 릴리스와 운영 적용은 아직 실행하지 않았다.

## PR 검증에서 발견한 문제

- [PR #8](https://github.com/jihoon22-lee/wsl-resource-guard/pull/8)의 [첫 CI](https://github.com/jihoon22-lee/wsl-resource-guard/actions/runs/37940708021)에서 Python 3.11–3.14, 브라우저, guard lifecycle, 의존성 감사, JS CodeQL, artifact 왕복 검사는 통과했다. 권한 경계와 Python CodeQL은 실패했다.
- 실제 권한을 낮춘 worker의 `config-result` 읽기는 권한 거부를 빈 결과로 숨기지 않고 OSError로 보고한다. 기존 root 전용 테스트의 기대값을 이 동작에 맞췄다. root-only 파일이 노출되지 않는 검증은 유지한다.
- CodeQL은 상태 snapshot의 `ControlError`/`TimeoutExpired` 문자열이 API까지 전달되는 경로를 발견했다. Compose와 systemd 조회 실패에 고정된 사용자 메시지를 사용하며 상태는 `unknown`, 메모리는 미관측으로 유지한다. 두 종류 예외의 내부 인수가 반환 JSON에 나타나지 않는 회귀 검사를 추가했다.
- 로컬 전체 단위 검사: 440개 중 438개 통과, root 전용 2개 skip. WSL root로 격리 fixture의 권한 전환 검사를 별도 실행하여 2개 모두 통과했다. fixture 실패 주입의 traceback과 기존 ResourceWarning은 테스트 실패로 집계하지 않는다.

## 알려진 제약

대규모 목록의 렌더링/정렬 성능 목표 미달과 실제 휴대전화·OS Push 수신 미검증은 [사용성 검증 기록](2026-10-09-usability-remediation.md)을 따른다. 릴리스 게시나 운영 적용이 이 제약의 해소를 뜻하지 않는다.
