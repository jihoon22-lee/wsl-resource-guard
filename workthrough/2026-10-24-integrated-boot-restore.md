# 2026-10-24 부팅 복구 통합 · 시작 절차 연결

## 범위

Windows 재부팅 후 로그온 → WSL 시작 → 등록된 운영 서비스 복구 경로를 하나로 정리한다.
서비스별 자동 실행은 Resource Guard 등록 정보와 각 systemd unit의 enable 상태만 기준으로 삼는다.
Windows 진입점은 별도 비공개 복구 키트(`E:\recovery`)이며 그 저장소의 변경·기록은 해당 저장소에 따로 남긴다.
기준 main은 `1ba7217`, 구현 커밋 `34da6d5`, 병합 `3e77c50`이다. GitHub push·릴리스는 하지 않았다.

| 항목 | 상태 | 근거 |
| --- | --- | --- |
| 단일 부팅 복구 경로 | 코드·설치·운영 적용 | `wrg-services-restore.service`만 부팅 복구, 예전 `devbox-wsl-service-recovery` 해제 |
| Docker 준비 지연 | 코드·단위 검증 | 운영 Docker를 멈출 수 없어 실제 지연은 재현하지 않음 |
| 일부 서비스 실패·중복 실행 | 코드·단위·격리 실기 검증 | `tests/integration_launcher.py` 통과 |
| 자동 실행 켜짐/꺼짐·중지 유지 | 코드·단위·격리 실기 검증 | 같은 격리 시험에서 `Requires=` 끌어올림까지 확인 |
| SallimBook 등록·시작 절차 연결 | 운영 적용 | 아래 운영 기록 |
| PC/WSL 전체 재부팅 | 미실시 | 중단 영향 설명 후 시점 협의 필요 |

## 발견한 문제

- Windows 로그온 작업이 매번 별도 고정 목록 복구 unit을 설치·enable했고, 그 스크립트가 `ssh`·`tailscaled`·`docker`를 `systemctl enable`해 Resource Guard의 기반 서비스 자동 실행 설정을 덮어썼다.
- 설치기의 `disable_legacy_unit()`은 정의만 있고 호출되지 않아 실제로 예전 unit을 비활성화하지 않았다. `packaging/devbox-*` 호환 진입점도 설치 경로가 없었다. 두 부팅 unit이 모두 enabled였다.
- `restore()`는 Docker가 준비되지 않으면 아무것도 하지 않고 성공으로 끝났고, 컨테이너가 없는 자동 실행 프로젝트를 건너뛰었으며 health를 기다리지 않았다. 유닛 제한 시간 300초는 SallimBook 시작 절차(최대 600초)보다 짧았다.
- SallimBook은 세 경로로 스스로 시작할 수 있었다: `WantedBy=default.target`인 startup unit(linger 사용), 컨테이너의 `unless-stopped`, 그리고 startup을 `Requires=`하는 health·CVD 서비스의 `Persistent=true` timer. 마지막 경로는 startup을 disable하거나 중지해도 다음 timer 때 `compose up`을 다시 실행한다.

## 구현

### 부팅 복구

- 자동 실행이 켜진 Compose 프로젝트: 재시작 정책을 `unless-stopped`로 맞추고 기존 컨테이너만 시작한다. 꺼진 프로젝트는 `no`로 맞추고 실행 중이면 중지한다. 빌드·pull·재생성·배포 스크립트는 실행하지 않는다.
- Docker가 비활성인데 Docker 자동 실행이 켜져 있으면 `systemctl start docker.service`(90초 제한)로 지연된 시작에 합류하거나 재시도한다. 꺼져 있으면 건너뛰었다고 보고한다.
- 컨테이너가 없는 자동 실행 프로젝트는 실패로 보고한다. 모든 자동 실행 프로젝트의 컨테이너 실행과 자체 healthcheck를 최대 120초 기다린다.
- 프로젝트별 실패를 모은 뒤 다른 프로젝트를 계속 처리하고, 하나라도 실패하면 unit이 실패로 끝난다. 결과는 프로젝트별로 journal에 남는다.
- 등록 잠금은 짧은 Docker 조작에만 잡고, 시작 절차 실행과 health 대기는 잠금 밖에서 수행해 웹 조작을 몇 분씩 막지 않는다. 시작 절차는 프로젝트별 잠금으로 동시에 두 번 실행하지 않는다.
- 부팅 unit(`wait`)은 시작 절차·health를 기다리고, 소켓의 `wrg services restore --confirm`은 요청만 접수한다.
- `wrg-services-restore.service`: 제한 시간 1200초, `After=wrg-service-control.service`, 공유 플래그·실행 표식 경로 쓰기 허용. 컨트롤러 runtime 디렉터리는 재시작 시 보존한다.

