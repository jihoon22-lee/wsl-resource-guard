# 개발·검증·릴리스

Python 3.11–3.14, Node.js, Playwright Chromium·WebKit을 사용합니다. 개발 체크아웃 하나에서 작업하고, 검증된 커밋만 설치기를 통해 기존 운영 설치본에 적용하세요.

```bash
# uv 0.12.23을 사용합니다. Python은 기존 3.11–3.14 설치를 지정하세요.
uv sync --locked --extra web --group dev --no-build --no-install-project --no-python-downloads --python python3
uv run --locked --no-sync python scripts/dependency_profiles.py
.venv/bin/python -m playwright install --with-deps chromium webkit
.venv/bin/python scripts/ci_unit.py
node --check wsl_resource_guard/web/app.js
.venv/bin/python tests/browser_offline.py
.venv/bin/python tests/browser_review_scenarios.py --browser chromium
.venv/bin/python tests/browser_review_scenarios.py --browser webkit
.venv/bin/python tests/browser_scale.py --iterations 1
.venv/bin/python tests/gunicorn_transport.py -q
```

전체 CI profile은 Flask·cryptography가 필요하며 예상하지 못한 skip을 실패로 처리합니다.
실제 controller나 개인 Tailnet을 사용하는 integration/browser 스크립트는 격리 환경을 명시적으로 준비한 경우에만 실행합니다.

`browser_review_scenarios.py`는 과거 결함 진단을 정상 동작을 요구하는 회귀 검사로 전환한 스크립트입니다. 서비스 워커는 이 fixture context에서 차단해 요청이 실제 네트워크로 빠지지 않게 하며 Push/worker 검증은 기존 별도 검사와 구분합니다.
규모 검사는 서비스 50/500개·세션 200/2,000개·14일 이력을 사용합니다. `--iterations 20`과 `--browser webkit`으로 p95를 추가 측정할 수 있습니다. 출력의 `within_target=false`는 성능 목표 미달이며 기능 검사의 성공과 별개입니다. CI는 장비별 시간 목표를 강제하지 않지만 데이터·대상·오류·레이아웃 assertion은 반드시 통과해야 합니다.

에이전트는 AGENTS.md의 작업 브랜치·검증·커밋·로컬 no-ff 병합·정리 절차를 따릅니다.
PR과 CI는 권장하며 작은 수정의 승인된 직접 push를 허용합니다. required PR/CI를 강제하지 않습니다.
비밀값·운영 데이터·실제 계정 경로를 커밋하지 마세요. 새 작업은 공개 이력에서 시작하고 이전 비공개 브랜치를 병합하지 않습니다.

의존성 선언은 `pyproject.toml`, 버전·해시 잠금은 `uv.lock` 하나로 관리합니다.
기본 guard/CLI는 외부 Python 의존성을 요구하지 않으며 웹은 `web` extra, 검사 도구는 `dev` group입니다.
운영 웹과 개발 환경의 공통 패키지는 같은 잠금에서 선택합니다. Python 3.11–3.14 CI에서 실제 설치 버전도 대조합니다.

선언을 바꾸면 `uv lock`을 실행합니다. 기존 버전은 기본 보존하며 의도한 패키지만 갱신하세요.

```bash
uv lock --upgrade-package flask
uv lock --check
uv sync --locked --extra web --group dev --no-build --no-install-project --no-python-downloads --python python3
uv run --locked --no-sync python scripts/dependency_profiles.py --audit
```

`--locked`는 선언과 잠금이 다르면 실패하며 실행 도중 잠금을 갱신하지 않습니다.
감사 스크립트는 같은 잠금에서 web/dev 입력을 해시 포함 requirements 형식으로 임시 export하고,
각각 감사한 뒤 입력과 감사 캐시를 삭제합니다. requirements 파일을 별도로 추적하거나 수동 관리하지 않습니다.
`pip` 패키지는 검사 도구 `pip-audit → pip-api`의 간접 의존성으로 남을 수 있지만 설치 명령에는 사용하지 않습니다.
Dependabot은 `uv` ecosystem으로 pyproject/uv.lock을 갱신하며 Actions 갱신도 유지합니다.
봇 PR은 잠금 검사·설치 검증·전체 CI로 확인합니다. 설정만 바꾼 상태를 실제 봇 갱신 성공으로 간주하지 않습니다.

[uv 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)에 따라 uv 0.12.23을 사용하세요.
운영 설치기는 사용자 uv 설정을 상속하지 않고 PyPI wheel만 허용합니다. 개발용 사설 인덱스·path/VCS·workspace·build hook은
현재 운영 설치기의 지원 범위가 아니므로 해당 설정을 추가한 프로젝트는 설치 전에 거부됩니다.

워크플로 Action은 전체 SHA와 버전 주석을 함께 갱신합니다. CI 도구의 검사 예외를 포괄적으로 추가하지 않습니다.

## 릴리스

프로젝트 버전을 갱신하고 main에 포함된 커밋에 `v<version>` 태그를 생성합니다.
태그 워크플로는 전체 테스트·공개정보·의존성·CodeQL 결과 검사를 거쳐 동일 커밋의 압축본을 게시합니다.
실패·누락된 검사나 차단 보안 결과에서는 Release가 생성되지 않습니다. 운영 WSL 배포는 자동 실행하지 않습니다.

Release 워크플로의 수동 실행은 게시 없는 리허설입니다. main에 포함된 커밋에서 프로젝트 버전에 맞는 `tag` 값을 넣고
`scenario=none`으로 실행하면 같은 분석·압축본 검증 경로를 통과하지만 실제 태그나 Release는 만들지 않습니다.
`check-failure`는 재사용 검사 workflow의 의존성 job을 실패시켜 package와 publish가 건너뛰어지는지 확인합니다.
`analysis-missing`, `security-high`는 해당 실행이 받은 SARIF 사본에만 실패를 주입해 게시 차단을 확인합니다.
잘못된 tag 값은 최초 검증에서 거부됩니다. 실제 게시는 `v*` 태그 push 이벤트에서만 수행합니다.

```bash
python3 scripts/release.py build dist
python3 scripts/release.py verify dist/wsl-resource-guard-0.1.0.tar.gz
```

빌드는 Git의 커밋된 파일을 사용하므로 미커밋 변경은 배포 파일에 포함되지 않습니다.
`SOURCE.json`은 버전·커밋·내용 해시이며 서명이 아닙니다. 같은 입력의 압축본 재현성과 게시 후 다운로드 체크섬을 확인합니다.
