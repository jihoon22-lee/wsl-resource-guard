# M1 알림 신뢰성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WSL 재시작이 지속 중인 디스크 경보를 다시 열거나 거짓 회복을 알리지 않게 하고, Windows 토스트 경로가 멈춰도 다른 채널이 전송되게 하며, 전송 실패 원인을 기록과 저널에 남긴다.

**Architecture:** 디스크 사건의 키를 부팅 범위(`disk.E::<boot_id>`)에서 호스트 범위(`disk.E::host`)로 바꾸고, `reconcile()` 시작 시 저장된 구형 키를 한 번 이전한다. 토스트 전송은 기존 `disks.bounded_probe`로 감싸 한 번에 하나만 실행하고 시간 제한을 둔다. 전달 worker는 채널이 돌려준 `detail`을 정리해 `reason`으로 보관·출력한다.

**Tech Stack:** Python 3.11+ 표준 라이브러리, `unittest`, Playwright(브라우저 검사), 프런트엔드는 빌드 없는 JS.

**Spec:** [상세 코드 리뷰와 Windows 확장 검토](2026-11-15-windows-wsl-expansion-review.md)의 N1·N2·N3, [작업 로드맵](2026-11-15-roadmap.md)의 M1.

## Global Constraints

- 새 의존성을 추가하지 않는다. Guard는 표준 라이브러리만 쓴다.
- 작업 브랜치는 `fix/alert-reliability` 하나다. main에서 수정하지 않는다. 커밋 메시지는 `<type>: <imperative English description>`이다.
- 검사는 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest ...`로 실행한다.
- `incidents.json`의 `version`은 1을 유지한다. 바뀌는 것은 디스크 사건의 `key` 값뿐이다. RAM·PSI·swap·OOM·MCP·세션 사건의 키는 바꾸지 않는다.
- 회복은 관측으로만 확정한다. 미관측·수집 공백을 정상으로 처리하지 않는다.
- 전달 기록의 `reason`과 저널 출력에 수신 주소·URL·비밀값이 들어가지 않는다. 예외는 종류 이름만 남긴다.
- 화면에 보이는 문구와 `CHANGELOG.md`는 한국어로 쓰고, `CHANGELOG.md`의 `## Unreleased` 아래에 항목을 추가한다.
- 릴리스 태그·원격 push·운영 배포는 이 계획에 포함하지 않는다.
- 알려진 제약으로 기록할 것: 이전 후 0.2.4로 되돌리면 `disk.E::host` 사건이 한 번 해소 처리되고 새 사건이 열린다.

## Review Focus

1. 재시작 뒤 드라이브가 마운트되지 않은 경우 — 같은 사건이 `unknown`으로 남고 회복을 알리지 않는다. (Task 1, Step 1의 `test_unobserved_disk_after_reboot_is_unknown_not_recovered`)
2. boot ID를 읽지 못해 빈 문자열인 경우 — 디스크 키는 영향받지 않고 구형 키 `disk.E::`도 이전된다. (Task 1, Step 1의 `test_same_boot_legacy_row_keeps_pending_recovery_and_blank_boot_migrates`)
3. 멈췄던 토스트 경로가 풀린 뒤 — 다음 토스트가 정상 전송된다. (Task 2, Step 1의 `test_stalled_toast_times_out_without_piling_up_and_recovers`)
4. 전송 함수가 URL·토큰이 든 예외를 던지는 경우 — `reason`에는 예외 종류만 남는다. (Task 3, Step 1의 `test_exception_text_never_reaches_the_ledger_or_journal`)
5. `reason`이 없는 기존 전달 기록 — 화면이 괄호 없이 그대로 표시한다. (Task 3, Step 5의 Gmail 행 검사)

---

### Task 1: 디스크 사건을 부팅을 넘어 유지 (N1)

**Files:**
- Modify: `wsl_resource_guard/incidents.py:51-56` (`_key`), `wsl_resource_guard/incidents.py:108-118` (`reconcile` 시작부), 새 함수 `_migrate_disk_keys`
- Modify: `docs/architecture.md` ("사건·신원·조치" 절), `CHANGELOG.md`
- Test: `tests/test_actionable_alerts.py` (`IncidentTests`, `DeliveryTests`)