### 시작 절차 연결

- 소유자에게 `<Compose 프로젝트>-startup.service`가 있으면 발견 단계에서 연결한다. 클라이언트가 unit 이름을 지정하지 않는다.
- 연결 조건: `RemainAfterExit=yes`인 oneshot, 그리고 실행 조건 drop-in. 조건 목록은 소유자 manager에 D-Bus(`busctl`)로 조회하며 다른 triggering 조건이 섞이면 거부한다. 조건이 없으면 등록을 거부하고 넣을 내용을 안내한다.
- 실행 허용 = 자동 실행 플래그(`/var/lib/wrg-shared/launch/<id>`, 영구) 또는 이번 부팅 시작 허가(`/run/wrg-services/launch/<id>.start`), 그리고 이번 부팅 중지 표식(`.stop`) 없음.
- 시작·재시작·부팅 복구는 시작 절차 unit을 실행하고 컨테이너를 직접 시작하지 않는다. 중지는 중지 표식을 만들고 컨테이너를 중지하며, 진행 중인 시작만 취소한다.
- 소유자 manager 호출은 `systemctl --user --machine=<owner>@`로 하며 root가 소유자 HOME에 쓰지 않는다. linger가 없으면 실패로 보고한다.
- 목록은 `시작 절차 <상태>`를 표시하고, 조건이 빠지거나 플래그가 자동 실행 설정과 다르면 오류로 표시한다.

### 정리

사용되지 않던 `packaging/devbox-wsl-service-recovery*`, `disable_legacy_unit()`과 테스트, CI shellcheck 대상을 제거했다. 예전 unit의 실제 해제는 Windows 진입점이 백업과 함께 1회 수행한다.

## 검증

- 단위: 새 `tests/test_launcher_restore.py` 34개(연결·거부·자동 실행 전환·시작/중지/재시작·중지 표식·잠금·linger·Docker 시작/실패/비활성·부분 실패·컨테이너 없음·health 대기·중복 실행). 기존 Docker 상태 미지정 테스트 1개를 의도(잠금 대기 실패)에 맞게 수정. 전체 560개 OK(root 전용 2 skip). `check_public.py` 통과.
- 격리 실기(`tests/integration_launcher.py`): systemd 259 소유자 manager와 Docker에서 alpine 컨테이너 2개와 임시 사용자 unit, 같은 drop-in·`Requires=` 구조를 만들고, 부팅 unit과 같은 샌드박스(`ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges`)의 root로 실행했다. 발견 연결, 냉부팅 복구, 다른 프로젝트 unhealthy 보고, 동시 복구 2개에서 시작 절차 1회, 자동 실행 끈 뒤 `Requires=` 끌어올림 건너뜀("no trigger condition checks were met"), 자동 실행 꺼짐에서 명시적 시작, 자동 실행 켜짐에서 중지 후 끌어올림 건너뜀("unmet condition check ConditionPathExists=!…stop"), 재적용 후 재시작과 컨테이너 ID 유지를 확인했다. 시험 전후 운영 컨테이너 상태는 같았고 픽스처는 제거했다.
- 실제 Docker 준비 지연, 소유자 manager 미기동, 실제 부팅 순서는 재현하지 않았다.

## 운영 적용 (2026-10-24, KST)

