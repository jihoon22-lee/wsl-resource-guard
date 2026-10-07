# 개발·검증·릴리스

Python 3.11–3.14, Node.js, Playwright Chromium을 사용합니다. 운영 서비스와 분리된 체크아웃에서 작업하세요.

```bash
# uv 0.12.23을 사용합니다. Python은 기존 3.11–3.14 설치를 지정하세요.
uv venv --python python3 --no-python-downloads .venv
uv pip sync --python .venv/bin/python --require-hashes --no-build requirements-dev.txt
uv pip check --python .venv/bin/python
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/python scripts/ci_unit.py
node --check wsl_resource_guard/web/app.js
.venv/bin/python tests/browser_offline.py
.venv/bin/python tests/gunicorn_transport.py -q
```

전체 CI profile은 Flask·cryptography가 필요하며 예상하지 못한 skip을 실패로 처리합니다.
실제 controller나 개인 Tailnet을 사용하는 integration/browser 스크립트는 격리 환경을 명시적으로 준비한 경우에만 실행합니다.

에이전트는 AGENTS.md의 작업 브랜치·검증·커밋·로컬 no-ff 병합·정리 절차를 따릅니다.
PR과 CI는 권장하며 작은 수정의 승인된 직접 push를 허용합니다. required PR/CI를 강제하지 않습니다.
비밀값·운영 데이터·실제 계정 경로를 커밋하지 마세요. 새 작업은 공개 이력에서 시작하고 이전 비공개 브랜치를 병합하지 않습니다.

의존성 업데이트는 입력 명세를 수정하고 uv 0.12.23으로 다시 잠급니다.
기존 출력 잠금의 버전은 기본 보존되며 의도한 패키지만 `--upgrade-package`로 올립니다.

```bash
uv pip compile requirements-web.in --generate-hashes --python-version 3.11 --universal -o requirements-web.txt
uv pip compile requirements-dev.in --generate-hashes --python-version 3.11 --universal -o requirements-dev.txt
.venv/bin/python -m pip_audit --require-hashes --disable-pip -r requirements-web.txt
.venv/bin/python -m pip_audit --require-hashes --disable-pip -r requirements-dev.txt
```

`requirements-*.in`과 해시가 있는 `.txt`가 명세와 잠금의 기준입니다. `uv sync`/`uv run`이나
`uv.lock`은 사용하지 않습니다. Dependabot은 이 파일 형식에 맞는 `pip` ecosystem을 유지합니다.
`pip` 패키지는 개발 검사 도구 `pip-audit → pip-api`의 간접 의존성입니다. 설치 명령에는 사용하지 않습니다.
새 환경에서 `uv pip check`를 실행해 Dependabot 갱신 중 간접 의존성이 빠지지 않았는지 확인합니다.
[uv 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 따르되 버전을 고정하세요.
uv는 pip.conf/PIP_INDEX_URL 설정을 읽지 않으므로 사설 인덱스를 쓰는 개발자는 uv 설정을 별도로 검토해야 합니다.
워크플로 Action은 전체 SHA와 버전 주석을 함께 갱신합니다. CI 도구의 검사 예외를 포괄적으로 추가하지 않습니다.

## 릴리스

프로젝트 버전을 갱신하고 main에 포함된 커밋에 `v<version>` 태그를 생성합니다.
태그 워크플로는 전체 테스트·공개정보·의존성·CodeQL 결과 검사를 거쳐 동일 커밋의 압축본을 게시합니다.
실패·누락된 검사나 차단 보안 결과에서는 Release가 생성되지 않습니다. 운영 WSL 배포는 자동 실행하지 않습니다.

```bash
python3 scripts/release.py build dist
python3 scripts/release.py verify dist/wsl-resource-guard-0.1.0.tar.gz
```

빌드는 Git의 커밋된 파일을 사용하므로 미커밋 변경은 배포 파일에 포함되지 않습니다.
`SOURCE.json`은 버전·커밋·내용 해시이며 서명이 아닙니다. 같은 입력의 압축본 재현성과 게시 후 다운로드 체크섬을 확인합니다.