**Interfaces:**
- Consumes: 없음.
- Produces:
  - `incidents.HOST_SCOPE = 'host'`
  - `incidents._key(condition, snapshot, boot) -> str` — 조건 키의 첫 `.` 뒤가 `disk.`로 시작하면 `'disk.<ID>:host'` 형태(예: `'disk.E::host'`)를 돌려준다. 다른 조건은 지금과 같다.
  - `incidents._migrate_disk_keys(rows: list[dict], boot: str, now: float) -> None` — 제자리 수정, 여러 번 호출해도 결과가 같다.

**이전 규칙** (`_migrate_disk_keys`가 구현할 결정):

- 구형 키는 `^(disk\.[A-Za-z]:):(.*)$`에 맞고 둘째 그룹이 `host`가 아닌 키다. 새 키는 첫째 그룹 + `:host`다.
- 구형 키의 부팅이 "현재 부팅"인 조건은 둘째 그룹과 `boot`가 모두 비어 있지 않고 같을 때뿐이다.
- 현재 부팅의 행: 키만 바꾼다. `recovery_pending`도 그대로 둔다.
- 다른 부팅의 `resolved` 행: 키를 바꾸고 `recovery_pending`·`recovery_since`·`recovery_checked_at`·`recovery_paused`를 지운다. revision은 그대로다.
- 다른 부팅의 미해소 행: 같은 새 키를 가진 미해소 행(현재 부팅 또는 이미 `host`)이 있으면 대체 처리한다 — `state='resolved'`, `resolved_at=now`, 위 회복 필드 삭제, `transitions`에 `{'at': now, 'state': 'resolved', 'superseded': True}` 추가(최근 16개 유지). 없으면 키만 바꿔 같은 사건으로 이어 간다.

- [ ] **Step 1: 실패하는 검사 작성**

`tests/test_actionable_alerts.py`의 `condition()` 아래에 추가:

```python
DISK = {'key': 'warning.disk.E:', 'severity': 'warning', 'reason': 'E: 디스크 여유 15.0%'}


def disk_metrics(now, percent=15.0, status='ok'):
    value = metrics(now)
    value.disks = [{'id': 'E:', 'kind': 'windows', 'status': status, 'total_bytes': 1000,
                    'available_bytes': percent * 10, 'available_percent': percent, 'error': ''}]
    return value
```

`IncidentTests`에 추가:

