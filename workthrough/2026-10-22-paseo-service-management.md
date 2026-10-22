# Paseo 서비스 관리와 연결 검사

## 요청과 범위

Paseo를 기존 WRG 서비스 관리에 추가하되 현재 연결을 보호한다.
서비스 템플릿·등록 정보·연결 검사·PC/모바일 표시를 구현했다.
초기 구현 시에는 연결 보호를 위해 시스템 서비스 전환을 보류했다. 이후 사용자 승인에 따른 운영 적용 결과는 아래에 기록한다.

## 구현

- `packaging/examples/paseo-daemon.service.in`: 소유자 계정의 foreground 데몬, 기존 home 유지, 종료 후 재시작과 시작 횟수 제한, cgroup 메모리 측정. 메모리 하드 제한은 설정하지 않음.
- 기존 systemd 검색·등록·실행·자동 실행·로그 기능 재사용. Paseo는 별도 설치된 앱이며 WRG가 프로그램을 설치하거나 제거하지 않음.
- `paseo.py`: 설치된 npm client의 암호화 프로토콜을 사용해 HTTP, 로컬 WebSocket ping/pong, 릴레이 ping/pong을 확인. 클라이언트마다 새 ID를 사용해 기존 세션을 재사용하지 않음.
- `owner_worker.py`: 무인자 고정 검사만 허용하며 등록 소유자로 권한을 내린 뒤 실행. V8 heap 96 MiB, 검사 작업의 가상 주소 공간만 2 GiB 허용. 총 실행 시간 제한과 공개 결과 스키마 적용.
- 컨트롤러의 독립 60초 sampler. 등록된 active 시스템 유닛만 검사하며 읽기 실패는 미확인. 서비스 InvocationID 변경 및 180초 경과 시 이전 정상 결과를 사용하지 않음.
- UI: HTTP/로컬/릴레이를 각각 표시하고 꺼짐·실패·미확인·오래됨 구분. 관리 창에 자동 재시작 횟수와 하위 작업 중단 영향 표시. Paseo의 재시작은 기존 관리 확인 창을 거치도록 빠른 조작 버튼을 생략.
- 연결 검사 실패는 요약에 표시하며 자동 재시작·알림 전송은 하지 않음.

## 검증

- `.venv/bin/python -m unittest discover -s tests -q`: 502개, 통과(기존 조건부 skip 2개). 의도적인 설치 실패/복구 테스트 로그와 기존 ResourceWarning이 출력되었으나 실패는 없음.
- 최종 상태 표시 보완 후 관련 단위 테스트 재실행: 컨트롤러 82개, Paseo 6개 통과.
- `node --check wsl_resource_guard/web/app.js`: 통과.
- `.venv/bin/python tests/browser_offline.py`: PC·모바일·태블릿 회귀 통과. Paseo의 로컬 정상/릴레이 실패, 미확인, 꺼짐, 오래된 결과와 관리 창을 데스크톱·390px 모바일에서 확인. 조회가 서비스 변경 요청을 보내지 않음.
- `.venv/bin/python tests/integration_paseo_service.py`: 임시 사용자 유닛에서 자동 실행 설정과 실제 실행의 독립성, 비정상 종료 복구, 재시작·중지 시 분리된 자식 프로세스 정리, 수동 중지 후 유지 확인. 실서비스를 사용하지 않음.
- 실제 운영 Paseo 0.12.0에 owner worker 프로토콜·메모리 제한을 적용한 읽기 검사: HTTP, 로컬, 릴레이 모두 정상. 로컬 약 217ms, 릴레이 약 495ms. 이 검사는 모바일 실기기 검증을 대신하지 않음.
- 운영 환경용으로 렌더링한 유닛에 `systemd-analyze verify` 통과.
- 작업 전후 기존 Paseo 사용자 서비스의 MainPID 동일, NRestarts=0 확인.

## 적용 상태와 남은 제약

2026-10-22 20:22 KST에 사용자 승인과 터미널의 관리자 인증 후 운영 적용을 완료했다.
검증 커밋 `957fe4015ea4`의 웹·컨트롤러·CLI reader·시스템 Guard를 기존 설치기로 배포했다.
사용자 임시 Paseo를 중지하고 영구 시스템 `paseo-daemon.service`로 전환했으며 자동 시작을 켜고 WRG에 등록했다.
전환 전후 Paseo 서버 ID와 config 해시가 일치한다. 복구 백업은 각 설치기의 root/owner 백업과 `wrg-paseo` 백업으로 보존했다.

운영 WRG의 검사 결과는 HTTP·로컬·릴레이 모두 정상(로컬 184ms, 릴레이 515ms)이었다.
네 시스템 유닛은 active·NRestarts=0이고, 이전 사용자 Paseo는 inactive다.
설치 BUILD는 root/owner 모두 `957fe4015ea4`·dirty=false이며 `wrg doctor`의 코드 일치 검사를 통과했다.
실제 Tailnet 웹의 1440px/390px 화면에서 등록·자동 실행·정상 배지·관리 창을 읽기 전용으로 확인했다.
다만 이 검사는 텍스트의 실제 표시 영역까지 검증하지 않아 사용자가 제보한 이름 잘림을 놓쳤다.
원인과 수정·추가 검증은 `2026-10-22-mobile-service-cards.md`에 기록한다.
WSL 시작 시 systemd 자동 실행은 설정했지만 실제 Windows/WSL 재부팅은 수행하지 않았다.

지원 범위는 기본 home·loopback TCP·npm 0.12.x client API다. 다른 배포와 API 변경은 별도 검증이 필요하다.
실제 휴대폰 재접속과 WSL 재부팅 복구는 아직 검증하지 않았다.
서비스 장애 푸시 알림, 릴레이 실패에 따른 자동 재시작, 사용자 systemd 유닛 관리 확장은 포함하지 않았다.

## 완료 절차

기능 브랜치에서 검증 후 로컬 main으로 병합한다. 원격 push·릴리스는 요청 범위에 포함하지 않는다.
격리 테스트의 유닛·프로세스·임시 디렉터리는 테스트 finally에서 정리했다.
운영 데몬과 후속 설치에 사용할 준비 유닛은 보존한다.