1. 14:51 적용 전 기준선(등록 정보, 컨테이너 ID·시작 시각·재시작 정책, unit·timer 상태)을 `/var/backups/wrg-services/integration-20261024-before`에 저장했다.
2. 15:11 `install-services.sh`로 `3e77c50`(clean)을 설치했다. 설치 복구 자료는 `/var/backups/wrg-services/f9d31460dc57476cac4bb1bb06ba6d0e`. 설치본 코드·두 unit이 원본과 일치하고, 기존 등록 13개는 바이트 단위로 같았다.
3. drop-in 없이 SallimBook 등록을 시도해 "실행 조건 없음"으로 거부되는 것을 운영에서 확인한 뒤, `sallimbook-prod-startup.service.d/wrg-launch.conf`를 추가하고 `compose:sallimbook-prod`를 등록했다. 자동 실행은 기존 상태(켜짐)를 유지했다.
4. Windows 진입점(별도 저장소)으로 예전 unit을 백업·해제하고 복구를 적용했다. FamilyCard·PointBook·SoolJang·SallimBook은 실행 유지, FamilyCare는 꺼짐 유지. 운영 컨테이너 ID·시작 시각·재시작 정책은 변하지 않았다.
5. 15:14 다른 세션이 SallimBook 새 배포(operator `code-v004`, `automation-v007`, `rendered-v009`)를 시작했다. SallimBook 일정 도구는 설치 시 `<unit>.d`가 있으면 `UNIT_ALREADY_EXISTS`, 활성화 시 drop-in이 있으면 거부하므로, 15:16 이 drop-in을 사본을 남기고 되돌렸다. 그 세션은 15:17 일정 설치·활성화에 성공했고 15:18 health 백업 유예 drop-in을 다시 적용했다.
6. 배포 종료(15:21 idle) 확인 후 15:30 drop-in을 다시 적용했다. 새 v007 unit 9개는 설치 기록 해시와 일치하고 백업·복원 시험 timer는 비활성으로 유지됐다. 소유자 권한의 조건 평가가 통과했다.
7. 등록 URL을 공개 Funnel 포트(`:443`)에서 비공개 앱 포트(`:8443`)로 바로잡았다(변경 전 등록 정보 사본 보관). 비공개 `/`·`/api/v1/health`·`/login`은 200이었다.
8. 15:32 Windows 예약 작업 직접 실행 결과 `LastTaskResult=0`, 실행 중 컨테이너의 ID·시작 시각·정책은 그대로였다.

## 남은 제약

- 트리거는 Windows **로그온 시**다. 재부팅 후 로그인하지 않으면 WSL과 서비스가 올라오지 않으며 로그인 전 무인 기동은 구성하지 않았다.
- PC/WSL 전체 재부팅 실증은 하지 않았다. 실행 시 WSL 안의 모든 작업·SSH·Paseo·원격 연결이 끊기고 Windows 재로그인이 필요하다. 다른 세션 작업이 없을 때 사용자와 시점을 정해 1회 확인한다.
- SallimBook 일정을 다시 설치·활성화할 때는 `sallimbook-prod-startup.service.d/wrg-launch.conf`를 먼저 지우고, 활성화 뒤 다시 넣어야 한다(health 백업 유예 drop-in과 같은 처리). SallimBook 쪽 배포 절차 반영은 이 작업 범위 밖이며 다른 세션에 알리지 않았다. drop-in이 빠지면 목록과 부팅 복구가 SallimBook을 오류로 보고하고, SallimBook은 자체 enabled unit으로 계속 기동한다.
- SallimBook은 별도 Tailscale 노드라 컨트롤러가 HTTP를 검사하지 않는다(같은 호스트만 검사하는 기존 보안 경계). 상태는 컨테이너·healthcheck와 SallimBook 자체 health timer로 확인한다.
- 자동 실행을 끈 채 SallimBook을 꺼 두면 SallimBook의 health·CVD timer는 실행되지만 서비스를 시작하지 않고 실패로 기록된다.
- 수동 재적용(`systemctl start wrg-services-restore.service` 또는 Windows 진입점)은 이번 부팅에 직접 켠 자동 실행 꺼짐 앱도 중지한다.