```python
    def test_disk_incident_continues_across_reboot(self):
        one = reconcile({}, [DISK], self.snap, disk_metrics(1000), self.settings, boot='boot-a')
        two = reconcile(one, [DISK], self.snap, disk_metrics(5000), self.settings, boot='boot-b')
        self.assertEqual([(r['id'], r['key'], r['state'], r['revision']) for r in two['incidents']],
                         [(one['incidents'][0]['id'], 'disk.E::host', 'active', 1)])

    def test_memory_incident_stays_boot_scoped(self):
        one = self.update({}, [condition()], 1000, boot='boot-a')
        two = self.update(one, [condition()], 5000, boot='boot-b')
        self.assertEqual({r['key'] for r in two['incidents']}, {'mcp_total:boot-a', 'mcp_total:boot-b'})

    def test_legacy_disk_rows_migrate_without_false_recovery(self):
        def legacy(char, key, state, **extra):
            return {'id': char * 32, 'key': key, 'state': state, 'severity': 'warning', 'revision': 1,
                    'opened_at': 900, 'observed_at': 990, 'reason': 'E:', 'transitions': [], **extra}
        out = {'version': 1, 'updated_at': 990, 'incidents': [
            legacy('a', 'disk.E::boot-a', 'active'),
            legacy('b', 'disk.E::boot-0', 'resolved', resolved_at=950, recovery_pending=True,
                   recovery_since=950, recovery_checked_at=990),
            legacy('c', 'disk.E::boot-b', 'active')]}
        for now in range(1000, 1000 + RECURRENCE_MERGE_SECONDS + 60, 15):
            out = reconcile(out, [DISK], self.snap, disk_metrics(now), self.settings, boot='boot-b')
        rows = {r['id'][0]: r for r in out['incidents']}
        self.assertEqual({r['key'] for r in rows.values()}, {'disk.E::host'})
        self.assertEqual({k: (r['state'], r['revision']) for k, r in rows.items()},
                         {'a': ('resolved', 1), 'b': ('resolved', 1), 'c': ('active', 1)})
        self.assertTrue(rows['a']['transitions'][-1]['superseded'])
        self.assertFalse(any('recovery_pending' in r or 'recovery_confirmed_at' in r for r in rows.values()))

    def test_same_boot_legacy_row_keeps_pending_recovery_and_blank_boot_migrates(self):
        base = {'severity': 'warning', 'revision': 1, 'opened_at': 900, 'reason': 'E:', 'transitions': []}
        pending = dict(base, id='d' * 32, key='disk.E::boot-b', state='resolved', observed_at=950, resolved_at=950,
                       recovery_pending=True, recovery_since=950, recovery_checked_at=990)
        out = reconcile({'version': 1, 'updated_at': 990, 'incidents': [pending]}, [], self.snap,
                        disk_metrics(1000, 50.0), self.settings, boot='boot-b')
        self.assertEqual((out['incidents'][0]['key'], out['incidents'][0].get('recovery_pending')),
                         ('disk.E::host', True))
        active = dict(base, id='e' * 32, key='disk.E::', state='active', observed_at=990)
        out = reconcile({'version': 1, 'updated_at': 990, 'incidents': [active]}, [DISK], self.snap,
                        disk_metrics(1000), self.settings, boot='')
        self.assertEqual([(r['id'], r['key'], r['state'], r['revision']) for r in out['incidents']],
                         [('e' * 32, 'disk.E::host', 'active', 1)])

    def test_unobserved_disk_after_reboot_is_unknown_not_recovered(self):
        out = reconcile({}, [DISK], self.snap, disk_metrics(1000), self.settings, boot='boot-a')
        for now in range(5000, 5000 + RECURRENCE_MERGE_SECONDS + 300, 15):
            out = reconcile(out, [], self.snap, disk_metrics(now, status='unmounted'), self.settings, boot='boot-b')
        self.assertEqual([(r['state'], r['revision']) for r in out['incidents']], [('unknown', 1)])
```

`DeliveryTests`에 추가:

```python
    def test_reboot_neither_resends_a_disk_alert_nor_claims_recovery(self):
        sent, store = [], {}
        def send(destination, row, notifier, origin):
            sent.append((row['state'], row['revision']))
            return NotificationResult('gmail', True, 'fixture')
        for boot, start, seconds in (('boot-a', 1000, 90), ('boot-b', 1200, 2400)):
            for now in range(start, start + seconds, 15):
                self.now = now
                store = reconcile(store, [DISK], fixture(True), disk_metrics(now), self.settings, boot=boot)
                atomic_write(self.root/'incidents.json', store)
                self.engine.tick(send=send)
        self.assertEqual(sent, [('active', 1)] * 2)
        self.assertEqual(len(store['incidents']), 1)
```

- [ ] **Step 2: 실패 확인**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_actionable_alerts.IncidentTests tests.test_actionable_alerts.DeliveryTests -v 2>&1 | tail -25`

Expected: 새 검사 6개 중 4개 FAIL — `test_disk_incident_continues_across_reboot`, `test_legacy_disk_rows_migrate_without_false_recovery`, `test_same_boot_legacy_row_keeps_pending_recovery_and_blank_boot_migrates`, `test_reboot_neither_resends_a_disk_alert_nor_claims_recovery`. `test_memory_incident_stays_boot_scoped`와 `test_unobserved_disk_after_reboot_is_unknown_not_recovered`는 기존 동작을 고정하는 검사라 수정 전에도 PASS다.

- [ ] **Step 3: `_key`와 `_migrate_disk_keys` 구현, `reconcile`에서 호출**

`wsl_resource_guard/incidents.py`에 `HOST_SCOPE`와 위 "이전 규칙"대로 `_migrate_disk_keys(rows, boot, now)`를 추가한다. `reconcile()`에서 `rows = copy.deepcopy(...)` 바로 다음, `active`를 만들기 전에 호출한다. `_unobserved()`는 `disk.E::host`에서도 드라이브 문자를 그대로 얻으므로 바꾸지 않는다.

- [ ] **Step 4: 통과 확인**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_actionable_alerts tests.test_disks tests.test_daemon -q 2>&1 | tail -4`

