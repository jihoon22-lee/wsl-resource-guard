# uv 전환과 Dependabot 업데이트

## 범위와 순서

1. Actions PR #3–#7 통합, 게시 없는 artifact 왕복 검사.
2. Playwright #2와 Gunicorn #1 통합, 해시 잠금 및 격리 웹 검증.
3. 개발·CI의 uv 전환, 설치기 신뢰 경계와 실패 복구 검증.
4. 독립 검토, 전체 CI, main 병합·push와 임시 산출물 정리.

운영 서비스는 재시작·배포하지 않는다. 기존 가상환경과 복구 백업을 보존한다.

## 결정

- 대화에서 승인한 계획을 이 기록에서 추적한다. 별도 내부 계획 파일을 공개하지 않는다.
- requirements 입력과 해시 잠금을 유지한다. uv.lock 도입은 이번 범위에 없다.
- Actions artifact 검증과 릴리스가 같은 tar.gz 형식·검증기를 사용한다.
- 의존성 PR들의 잠금 파일 충돌은 입력 명세와 기존 고정 버전을 기준으로 해결한다.

## 실행 기록

- Actions 다섯 PR을 공개 main의 최신 보안 수정 위에 통합했다.
- upload-artifact v7 → download-artifact v8을 별도 job에서 실행하고,
  체크섬·메타데이터·동일 커밋 재빌드 바이트를 비교하는 검사를 추가했다.
- 검증과 최종 정리 결과는 작업 진행 중 아래에 누적한다.

### 의존성과 실행 환경

- Playwright 1.63 잠금에서 누락된 pip-api의 pip 의존성을 uv compile로 복원했다.
  깨끗한 uv 환경에서 44개 패키지 설치·uv pip check, 339개 단위 검사와 PC·모바일
  offline 브라우저 검사가 통과했다.
- Gunicorn 26.2에서 339개 단위 검사와 실제 TCP·Unix 소켓 검사 3개가 통과했다.
  health, 헤더 위조 거부, Host·신원·CSRF, SSE, 모호한 Content-Length 거부, 정상 종료를 확인했다.
- root 설치용 uv는 0.12.23 공식 archive의 고정 SHA-256을 확인하고 root 소유 임시 경로에서 실행한다.
  고정 Python 경로, 빈 명시적 uv 설정, 새 HOME/XDG·캐시, 제한 환경, wheel-only 해시 설치를 사용한다.
  기존 환경을 덮어쓰지 않고 준비된 환경의 Python/Gunicorn을 웹 계정으로 검사한다.
- 환경 격리·해시·링크 거부·기존 환경 보존 테스트를 먼저 실행해 미구현 실패를 확인했고,
  구현 후 신규 4개와 전체 343개 단위 검사가 통과했다.
- 결정: uv 자체는 매 설치마다 검증·임시 실행 후 정리한다. 지속되는 사용자/root uv 설치를
  신뢰하지 않기 위함이며, 설치 시 GitHub Releases와 PyPI 직접 연결이 필요하다.
- 결정: 요구한 무중단 원칙에 따라 이번에는 운영 배포·새 릴리스 태그를 만들지 않는다.
  로컬 transport와 격리 컨테이너·호스팅 CI로 설치 및 의존성 변경을 검증한다.
- GitHub의 새 브랜치 git push가 서버 오류로 실패했다. API로 공개 main 기반 ref를 만든 후
  fast-forward push가 성공했고 작업 브랜치에서 CI를 수동 실행했다.