Expected: `OK`. 기존 검사는 수정 없이 통과해야 한다.

- [ ] **Step 5: 문서 반영**

`docs/architecture.md`의 `incidents.py` 설명 문단에 한 문장을 추가한다: 디스크 사건은 WSL 재시작을 넘어 같은 사건으로 이어지고, RAM·PSI·swap·OOM·MCP·세션 사건은 부팅 범위다. `CHANGELOG.md`의 `## Unreleased`에 추가한다: "WSL을 다시 시작해도 지속 중인 디스크 경보가 새 경보로 다시 열리거나 회복으로 잘못 알려지지 않습니다. 이전 버전의 디스크 경보 기록은 처음 실행할 때 이어서 사용합니다."

- [ ] **Step 6: Commit**

```bash
git add wsl_resource_guard/incidents.py tests/test_actionable_alerts.py docs/architecture.md CHANGELOG.md
git commit -m "fix: Keep disk incidents across WSL restarts"
```

---

### Task 2: Windows 토스트 경로에 시간 제한 (N2)

**Files:**
- Modify: `wsl_resource_guard/notifications.py:129` (`_send_windows_toast`), 모듈 상수 추가
- Modify: `CHANGELOG.md`
- Test: `tests/test_notifications.py` (`NotificationTests`), `tests/test_actionable_alerts.py` (`DeliveryTests`)

**Interfaces:**
- Consumes: `disks.bounded_probe(path: str, call, timeout: float | None = None)`, `disks.ProbeTimeout`, `disks._PROBES` (검사 정리용).
- Produces:
  - `notifications.TOAST_TIMEOUT_SECONDS = 20` — 내부 `subprocess.run(timeout=15)`보다 길어야 한다.
  - `Notifier._deliver_windows_toast(title: str, message: str) -> NotificationResult` — 현재 `_send_windows_toast`의 본문 그대로.
  - `Notifier._send_windows_toast(title, message) -> NotificationResult` — `bounded_probe('windows-toast', lambda: self._deliver_windows_toast(title, message), timeout=TOAST_TIMEOUT_SECONDS)`를 실행하고, `ProbeTimeout`이면 `NotificationResult('windows_toast', False, 'Timeout: Windows interop did not answer')`를 돌려준다.

`detail`에 `Timeout`이 들어가야 하는 이유: `delivery.py`가 이 단어가 든 결과를 "접수 여부 미확인"으로 분류해 자동 재전송하지 않는다. 토스트가 실제로 표시됐는지 알 수 없는 경우에 맞는 분류다.

- [ ] **Step 1: 실패하는 검사 작성**

`tests/test_notifications.py`에 `import threading`을 추가하고 `NotificationTests`에 추가:

```python
    def test_stalled_toast_times_out_without_piling_up_and_recovers(self) -> None:
        from wsl_resource_guard import notifications
        from wsl_resource_guard.disks import _PROBES
        notifier = Notifier(Settings())
        entered, release = threading.Event(), threading.Event()

        def stalled():
            entered.set()
            release.wait(5)
            return False

        done = MagicMock(returncode=0, stderr=b"")
        self.assertGreater(notifications.TOAST_TIMEOUT_SECONDS, 15)
        with (patch("wsl_resource_guard.notifications.Path") as path,
              patch("wsl_resource_guard.notifications.subprocess.run", return_value=done),
              patch.object(notifications, "TOAST_TIMEOUT_SECONDS", .05)):
            path.return_value.exists.side_effect = stalled
            try:
                first = notifier._send_windows_toast("t", "m")
                second = notifier._send_windows_toast("t", "m")
                self.assertTrue(entered.is_set())
                for result in (first, second):
                    self.assertFalse(result.sent)
                    self.assertFalse(result.skipped)
                    self.assertIn("Timeout", result.detail)
                self.assertEqual(path.return_value.exists.call_count, 1)
            finally:
                release.set()
                _PROBES["windows-toast"].join(3)
            path.return_value.exists.side_effect = None
            path.return_value.exists.return_value = True
            self.assertTrue(notifier._send_windows_toast("t", "m").sent)
```

`tests/test_actionable_alerts.py`의 `DeliveryTests`에 추가:

```python
    def test_stalled_toast_does_not_block_other_channels(self):
        import threading
        from wsl_resource_guard import notifications
        from wsl_resource_guard.disks import _PROBES
        from wsl_resource_guard.notifications import Notifier
        self.engine.settings = replace(self.settings, gmail_enabled=False, windows_toast_enabled=True,
                                       webhook_enabled=True)
        release = threading.Event()
        with (patch('wsl_resource_guard.notifications.Path') as path,
              patch.object(notifications, 'TOAST_TIMEOUT_SECONDS', .05),
              patch.object(Notifier, '_send_webhook',
                           return_value=NotificationResult('webhook', True, 'HTTP 200')) as webhook):
            path.return_value.exists.side_effect = lambda: release.wait(5) and False
            try:
                self.engine.tick()
            finally:
                release.set()
                _PROBES['windows-toast'].join(3)
        self.assertEqual(webhook.call_count, 1)
        states = {d['channel']: d['status'] for d in self.ledger()['events'][self.row['id']]['destinations'].values()}
        self.assertEqual(states, {'windows_toast': 'unknown', 'webhook': 'accepted'})
```

- [ ] **Step 2: 실패 확인**

Run: `PYTHONDONTWRITEBYTECODE=1 timeout 120 .venv/bin/python -m unittest tests.test_notifications.NotificationTests.test_stalled_toast_times_out_without_piling_up_and_recovers tests.test_actionable_alerts.DeliveryTests.test_stalled_toast_does_not_block_other_channels -v 2>&1 | tail -12`

Expected: 둘 다 실패한다. 첫 검사는 `AttributeError: ... has no attribute 'TOAST_TIMEOUT_SECONDS'`, 둘째 검사도 같은 속성 오류다.

- [ ] **Step 3: 구현**

`wsl_resource_guard/notifications.py`에 `TOAST_TIMEOUT_SECONDS`를 추가하고, 현재 `_send_windows_toast`의 본문을 `_deliver_windows_toast`로 옮긴 뒤 `_send_windows_toast`를 Interfaces대로 감싼다. `bounded_probe`와 `ProbeTimeout`은 `metrics.py`와 같은 방식으로 함수 안에서 `from .disks import ...`로 가져온다.

- [ ] **Step 4: 통과 확인**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_notifications tests.test_actionable_alerts tests.test_webpush tests.test_push_consistency -q 2>&1 | tail -4`

Expected: `OK`. 기존 토스트 검사 3개(`test_windows_toast_failure_includes_stderr_detail` 등)가 수정 없이 통과해야 한다.

- [ ] **Step 5: 변경 기록과 Commit**

`CHANGELOG.md`의 `## Unreleased`에 추가한다: "Windows 알림 경로가 응답하지 않아도 Gmail·푸시 등 다른 채널의 전송이 멈추지 않습니다. 응답하지 않은 Windows 알림은 접수 여부 미확인으로 기록합니다."

```bash
git add wsl_resource_guard/notifications.py tests/test_notifications.py tests/test_actionable_alerts.py CHANGELOG.md
git commit -m "fix: Bound the Windows toast path so other channels keep sending"
```

---

### Task 3: 전송 실패 원인 보관·출력·표시 (N3)

**Files:**
- Modify: `wsl_resource_guard/delivery.py:194-205` (`tick`의 결과 처리), 새 함수 `failure_reason`
- Modify: `wsl_resource_guard/web/investigate.js:54`
- Modify: `docs/usage.md` (알림 전달 상태 설명), `CHANGELOG.md`
- Test: `tests/test_actionable_alerts.py` (`DeliveryTests`), `tests/browser_actionable.py`

**Interfaces:**
- Consumes: `NotificationResult.detail` — 채널 구현이 이미 상태 코드, 예외 종류 이름, 정리된 PowerShell 오류 줄로 제한한다.
- Produces:
  - `delivery.failure_reason(detail: str) -> str` — 줄바꿈을 포함한 연속 공백을 공백 하나로 줄이고, 남은 출력 불가능한 문자를 지운 뒤 200자로 자른다.
  - 전달 기록의 수신처 항목: 상태가 `retry`·`failed`·`unknown`이면 `reason`(문자열)을 저장하고, `accepted`·`skipped`이면 `reason` 키를 제거한다. 기존 `detail` 문구는 바꾸지 않는다.
  - 저널 출력: 위 세 상태일 때 한 줄, 형식 `delivery channel=<channel> status=<status> attempt=<attempts> reason=<reason>`, `print(..., flush=True)`.
  - 화면: `reason`이 있으면 `detail` 뒤에 ` (<reason>)`을 붙인다.

- [ ] **Step 1: 실패하는 검사 작성**

`tests/test_actionable_alerts.py`의 `DeliveryTests`에 추가:

```python
    def test_failure_reason_is_kept_logged_and_cleared_on_success(self):
        import contextlib, io
        outcome = {'two': 'PowerShell exit 1: 액세스가\n거부되었습니다 ' + 'x' * 300}
        def send(destination, row, notifier, origin):
            detail = outcome.get(destination['recipient'][:3], 'sent')
            return NotificationResult('gmail', detail == 'sent', detail)
        journal = io.StringIO()
        with contextlib.redirect_stdout(journal):
            self.engine.tick(send=send)
        rows = {d['status']: d for d in self.ledger()['events'][self.row['id']]['destinations'].values()}
        self.assertNotIn('reason', rows['accepted'])
        self.assertTrue(rows['retry']['reason'].startswith('PowerShell exit 1: 액세스가 거부되었습니다 x'))
        self.assertEqual(len(rows['retry']['reason']), 200)
        self.assertEqual(rows['retry']['detail'], '전송에 실패했습니다.')
        self.assertEqual(journal.getvalue().count('delivery channel='), 1)
        self.assertIn('delivery channel=gmail status=retry attempt=1 reason=PowerShell exit 1: 액세스가 거부되었습니다',
                      journal.getvalue())
        outcome.clear()
        self.now = 1060; self.store['updated_at'] = 1060; atomic_write(self.root/'incidents.json', self.store)
        self.engine.tick(send=send)
        self.assertFalse(any('reason' in d for d in self.ledger()['events'][self.row['id']]['destinations'].values()))

    def test_exception_text_never_reaches_the_ledger_or_journal(self):
        import contextlib, io
        def send(destination, row, notifier, origin):
            raise RuntimeError('https://hooks.example.test/private-marker')
        journal = io.StringIO()
        with contextlib.redirect_stdout(journal):
            self.engine.tick(send=send)
        reasons = {d['reason'] for d in self.ledger()['events'][self.row['id']]['destinations'].values()}
        self.assertEqual(reasons, {'RuntimeError'})
        self.assertNotIn('private-marker', (self.root/'delivery.json').read_text() + journal.getvalue())
```

- [ ] **Step 2: 실패 확인**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_actionable_alerts.DeliveryTests.test_failure_reason_is_kept_logged_and_cleared_on_success tests.test_actionable_alerts.DeliveryTests.test_exception_text_never_reaches_the_ledger_or_journal -v 2>&1 | tail -12`

Expected: 둘 다 `KeyError: 'reason'`으로 실패한다.

- [ ] **Step 3: `failure_reason` 구현과 `tick` 반영**

`wsl_resource_guard/delivery.py`에 `failure_reason`을 추가한다. `tick()`에서 `status`를 정한 뒤 Interfaces의 규칙대로 `reason`을 저장하거나 제거하고, 저장한 경우 저널 줄을 출력한다. 예외 경로는 지금처럼 `type(exc).__name__`만 `detail`로 쓴다.

- [ ] **Step 4: 통과 확인**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_actionable_alerts tests.test_scheduled_delivery tests.test_owner_worker -q 2>&1 | tail -4`

Expected: `OK`.

- [ ] **Step 5: 화면 표시와 브라우저 검사**

`tests/browser_actionable.py`의 fixture에서 `'one'` 수신처에 `'reason':'HTTP 410'`을 추가하고, `'실패 · 재시도'` 검사 다음 줄에 추가한다:

```python
            expect(page.locator('#incident-detail')).to_contain_text('전송 실패 (HTTP 410) · 시도 1회')
            expect(page.locator('#incident-detail')).to_contain_text('접수됨 · 시도 1회')
```

Run: `.venv/bin/python tests/browser_actionable.py --browser chromium`

Expected: 첫 줄에서 실패한다(화면에 `(HTTP 410)`이 없다).

`wsl_resource_guard/web/investigate.js:54`에서 `${esc(d.detail || "")}`를 `${esc(d.detail || "")}${d.reason ? ` (${esc(d.reason)})` : ""}`로 바꾼다.

Run: `node --check wsl_resource_guard/web/investigate.js && .venv/bin/python tests/browser_actionable.py --browser chromium`

Expected: 구문 검사 통과, 1440px·390px 시나리오 전체 통과.

- [ ] **Step 6: 문서와 Commit**

`docs/usage.md`의 알림 전달 상태 설명에 한 문장을 추가한다: 실패·재시도·접수 미확인 항목에는 원인이 괄호로 표시되고 같은 내용이 Guard 저널에 남는다. `CHANGELOG.md`의 `## Unreleased`에 추가한다: "알림 전송이 실패하거나 접수 여부를 확인하지 못하면 원인을 경보 상세와 Guard 로그에 남깁니다. 수신 주소와 비밀값은 포함하지 않습니다."

```bash
git add wsl_resource_guard/delivery.py wsl_resource_guard/web/investigate.js tests/test_actionable_alerts.py tests/browser_actionable.py docs/usage.md CHANGELOG.md
git commit -m "fix: Keep and show why a notification delivery failed"
```

---

### Task 4: 전체 검증, 작업 기록, 병합

**Files:**
- Create: `workthrough/<실행일>-alert-reliability.md`

**Interfaces:**
- Consumes: Task 1–3의 커밋.
- Produces: 로컬 main의 병합 커밋과 작업 기록.

- [ ] **Step 1: 전체 검증**

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q
for f in app.js investigate.js sw.js theme.js; do node --check wsl_resource_guard/web/$f; done
node tests/worker_notifications.js
.venv/bin/python tests/browser_offline.py
.venv/bin/python tests/browser_actionable.py --browser chromium
.venv/bin/python tests/gunicorn_transport.py -q
.venv/bin/python scripts/check_public.py
```

Expected: 단위 검사는 기준선 560개에 새 검사 10개가 더해져 `OK (skipped=2)`, 나머지는 모두 통과. 실패가 있으면 원인을 고치고 해당 Task의 검사부터 다시 실행한다.

- [ ] **Step 2: 작업 기록 작성**

`workthrough/<실행일>-alert-reliability.md`에 변경 내용, 위 검증의 실제 결과, 실행하지 않은 검증(WebKit, 실제 9p 정지, 운영 저장소의 실제 이전, 실기기 알림 수신), 알려진 제약(0.2.4로 되돌릴 때의 동작, 정기 이메일 실패 원인은 범위 밖)을 구분해 적는다. fixture 통과와 운영 적용을 같은 상태로 적지 않는다. 새 파일을 `git add`한 뒤 `scripts/check_public.py`를 다시 실행한다(미추적 파일은 검사 대상이 아니다).

- [ ] **Step 3: Commit과 병합**

```bash
git add workthrough/<실행일>-alert-reliability.md
git commit -m "docs: Record alert reliability fixes and verification"
git switch main
git merge --no-ff fix/alert-reliability -m 'merge: Fix alert reliability across restarts and stalled channels'
git merge-base --is-ancestor fix/alert-reliability main && git branch -d fix/alert-reliability
git status --short && git branch --list && git worktree list
```

Expected: 작업 트리가 깨끗하고 `main`만 남는다. 릴리스·push·운영 배포는 하지 않고, 필요 여부를 보고에 적는다(로드맵 D7).
