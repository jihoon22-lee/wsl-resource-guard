"""Offline dashboard checks: local web assets + fixture API, no Tailscale or services.

Run: .venv/bin/python tests/browser_offline.py
Every request to the fake origin is answered from fixtures below, so the checks
never reach the real controller and are safe to run on any machine.
"""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wsl_resource_guard import webapp
from wsl_resource_guard.service_control import unit_detail

ORIGIN = 'https://wrg.offline.test'
ASSETS = Path(__file__).resolve().parents[1] / 'wsl_resource_guard/web'
SESSION_PID = 1234567  # seven digits: locale formatting would insert commas
SERVICE_PID = 7654321  # session under a systemd unit: the kill control must be disabled


def session_row(pid: int) -> dict:
    return {'root_pid': pid, 'provider': 'claude', 'root_name': 'claude', 'project': 'demo',
            'age_seconds': 7200, 'youngest_process_age_seconds': 60, 'rss_kib': 1048576,
            'swap_kib': 0, 'process_count': 3, 'mcp_rss_kib': 0, 'mcp_count': 0,
            'cgroup': '/user.slice/demo.scope', 'cpu_percent': 1.0, 'limits': {},
            'stale': False, 'killable': True, 'projects': []}


def fixtures() -> dict:
    metrics = {'mem_total_kib': 20971520, 'mem_available_kib': 5767168, 'swap_total_kib': 8388608,
               'swap_free_kib': 8388608, 'psi_some_avg60': 0.1, 'psi_full_avg60': 0.0}
    return {
        '/api/bootstrap': {'csrf': 'offline', 'login': 'owner@example.com', 'origin': ORIGIN,
                           # Interaction checks use explicit refresh/stream events.
                           'refresh_seconds': 0},
        # Warning severity + fresh timestamps: the overview alert banner shows.
        '/api/monitor': {'collected_at': 1, 'daemon_updated_at': time.time(), 'metrics': metrics,
                         'severity': 'warning',
                         'reasons': ['가용 RAM 5.5 GiB가 경고 임계값을 밑돌았습니다'],
                         'observations': [],
                         'pending_reasons': ['PSI some 압력이 경고 임계값을 넘는 중'],
                         'thresholds': {'warning_available_gib': 8, 'critical_available_gib': 4,
                                        'warning_psi_some_avg60': 5, 'critical_psi_full_avg60': 20,
                                        'warning_disk_free_percent': 20,
                                        'critical_disk_free_percent': 10},
                         'top': [
                             {'project': 'alpha', 'provider': 'claude', 'root_pid': 11,
                              'rss_kib': 2097152, 'swap_kib': 0, 'cpu_percent': 1.0,
                              'process_count': 2, 'mcp_count': 0},
                             {'project': 'beta', 'provider': 'opencode', 'root_pid': 22,
                              'rss_kib': 524288, 'swap_kib': 0, 'cpu_percent': 2.0,
                              'process_count': 1, 'mcp_count': 1}],
                         'sessions': [session_row(SESSION_PID),
                                                 {**session_row(SERVICE_PID),
                                                  'cgroup': '/system.slice/demo-web.service',
                                                  'killable': False,
                                                  'kill_block_reason': 'systemd 관리 서비스는 여기서 종료하지 않습니다.'}],
                         'mcp': [], 'mcp_count': 0,
                         'mcp_process_count': 0, 'mcp_rss_kib': 0, 'stale_session_hours': 48,
                         'top_other': [], 'leaks': [],
                         'alerts': {'channels': {'windows_toast': 'enabled',
                                                 'gmail': 'not configured',
                                                 'discord': 'disabled'},
                                    'channel_errors': {}, 'email_heartbeat_enabled': False,
                                    'last_alert': None}},
        '/api/services': {'origin': ORIGIN, 'services': [
            {'id': 'demo', 'name': 'Demo', 'kind': 'systemd', 'target': 'demo.service', 'category': 'app',
             'state': 'active', 'detail': 'running', 'autostart': True, 'memory_bytes': 2**30,
             'memory_high': 2**30, 'memory_max': 2 * 2**30,
             'health': {'ok': False, 'status': 502, 'error': 'HTTP 502', 'checked_at': time.time()},
             'url': 'https://demo.example.test/'},
            {'id': 'devin-web', 'name': 'Devin Web', 'kind': 'systemd',
             'target': 'devin-web.service', 'category': 'app', 'state': 'active',
             'detail': unit_detail({'ActiveState': 'active', 'SubState': 'exited',
                                    'Type': 'oneshot', 'RemainAfterExit': 'yes'}),
             'systemd_substate': 'exited', 'autostart': True, 'memory_bytes': None, 'url': ''},
            # Autostart on but not running: the overview summary lists it.
            {'id': 'broken-app', 'name': 'Broken App', 'kind': 'systemd',
             'target': 'broken-app.service', 'category': 'app', 'state': 'dead',
             'detail': 'dead', 'autostart': True, 'memory_bytes': None, 'url': '',
             'error': 'start-limit-hit'}]},
        # 'demo' is missing from the middle sample: the chart must break, not drop to zero.
        '/api/service-memory': [{'timestamp': 1000, 'services': {'demo': 2**30}},
                                {'timestamp': 1060, 'services': {}},
                                {'timestamp': 1120, 'services': {'demo': 2**30}}],
        # C: reaches critical within 72 h: the overview banner carries the forecast.
        '/api/disks': {'disks': [
            {'id': 'C:', 'name': 'C:', 'kind': 'windows', 'mount': '/mnt/c',
             'checked_at': time.time(), 'observed_at': time.time(),
             'status': 'ok', 'error': '', 'severity': 'warning',
             'total_bytes': 500 * 2**30, 'used_bytes': 470 * 2**30,
             'available_bytes': 30 * 2**30, 'reserved_bytes': 0,
             'available_percent': 6.0, 'used_percent': 94.0,
             'insight': {'hours_to_critical': 9, 'recent_gib_per_hour': 2.8}}],
            'history': [], 'updated_at': time.time(), 'sample_interval_seconds': 60,
            'thresholds': {'warning_free_percent': 20, 'critical_free_percent': 10}},
        '/api/docker-df': {'available': True, 'checked_at': time.time(), 'rows': [
            {'Type': 'Images', 'TotalCount': '12', 'Active': '4', 'Size': '18.2GB',
             'Reclaimable': '9.1GB (50%)'},
            {'Type': 'Build Cache', 'TotalCount': '40', 'Active': '0', 'Size': '6GB',
             'Reclaimable': '6GB'}]},
        '/api/attribution': {'keys': ['alpha', 'beta'], 'rows': [
            {'timestamp': int(time.time()) - 600 + i * 60,
             'projects': {'alpha': 2 * 2**20, 'beta': 2**20}} for i in range(10)]},
        '/api/session-history': [
            {'provider': 'claude', 'root_pid': 4242, 'root_name': 'claude', 'project': 'gone-project',
             'started_at': time.time() - 7200, 'first_seen': time.time() - 7200,
             'last_seen': time.time() - 3600, 'peak_rss_kib': 3 * 2**20, 'peak_mcp_count': 4,
             'persistent': False, 'ended': True, 'duration_seconds': 3600}],
        '/api/push/key': {'available': True, 'subscriptions': 0,
                          'public_key': 'BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4'},
        '/api/history': [], '/api/audit': [],
        '/api/alerts': {
            'episodes': [{'start': 1000, 'end': 1060, 'duration_seconds': 60,
                          'worst_severity': 'warning', 'ongoing': False,
                          'reasons_top5': ['RAM 경고'],
                          'reason_summary': [{'label': 'RAM 경고', 'samples': 3,
                                              'last': '가용 RAM 1.5 GiB'}]},
                         # Ongoing since yesterday: the card must show a date.
                         {'start': time.time() - 86400, 'end': time.time(),
                          'duration_seconds': 86400, 'worst_severity': 'warning',
                          'ongoing': True, 'reasons_top5': ['디스크 여유'],
                          'reason_summary': [{'label': '디스크 여유', 'samples': 2,
                                              'last': 'E: 디스크 여유 18.8%'}]},
                         # Started before the 7-day window but ended inside it:
                         # the card counts by start (same rule as reasons_7d),
                         # so this must NOT raise the "지난 7일 경보" count.
                         {'start': time.time() - 8 * 86400, 'end': time.time() - 3600,
                          'duration_seconds': 7 * 86400, 'worst_severity': 'critical',
                          'ongoing': False, 'reasons_top5': ['오래된 경보'],
                          'reason_summary': [{'label': '오래된 경보', 'samples': 5,
                                              'last': 'PSI full 30%'}]}],
            'reasons_7d': [{'label': '디스크 여유', 'episodes': 2,
                            'total_seconds': 7200},
                           {'label': 'RAM 경고', 'episodes': 1,
                            'total_seconds': 60}],
        },
        '/api/settings': {
            'config_path': '/home/demo/.config/wsl-resource-guard/config.toml',
            'load_warnings': ['unknown_key: 알 수 없는 설정이라 무시합니다.'],
            'groups': ['메모리·PSI 경보', '디스크', '세션·MCP', '알림', '수집·보존'],
            'rules': [{'key': 'warning_available_gib', 'value': 6.0, 'kind': 'float',
                       'min': 0.1, 'max': 1024, 'description': 'Warning: 가용 RAM 임계값 (GiB)',
                       'group': '메모리·PSI 경보', 'default': 4.0, 'editable': True},
                      {'key': 'email_heartbeat_minute', 'value': 30, 'kind': 'int',
                       'min': 0, 'max': 59, 'description': 'heartbeat 발송 분',
                       'group': '알림', 'default': 0, 'editable': True},
                      {'key': 'gmail_enabled', 'value': True, 'kind': 'bool',
                       'min': None, 'max': None, 'description': 'Gmail 알림 사용',
                       'group': '알림', 'default': True, 'editable': False}],
            'config_request': {'key': 'warning_available_gib', 'value': 6.0,
                               'requested_at': time.time(), 'state': 'applied', 'message': ''},
            'weekly_report': {'slot': '2026-W40', 'sent_at': time.time() - 86400},
            'disk_drives': ['C:'], 'wsl_vhd_path': 'D:\\vhd', 'project_roots': ['/projects'],
            'channels': {'windows_toast': 'enabled', 'gmail': 'configured',
                         'discord': 'disabled'},
            'channel_errors': {'gmail': 'SMTP 실패'},
            'code_copies': {'level': 'WARN',
                            'detail': 'CLI abc ≠ 웹 def',
                            'cli_commit': 'abc', 'web_commit': 'def',
                            'cli_dirty': False, 'web_dirty': False},
        },
    }


def serve(route, posts: list, gets: list, overrides: dict) -> None:
    request = route.request
    path = request.url.removeprefix(ORIGIN).split('?', 1)[0]
    if path == '/':
        # The real CSP so console violations fail the run (R1 regression guard).
        return route.fulfill(body=(ASSETS / 'index.html').read_text(), content_type='text/html',
                             headers={'Content-Security-Policy': webapp.CSP})
    if path == '/sw.js':
        return route.fulfill(body=(ASSETS / 'sw.js').read_text(), content_type='text/javascript')
    if path.startswith('/assets/'):
        name = path.removeprefix('/assets/')
        kinds = {'.css': 'text/css', '.svg': 'image/svg+xml',
                 '.webmanifest': 'application/manifest+json'}
        kind = next((v for ext, v in kinds.items() if name.endswith(ext)),
                    'text/javascript')
        return route.fulfill(body=(ASSETS / name).read_text(), content_type=kind)
    if request.method == 'POST':
        posts.append((path, json.loads(request.post_data or '{}')))
        return route.fulfill(body=json.dumps({'message': 'SIGTERM을 보냈습니다.'}),
                             content_type='application/json')
    gets.append(path)
    override = overrides.get(path)
    if override == 'bad-gateway':
        return route.fulfill(body='Bad Gateway', status=502, content_type='text/plain')
    if override == 'hang':
        # Kept pending to expose the loading state; the check fulfills it afterwards.
        overrides.setdefault('__pending__', []).append(route)
        return
    if override == 'slow':
        time.sleep(0.4)
    body = fixtures().get(path, {}) if isinstance(override, str) else overrides.get(path, fixtures().get(path, {}))
    return route.fulfill(body=json.dumps(body), content_type='application/json')


def check_session_kill_posts_exact_pid(page, posts: list) -> None:
    page.goto(ORIGIN + '/#sessions')
    page.evaluate('killFollowUpMs = 300')
    button = page.locator(f'#sessions-table [data-kill="{SESSION_PID}"]')
    expect(button).to_have_count(1)
    button.click()
    expect(page.locator('#dialog-content .dialog-text')).to_contain_text('1,234,567')
    page.locator('#confirm-action').click()
    expect(page.locator('#notice')).to_contain_text('SIGTERM')
    assert posts == [(f'/api/sessions/{SESSION_PID}/kill', {'confirmed': True})], posts
    # The fixture still lists the PID afterwards: the follow-up says so and
    # points at the terminal-only force kill.
    expect(page.locator('#notice')).to_contain_text(f'아직 실행 중: PID {SESSION_PID}')
    expect(page.locator('#notice')).to_contain_text('--kill')
    posts.clear()


def check_service_chart_breaks_on_missing_sample(page) -> None:
    page.goto(ORIGIN + '/#services')
    path = page.locator('#service-memory-chart svg.chart path').first
    expect(path).to_have_count(1)
    d = path.get_attribute('d') or ''
    assert d.count('M') == 2, f'missing sample must split the line, got: {d}'


def check_non_json_response_shows_http_status(page, overrides: dict) -> None:
    overrides['/api/monitor'] = 'bad-gateway'
    try:
        page.goto(ORIGIN + '/#overview')
        notice = page.locator('#notice')
        expect(notice).to_contain_text('HTTP 502')
        expect(notice).not_to_contain_text('Unexpected token')
    finally:
        overrides.pop('/api/monitor', None)


def check_partial_refresh_keeps_loaded_sections(page, overrides: dict) -> None:
    page.goto(ORIGIN + '/#overview')
    expect(page.locator('#view-overview .metric').first).to_be_visible()
    overrides['/api/disks'] = 'bad-gateway'
    try:
        page.locator('#refresh').click()
        notice = page.locator('#notice')
        expect(notice).to_contain_text('일부 갱신 실패')
        expect(notice).to_contain_text('502')
        expect(page.locator('#view-overview .metric').first).to_be_visible()
    finally:
        overrides.pop('/api/disks', None)


def check_forced_refresh_is_queued(page, overrides: dict, gets: list) -> None:
    overrides['/api/monitor'] = 'slow'
    try:
        page.goto(ORIGIN + '/#top')
        # wait_for_function evals inside the page, which the strict CSP blocks;
        # the refresh button is disabled while a refresh is in flight instead.
        expect(page.locator('#refresh')).to_be_disabled()
        page.evaluate('refresh(true)')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and gets.count('/api/monitor') < 2:
            page.wait_for_timeout(100)
        assert gets.count('/api/monitor') >= 2, (
            f'a force-refresh during an in-flight refresh must run again, got: {gets}')
    finally:
        overrides.pop('/api/monitor', None)


def check_service_session_cannot_be_killed(page) -> None:
    page.goto(ORIGIN + '/#sessions')
    row = page.locator(f'#sessions-table tr:has-text("PID {SERVICE_PID:,}")')
    disabled = row.locator('button[disabled]')
    expect(disabled).to_have_count(1)
    title = disabled.get_attribute('title') or ''
    assert 'systemd 관리 서비스' in title, title
    assert not row.locator('[data-kill]').count(), 'service session must not offer a kill control'


def check_refresh_preserves_expanded_details(page, gets: list) -> None:
    page.goto(ORIGIN + '/#sessions')
    details = page.locator('#sessions-table details[data-key]').first
    expect(details).to_have_count(1)
    details.locator('summary').click()
    assert details.evaluate('d => d.open')
    before = gets.count('/api/monitor')
    page.locator('#refresh').click()
    # wait_for_function evals inside the page, which the strict CSP blocks;
    # wait for the refresh request and the re-enabled button instead.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and gets.count('/api/monitor') <= before:
        page.wait_for_timeout(50)
    expect(page.locator('#refresh')).to_be_enabled()
    assert details.evaluate('d => d.open'), 'expanded details must survive refresh'


def check_table_sorting(page) -> None:
    page.goto(ORIGIN + '/#top')
    first_project = page.locator('#top-table tbody td').first
    expect(first_project).to_contain_text('alpha')  # fixture order
    ram_header = page.locator('#top-table .th-sort', has_text='RAM')
    ram_header.click()
    expect(page.locator('#top-table tbody td').first).to_contain_text('beta')
    ram_header.click()
    expect(page.locator('#top-table tbody td').first).to_contain_text('alpha')


def check_nav_aria_current(page) -> None:
    page.goto(ORIGIN + '/#sessions')
    selected = page.locator('.nav-item[aria-current="page"]')
    expect(selected).to_have_count(1)
    expect(selected).to_contain_text('LLM 세션')


def check_overview_stale_and_channel_chips(page, overrides: dict) -> None:
    stale_monitor = fixtures()['/api/monitor']
    stale_monitor['daemon_updated_at'] = None
    stale_monitor['severity'] = 'normal'
    stale_monitor['reasons'] = []
    stale_monitor['pending_reasons'] = []
    overrides['/api/monitor'] = stale_monitor
    try:
        page.goto(ORIGIN + '/#overview')
        # daemon_updated_at is None here: cards must dim and report age.
        expect(page.locator('#view-overview .cards.stale')).to_have_count(1)
        expect(page.locator('#view-overview')).to_contain_text('분 전 값')
        # Stale data asks for a collection check instead of judging severity.
        banner = page.locator('#view-overview .alert-banner')
        expect(banner).to_have_count(1)
        expect(banner).to_contain_text('Guard의 최근 수집 상태를 확인해 주세요')
        expect(page.locator('#view-overview')).to_contain_text('Windows 토스트 사용')
        expect(page.locator('#view-overview')).to_contain_text('Gmail 설정 안 됨')
    finally:
        overrides.pop('/api/monitor', None)


def check_overview_alert_banner(page) -> None:
    # The previous check leaves the page on #overview with the stale override;
    # a same-URL goto does not reload, so force one to refetch fixtures.
    page.goto(ORIGIN + '/#overview')
    page.reload()
    view = page.locator('#view-overview')
    banner = view.locator('.alert-banner')
    expect(banner).to_have_count(1)
    expect(banner).to_contain_text('지금 확인이 필요합니다')
    expect(banner).to_contain_text('가용 RAM 5.5 GiB')
    expect(banner).to_contain_text('판정 대기: PSI some')
    # The urgent disk forecast moved here, with a terminal-only follow-up hint.
    expect(banner).to_contain_text('C: 위험까지 약 9시간')
    expect(banner).to_contain_text('docker system df')
    expect(banner.locator('[data-navigate="disks"]')).to_have_count(1)
    expect(banner.locator('[data-navigate="top"]')).to_have_count(1)
    # The same forecast must not repeat in the 정리 제안 panel.
    suggestions = view.locator('.panel', has_text='정리 제안')
    if suggestions.count():
        assert '위험까지' not in suggestions.inner_text(), suggestions.inner_text()
    # Glyph icons are gone; only judged cards carry a status dot.
    assert not view.locator('.metric-icon').count()
    dots = view.locator('.metric .metric-dot')
    assert dots.count() == 1, f'expected one status dot, got {dots.count()}'
    assert 'warn' in (dots.first.get_attribute('class') or '').split()
    assert dots.first.get_attribute('aria-label') == '주의'
    # PSI 0.1% against a 5% warning: the bar is scaled to the threshold mark
    # (60%), not drawn as 0.1% of a 0-100 track.
    psi = view.locator('.metric', has_text='PSI')
    width = psi.locator('.meter span').evaluate('(e) => e.style.width')
    assert width == '1.2%', f'PSI meter must scale to its threshold, got {width!r}'
    assert psi.locator('.meter-mark').evaluate('(e) => e.style.left') == '60%'
    # The service summary lists only apps needing attention.
    expect(view.locator('.badge-count')).to_contain_text('2 / 3 실행')
    mini = view.locator('.mini-list')
    expect(mini).to_contain_text('Broken App')
    expect(mini).to_contain_text('자동 실행 켜짐')
    expect(mini).to_contain_text('start-limit-hit')
    # Demo runs, but its web health probe fails: it needs attention too.
    expect(mini).to_contain_text('Demo')
    expect(mini).to_contain_text('웹 응답 없음 · HTTP 502')
    assert 'Devin Web' not in mini.inner_text(), mini.inner_text()


def check_legend_toggles_series(page) -> None:
    page.goto(ORIGIN + '/#services')
    key = page.locator('#service-memory-chart .legend-key').first
    path = page.locator('#service-memory-chart svg.chart path').first
    expect(key).to_have_count(1)
    key.click()
    assert path.get_attribute('visibility') == 'hidden'
    assert 'off' in (key.get_attribute('class') or '')
    key.click()
    assert path.get_attribute('visibility') == 'visible'


def check_quick_service_action(page, posts: list) -> None:
    page.goto(ORIGIN + '/#services')
    posts.clear()
    button = page.locator('#services-list [data-quick="demo"]')
    expect(button).to_have_count(1)
    expect(button).to_contain_text('재시작')
    # Rows render a 24 h sparkline next to the memory figure.
    expect(page.locator('#services-list svg.spark')).to_have_count(1)
    # First tap only arms the button; the second tap within 3 s sends the POST.
    button.click()
    page.wait_for_timeout(200)
    assert not posts, f'first tap must not act: {posts}'
    expect(button).to_contain_text('한 번 더')
    button.click()
    expect(page.locator('#notice')).to_contain_text('SIGTERM')
    assert posts == [('/api/services/demo/action',
                      {'action': 'restart', 'confirmed': True})], posts


def check_service_limits_health_and_dialog(page) -> None:
    page.goto(ORIGIN + '/#services')
    row = page.locator('#services-list tr').filter(has=page.locator('[data-manage="demo"]'))
    # At MemoryHigh (1 GiB of a 2 GiB max) the gauge is amber, half full.
    limit = row.locator('.limit')
    expect(limit).to_have_count(1)
    assert 'warn' in (limit.get_attribute('class') or '').split()
    assert limit.locator('.meter span').evaluate('(e) => e.style.width') == '50%'
    expect(limit).to_contain_text('High 1.00 GiB · Max 2.00 GiB')
    expect(row.locator('.pill.health')).to_contain_text('응답 없음')
    expect(row).to_contain_text('웹 응답 확인 실패: HTTP 502')
    # The sparkline names its real range instead of implying a crash.
    expect(row.locator('svg.spark title')).to_contain_text('24시간 최소')
    # Grouped dialog: a running service cannot be started again.
    row.locator('[data-manage="demo"]').click()
    expect(page.locator('.action-group-label').first).to_contain_text('지금 실행')
    start = page.locator('#dialog-content [data-action="start"]')
    expect(start).to_be_disabled()
    assert start.get_attribute('title') == '이미 실행 중입니다'
    expect(page.locator('#dialog-content [data-action="restart"]')).to_be_enabled()
    expect(page.locator('#dialog-content [data-action="autostart-on"]')).to_be_disabled()
    expect(page.locator('#dialog-content .dialog-text')).to_contain_text('웹 응답 없음')
    page.keyboard.press('Escape')
    # Log axis toggle re-renders the chart and remembers the choice.
    toggle = page.locator('#service-memory-chart [data-chart-scale]')
    expect(toggle).to_contain_text('로그 축')
    toggle.click()
    expect(page.locator('#service-memory-chart .badge-count')).to_contain_text('로그')
    expect(page.locator('#service-memory-chart svg.chart text', has_text='1M')).to_have_count(1)
    page.locator('#service-memory-chart [data-chart-scale]').click()
    expect(page.locator('#service-memory-chart [data-chart-scale]')).to_contain_text('로그 축')


def check_disk_docker_and_vhd_guide(page, overrides: dict) -> None:
    data = fixtures()['/api/disks']
    data['disks'].append({
        'id': 'wsl', 'name': 'WSL · Ubuntu', 'kind': 'wsl', 'mount': '/',
        'checked_at': time.time(), 'observed_at': time.time(), 'status': 'ok', 'error': '',
        'severity': 'not-monitored', 'total_bytes': 1000 * 2**30, 'used_bytes': 300 * 2**30,
        'available_bytes': 650 * 2**30, 'reserved_bytes': 50 * 2**30,
        'available_percent': 65.0, 'used_percent': 30.0,
        'vhd': {'path': '/mnt/e/WSL/Ubuntu/ext4.vhdx', 'file_bytes': 800 * 2**30,
                'host_drive': 'C:', 'status': 'ok'},
        'insight': {'vhd_reclaim_gib': 500}})
    overrides['/api/disks'] = data
    try:
        page.goto(ORIGIN + '/#disks')
        page.reload()
        panel = page.locator('#docker-df')
        expect(panel).to_contain_text('빌드 캐시')
        expect(panel).to_contain_text('9.1GB (50%)')
        page.locator('#view-disks [data-vhd-guide]').click()
        guide = page.locator('#dialog-content .guide')
        expect(guide).to_contain_text('sudo fstrim -av')
        # The WSL path becomes the Windows path diskpart needs.
        expect(guide).to_contain_text('select vdisk file="E:\\WSL\\Ubuntu\\ext4.vhdx"')
        page.keyboard.press('Escape')
        # The overview suggestion opens the same guide.
        page.goto(ORIGIN + '/#overview')
        suggestion = page.locator('#view-overview .reason [data-vhd-guide]')
        expect(suggestion).to_have_count(1)
        suggestion.click()
        expect(page.locator('#dialog-title')).to_have_text('VHDX 정리 방법')
        page.keyboard.press('Escape')
    finally:
        overrides.pop('/api/disks', None)


def check_shell_navigation(page) -> None:
    page.goto(ORIGIN + '/#overview')
    page.reload()
    expect(page.locator('.sidebar .nav-label')).to_have_count(4)
    expect(page.locator('.sidebar .nav-item svg.icon')).to_have_count(10)
    # Badges: the failing app and the warning drive are counted.
    expect(page.locator('.sidebar [data-badge="services"]')).to_have_text('2')
    expect(page.locator('.sidebar [data-badge="disks"]')).to_have_text('1')
    expect(page.locator('.sidebar [data-badge="alerts"]')).to_have_class(re.compile(r'\bdot\b'))
    # Cards open their detail view.
    page.locator('#view-overview a.metric[href="#sessions"]').click()
    expect(page.locator('#view-sessions')).to_be_visible()
    # Filters and sort survive a reload through the hash.
    page.locator('#session-search').fill('demo')
    page.locator('#sessions-table .th-sort', has_text='RAM').click()
    expect(page).to_have_url(re.compile(r'#sessions\?q=demo&sort=4%3Aasc$'))
    page.reload()
    expect(page.locator('#session-search')).to_have_value('demo')
    expect(page.locator('#sessions-table th[aria-sort="ascending"]')).to_contain_text('RAM')
    # Keyboard: g h jumps to history, / focuses a view's search, ? helps.
    page.locator('body').click(position={'x': 5, 'y': 5})
    page.keyboard.press('g')
    page.keyboard.press('t')
    expect(page.locator('#view-top')).to_be_visible()
    page.keyboard.press('/')
    expect(page.locator('#top-search')).to_be_focused()
    page.locator('body').click(position={'x': 5, 'y': 5})
    page.keyboard.press('?')
    expect(page.locator('#dialog-title')).to_have_text('단축키')
    page.keyboard.press('Escape')
    # Ages carry the absolute start time.
    page.goto(ORIGIN + '/#sessions')
    page.locator('#session-search').fill('')
    expect(page.locator('#sessions-table .age').first).to_have_attribute('title', re.compile('^시작 '))


def check_history_tools(page, overrides: dict) -> None:
    overrides['/api/history'] = history_fixture(30)
    try:
        page.goto(ORIGIN + '/#history')
        page.reload()
        expect(page.locator('#refresh')).to_be_enabled()
        # Stacked project breakdown with a total in the tooltip.
        stacked = page.locator('#history-chart .panel', has_text='LLM 세션 메모리 구성')
        expect(stacked.locator('path.stack-area')).to_have_count(2)
        hover_chart(page, stacked.locator('svg.chart'), 0.5)
        expect(stacked.locator('.chart-tip')).to_contain_text('합계 3.00')
        expect(stacked.locator('.chart-tip')).to_contain_text('alpha 2.00')
        # Dragging across the memory chart zooms every chart to that window.
        svg = page.locator('#history-chart svg.chart').first
        box = svg.bounding_box()
        page.mouse.move(box['x'] + box['width'] * 0.3, box['y'] + box['height'] * 0.5)
        page.mouse.down()
        page.mouse.move(box['x'] + box['width'] * 0.6, box['y'] + box['height'] * 0.5, steps=5)
        page.mouse.up()
        expect(page.locator('#history-window')).to_contain_text('확대 중')
        expect(page).to_have_url(re.compile(r'#history\?range=3h&from=\d+&to=\d+'))
        assert page.locator('#history-table tbody tr').count() < 30
        page.locator('#history-unzoom').click()
        expect(page.locator('#history-window')).to_be_hidden()
        expect(page.locator('#history-table tbody tr')).to_have_count(30)
        # 14-day range is offered.
        page.locator('.range-picker [data-range="14d"]').click()
        expect(page).to_have_url(re.compile(r'range=14d'))
        page.locator('.range-picker [data-range="3h"]').click()
        # Alert episode -> its window in history.
        page.goto(ORIGIN + '/#alerts')
        page.locator('#alerts-table [data-episode]').first.click()
        expect(page.locator('#view-history')).to_be_visible()
        expect(page.locator('#history-window')).to_contain_text('확대 중')
    finally:
        overrides.pop('/api/history', None)
    # Session history lists ended sessions with their peak memory.
    page.goto(ORIGIN + '/#sessions')
    history = page.locator('#session-history')
    expect(history).to_contain_text('gone-project')
    expect(history).to_contain_text('종료')
    expect(history).to_contain_text('3.00 GiB')


def check_snooze_and_bulk_stale_kill(page, posts: list, overrides: dict) -> None:
    page.goto(ORIGIN + '/#overview')
    page.reload()
    posts.clear()
    page.locator('#overview-alert [data-snooze]').click()
    expect(page.locator('#dialog-content')).to_contain_text('재알림만 멈춥니다')
    page.locator('[data-snooze-minutes="240"]').click()
    expect(page.locator('#notice')).to_be_visible()
    assert posts == [('/api/alerts/snooze', {'minutes': 240})], posts
    posts.clear()
    monitor = fixtures()['/api/monitor']
    monitor['sessions'][0]['stale'] = True
    overrides['/api/monitor'] = monitor
    try:
        page.goto(ORIGIN + '/#sessions')
        page.reload()
        bulk = page.locator('#kill-stale')
        expect(bulk).to_contain_text('오래된 세션 1개 종료')
        bulk.click()
        expect(page.locator('#dialog-content')).to_contain_text(f'PID {SESSION_PID:,}')
        page.locator('#confirm-action').click()
        expect(page.locator('#notice')).to_be_visible()
        assert posts == [('/api/sessions/kill-stale',
                          {'pids': [SESSION_PID], 'confirmed': True})], posts
    finally:
        overrides.pop('/api/monitor', None)
        posts.clear()


def check_live_summary_stream(browser) -> None:
    page = browser.new_page()
    summary = {'severity': 'critical', 'updated_at': time.time(), 'reasons': ['x'],
               'disk_alerts': ['E:'], 'disk_critical': True, 'service_problems': 5,
               'service_failed': True, 'stale_sessions': 0, 'snooze_until': 0}

    def route(r):
        if r.request.url.endswith('/api/stream'):
            return r.fulfill(body=f'retry: 60000\n\nevent: summary\ndata: {json.dumps(summary)}\n\n',
                             content_type='text/event-stream')
        return serve(r, [], [], {})
    page.route(ORIGIN + '/**', route)
    # Audit loads neither services nor disks: the badges come from the stream.
    page.goto(ORIGIN + '/#audit')
    expect(page.locator('.sidebar [data-badge="services"]')).to_have_text('5')
    expect(page.locator('.sidebar [data-badge="services"]')).to_have_class(re.compile(r'\bbad\b'))
    expect(page.locator('.sidebar [data-badge="disks"]')).to_have_text('1')
    expect(page).to_have_title(re.compile('^🔴'))
    expect(page.locator('body')).to_have_class(re.compile(r'\blive\b'))
    page.close()


def check_settings_view(page) -> None:
    page.goto(ORIGIN + '/#settings')
    view = page.locator('#view-settings')
    expect(view).to_contain_text('config.toml')
    expect(view).to_contain_text('알 수 없는 설정')
    expect(view).to_contain_text('Gmail 오류')
    expect(view).to_contain_text('SMTP 실패')
    expect(view).to_contain_text('warning_available_gib')
    # Rules are grouped into per-group panels titled by group name (C4).
    groups = view.locator('#settings-groups .panel')
    expect(groups).to_have_count(2)
    expect(groups.nth(0).locator('.panel-heading h2')).to_contain_text('메모리·PSI 경보')
    expect(groups.nth(1).locator('.panel-heading h2')).to_contain_text('알림')
    # The description is the main row text; the key is a secondary <code>.
    first_cell = groups.nth(0).locator('tbody td').first
    expect(first_cell.locator('strong')).to_contain_text('가용 RAM 임계값')
    expect(first_cell.locator('code')).to_have_text('warning_available_gib')
    # A row differing from its default is marked.
    expect(view.locator('#settings-groups .changed').first).to_contain_text('·변경됨')
    # '기본값과 다른 항목만' keeps only changed rows; matching groups stay.
    assert view.locator('#settings-groups tbody tr').count() == 3
    view.locator('#settings-changed-only').check()
    expect(view.locator('#settings-groups tbody tr')).to_have_count(2)
    expect(view.locator('#settings-groups')).not_to_contain_text('gmail_enabled')
    view.locator('#settings-changed-only').uncheck()
    expect(view.locator('#settings-groups tbody tr')).to_have_count(3)
    # A 0 lower bound must render, and the view must not repeat the page h1.
    expect(view).to_contain_text('0 ~ 59')
    expect(view.locator('h1')).to_have_count(0)
    expect(page.locator('h1:visible')).to_have_count(1)
    # A disabled channel is a neutral 꺼짐 pill, not a warning.
    chip = view.locator('.channel-chips .pill', has_text='Discord 꺼짐')
    expect(chip).to_have_count(1)
    assert 'warn' not in (chip.get_attribute('class') or '').split()
    # Push block offers registering this device (headless Chromium has the APIs).
    expect(view.locator('#push-device')).to_contain_text('이 기기 푸시 알림')
    expect(view.locator('#push-on')).to_have_count(1)
    # Numeric rules are editable from the web; switches stay terminal-only.
    expect(view.locator('#config-request')).to_contain_text('적용됨')
    expect(view.locator('#settings-channels')).to_contain_text('주간 리포트')
    assert not view.locator('[data-config-key="gmail_enabled"]').count()
    view.locator('[data-config-key="warning_available_gib"]').click()
    page.locator('#config-value').fill('2000')
    page.locator('#confirm-action').click()
    expect(page.locator('#notice')).to_contain_text('0.1 ~ 1024')
    page.locator('#config-value').fill('6.5')
    page.locator('#confirm-action').click()
    expect(page.locator('#dialog')).not_to_have_attribute('open', '')
    # Secret values must never reach the settings payload or the page.
    body_text = view.inner_text()
    assert 'password' not in body_text.lower(), body_text


def check_alert_episodes_view(page) -> None:
    page.goto(ORIGIN + '/#alerts')
    view = page.locator('#view-alerts')
    expect(page.locator('#page-title')).to_contain_text('경보가 시작되고 끝난 구간')
    expect(view.locator('#alerts-table')).to_contain_text('RAM 경고')
    expect(view.locator('#alerts-table')).to_contain_text('주의')
    # Normalized reason label, observation count chip, last raw reason.
    expect(view.locator('#alerts-table')).to_contain_text('3회 관측')
    expect(view.locator('#alerts-table')).to_contain_text('가용 RAM 1.5 GiB')
    # '지난 7일 원인별' panel: label, N회 · 누적 text, JS-applied bar width (C4).
    reasons = view.locator('#alerts-reasons')
    expect(reasons).to_contain_text('지난 7일 원인별')
    expect(reasons).to_contain_text('디스크 여유')
    expect(reasons).to_contain_text('RAM 경고')
    expect(reasons).to_contain_text('2회 · 누적 2시간 0분')
    expect(reasons).to_contain_text('1회 · 누적 1분')
    bars = reasons.locator('.reason-bar [data-percent]')
    assert bars.count() == 2, f'expected 2 reason bars, got {bars.count()}'
    first_width = bars.first.evaluate('(e) => e.style.width')
    assert first_width == '100%', f'max-count bar must be full width, got: {first_width!r}'
    second_width = bars.nth(1).evaluate('(e) => e.style.width')
    assert second_width == '50%', f'bar must scale to the max count, got: {second_width!r}'
    # '지난 7일 경보' counts by episode start (same cutoff as the server rollup):
    # the boundary episode ended inside the window but started before it.
    recent_card = view.locator('#alerts-summary .metric').first
    expect(recent_card.locator('.metric-value')).to_have_text(re.compile(r'^1'))
    # Ongoing since yesterday: a date replaces the bare time; severity is Korean.
    ongoing = view.locator('#alerts-summary .metric').nth(1)
    expect(ongoing.locator('.metric-value')).to_contain_text(re.compile(r'\d{2}\.'))
    expect(ongoing).to_contain_text('주의')


def check_no_text_below_11px(page) -> None:
    for view in ('overview', 'services'):
        page.goto(ORIGIN + f'/#{view}')
        # #loading hides after the first refresh renders the view.
        expect(page.locator('#loading')).to_be_hidden()
        tiny = page.locator(f'#view-{view}').evaluate('''(root) => {
          const skip = new Set(['SCRIPT', 'STYLE', 'OPTION']);
          const bad = [];
          for (const el of root.querySelectorAll('*')) {
            if (skip.has(el.tagName) || !el.getClientRects().length) continue;
            const own = [...el.childNodes].some(
              (n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
            if (!own) continue;
            const size = parseFloat(getComputedStyle(el).fontSize);
            if (size < 11)
              bad.push(`${el.tagName}.${el.getAttribute('class') || ''} ${size}px`);
          }
          return bad;
        }''')
        assert not tiny, f'{view} has text below 11px: {tiny}'


def check_service_memory_cell_not_clipped(page) -> None:
    page.goto(ORIGIN + '/#services')
    cells = page.locator('#services-list .services-table td.num')
    assert cells.count(), 'service table has no memory cells'
    for i in range(cells.count()):
        clipped = cells.nth(i).evaluate('(e) => e.scrollWidth > e.clientWidth + 1')
        assert not clipped, 'service memory cell content is clipped'


def contrast_ratio(fg: str, bg: str) -> float:
    def luminance(css: str) -> float:
        channels = [float(v) / 255 for v in re.findall(r'[\d.]+', css)[:3]]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722

    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def check_skip_link_contrast(page) -> None:
    page.goto(ORIGIN + '/#overview')
    fg, bg = page.locator('.skip-link').evaluate(
        '(e) => [getComputedStyle(e).color, getComputedStyle(e).backgroundColor]')
    ratio = contrast_ratio(fg, bg)
    assert ratio >= 4.5, f'light-theme skip-link contrast {ratio:.2f} ({fg} on {bg})'


def check_log_viewer(page, gets: list) -> None:
    page.goto(ORIGIN + '/#services')
    page.locator('#services-list [data-manage="demo"]').first.click()
    page.locator('#show-logs').click()
    expect(page.locator('#log-lines')).to_have_count(1)
    page.locator('#log-lines').select_option('500')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(
            p == '/api/services/demo/logs' for p in gets):
        page.wait_for_timeout(100)
    assert any(p == '/api/services/demo/logs' for p in gets), gets
    page.locator('#dialog-close').click()


def check_history_shows_loading_until_data_arrives(page, overrides: dict) -> None:
    overrides['/api/history'] = 'hang'
    try:
        page.goto(ORIGIN + '/#history')
        expect(page.locator('#history-chart')).to_contain_text('불러오는 중')
        expect(page.locator('#view-history')).not_to_contain_text('저장된 자원 이력이 없습니다')
    finally:
        overrides.pop('/api/history', None)
        for route in overrides.pop('__pending__', []):
            route.fulfill(body='[]', content_type='application/json')


def check_chart_breaks_on_time_gap(page, overrides: dict) -> None:
    overrides['/api/service-memory'] = [
        {'timestamp': 1000, 'services': {'demo': 2**30}},
        {'timestamp': 1060, 'services': {'demo': 2**30}},
        {'timestamp': 4000, 'services': {'demo': 2**30}},
    ]
    try:
        page.goto(ORIGIN + '/#services')
        path = page.locator('#service-memory-chart svg.chart path').first
        expect(path).to_have_count(1)
        d = path.get_attribute('d') or ''
        assert d.count('M') == 2, f'a timestamp jump must split the line, got: {d}'
        # t=4000 is the last sample and must sit at the right edge (900 - 18 = 882).
        assert 'M882.0' in d, f'the x axis must scale by timestamp, got: {d}'
    finally:
        overrides.pop('/api/service-memory', None)


def history_fixture(n: int = 30) -> list:
    """Rows every 5 minutes; >20 rows exercises the narrow-screen table fold."""
    base = int(time.time()) - n * 300
    return [{'timestamp': base + i * 300, 'severity': 'normal',
             'metrics': {'mem_total_kib': 20971520, 'mem_available_kib': 5767168,
                         'swap_total_kib': 8388608, 'swap_free_kib': 8388608,
                         'psi_some_avg60': 0.1, 'psi_full_avg60': 0.0,
                         'swap_in_mib_per_minute': 0.0,
                         'swap_out_mib_per_minute': 0.0}}
            for i in range(n)]


def view_settled(page, view: str) -> None:
    """Hash-only goto returns before the hashchange handler runs; wait for the
    view to unhide (navigate ran) and the refresh it kicked off to finish."""
    page.goto(ORIGIN + f'/#{view}')
    expect(page.locator(f'#view-{view}')).to_be_visible()
    expect(page.locator('#refresh')).to_be_enabled()


def box_height(locator, minimum: float, label: str) -> None:
    """The ResizeObserver on <main> re-renders chart views ~250ms after a
    height change; retry through the transient detach it can cause."""
    page = locator.page
    deadline = time.monotonic() + 8
    box = None
    while time.monotonic() < deadline:
        box = locator.bounding_box()
        if box is not None:
            break
        page.wait_for_timeout(50)
    assert box is not None, f'{label} has no rendered box'
    assert box['height'] >= minimum, f'{label} height {box["height"]} < {minimum}'


def check_mobile_layout(page, overrides: dict, gets: list) -> None:
    """390px: sticky top bar, card-mode tables, folded history, 44px targets."""
    overrides['/api/history'] = history_fixture(30)
    try:
        page.goto(ORIGIN + '/#history')
        expect(page.locator('#history-table tbody tr')).to_have_count(30)
        # Folded to the first 20 rows; the toggle reopens and folds again.
        assert page.locator('#history-table tbody tr:visible').count() == 20
        toggle = page.locator('#history-toggle')
        expect(toggle).to_contain_text('전체 30개 보기')
        toggle.click()
        assert page.locator('#history-table tbody tr:visible').count() == 30
        expect(toggle).to_contain_text('접기')
        # The expanded choice survives a refresh re-render.
        before = gets.count('/api/history')
        page.locator('#refresh').click()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and gets.count('/api/history') <= before:
            page.wait_for_timeout(50)
        assert gets.count('/api/history') > before, 'manual refresh must refetch history'
        # #refresh re-enables only after every queued refresh has rendered.
        expect(page.locator('#refresh')).to_be_enabled()
        expect(page.locator('#history-table tbody tr')).to_have_count(30)
        assert page.locator('#history-table tbody tr:visible').count() == 30
        # Switching the range folds the table again.
        page.locator('.range-picker [data-range="24h"]').click()
        expect(page.locator('#refresh')).to_be_enabled()
        expect(page.locator('#history-table tbody tr')).to_have_count(30)
        assert page.locator('#history-table tbody tr:visible').count() == 20
        # The sticky top bar stays at the top while the page scrolls.
        sidebar = page.locator('.sidebar')
        assert sidebar.evaluate('e => getComputedStyle(e).position') == 'sticky'
        page.mouse.move(200, 400)
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(150)
        box = sidebar.bounding_box()
        assert box is not None and box['y'] <= 1, (
            f'sticky sidebar must stay on top, got: {box}')
        # Chrome hidden in the compact bar: theme label and sidebar footer.
        assert page.locator('.theme-picker label').evaluate(
            'e => getComputedStyle(e).display') == 'none'
        assert page.locator('.sidebar-foot').evaluate(
            'e => getComputedStyle(e).display') == 'none'
        # The fold changed <main>'s height, so the ResizeObserver's debounced
        # re-render is still pending; wait it out before reading geometry.
        page.wait_for_timeout(400)
        expect(page.locator('#refresh')).to_be_enabled()
        # The menu is a bottom tab bar on phones; the top bar keeps the brand.
        tabbar = page.locator('.tabbar')
        assert tabbar.evaluate('e => getComputedStyle(e).position') == 'fixed'
        tab_box = tabbar.bounding_box()
        assert tab_box and tab_box['y'] + tab_box['height'] >= 799, tab_box
        assert page.locator('.sidebar nav').evaluate('e => getComputedStyle(e).display') == 'none'
        # 44px touch targets on this view.
        box_height(tabbar.locator('.tab-item.selected'), 44, 'tab item')
        for b in page.locator('.range-picker .button').all():
            box_height(b, 44, 'range button')
        for k in page.locator('#history-chart .legend-key').all():
            box_height(k, 44, 'legend key')
    finally:
        overrides.pop('/api/history', None)
    view_settled(page, 'services')
    # The services chart render can race the first box read on this view.
    page.wait_for_timeout(400)
    row = page.locator('.services-table tbody tr').first
    display = row.evaluate('e => getComputedStyle(e).display')
    assert display == 'grid', f'390px service rows must be grid cards, got: {display}'
    cell = row.locator('td').first
    assert cell.evaluate('e => getComputedStyle(e).display') == 'block'
    assert cell.evaluate('e => getComputedStyle(e).textAlign') == 'left'
    assert cell.get_attribute('data-label') == '서비스'
    thead = page.locator('.services-table thead').first
    assert thead.evaluate('e => e.getBoundingClientRect().width') <= 2
    # Touch targets on the services cards and toolbar.
    link = page.locator('.services-table td a').first
    expect(link).to_have_count(1)
    box_height(link, 44, 'service link')
    box_height(page.locator('.services-table .button.compact').first, 44,
               'compact button')
    box_height(page.locator('#register'), 44, 'register button')
    view_settled(page, 'sessions')
    deadline = time.monotonic() + 8
    box = None
    while time.monotonic() < deadline:
        box = page.locator('#session-stale').bounding_box()
        if box is not None:
            break
        page.wait_for_timeout(50)
    assert box and box['width'] >= 20 and box['height'] >= 20, (
        f'checkbox must grow to 20px, got: {box}')
    view_settled(page, 'mcp')
    box_height(page.locator('#mcp-hours'), 44, 'mcp-hours input')
    # MCP is not a tab: 더보기 is marked selected and its menu navigates.
    expect(page.locator('#tab-more')).to_have_class(re.compile(r'\bselected\b'))
    page.locator('#tab-more').click()
    menu = page.locator('#dialog-content .more-menu')
    expect(menu.locator('[data-view="settings"]')).to_have_count(1)
    menu.locator('[data-view="alerts"]').click()
    expect(page.locator('#view-alerts')).to_be_visible()
    expect(page.locator('#dialog')).not_to_have_attribute('open', '')
    # Overview on a phone: action first; healthy disks fold behind a toggle.
    view_settled(page, 'overview')
    services_y = page.locator('#overview-services').bounding_box()['y']
    status_y = page.locator('#overview-status').bounding_box()['y']
    assert services_y < status_y, (services_y, status_y)


def check_tablet_card_mode(page) -> None:
    """768px: tables are cards, but the sidebar keeps its desktop layout."""
    view_settled(page, 'services')
    row = page.locator('.services-table tbody tr').first
    display = row.evaluate('e => getComputedStyle(e).display')
    assert display != 'table-row', f'768px must use card mode, got: {display}'
    assert page.locator('.sidebar').evaluate(
        'e => getComputedStyle(e).position') == 'fixed'


def check_desktop_history_not_collapsed(page, overrides: dict) -> None:
    """1440px: the fold is a narrow-screen feature; all rows stay visible."""
    overrides['/api/history'] = history_fixture(30)
    try:
        page.goto(ORIGIN + '/#history')
        page.reload()
        expect(page.locator('#refresh')).to_be_enabled()
        expect(page.locator('#history-table tbody tr')).to_have_count(30)
        assert page.locator('#history-table tbody tr:visible').count() == 30
        expect(page.locator('#history-toggle')).to_be_hidden()
    finally:
        overrides.pop('/api/history', None)


def check_legend_shown_count(page, overrides: dict) -> None:
    """A legend over 4 series shows '표시 N/M' and updates on toggle."""
    services = [
        {'id': f'svc-{i}', 'name': f'Svc {i}', 'kind': 'systemd',
         'target': f'svc-{i}.service', 'category': 'app', 'state': 'active',
         'detail': 'running', 'autostart': True,
         'memory_bytes': (i + 1) * 2**28, 'url': ''}
        for i in range(12)]
    overrides['/api/services'] = {'origin': ORIGIN, 'services': services}
    overrides['/api/service-memory'] = [
        {'timestamp': 1000 + t * 60,
         'services': {s['id']: (i + 1) * 2**28 for i, s in enumerate(services)}}
        for t in range(3)]
    try:
        page.goto(ORIGIN + '/#services')
        page.reload()
        expect(page.locator('#refresh')).to_be_enabled()
        counter = page.locator('#service-memory-chart .legend-count')
        expect(counter).to_have_count(1)
        expect(counter).to_contain_text('표시 4/12')
        page.locator('#service-memory-chart .legend-key').first.click()
        expect(counter).to_contain_text('표시 3/12')
    finally:
        overrides.pop('/api/services', None)
        overrides.pop('/api/service-memory', None)


def check_oneshot_status(page) -> None:
    page.goto(ORIGIN + '/#services')
    row = page.locator('#services-list tr').filter(has=page.locator('[data-manage="devin-web"]'))
    expect(row).to_contain_text('시작 완료 · 활성 상태 유지')
    expect(row).not_to_contain_text('exited')
    expect(row.locator('[data-quick-action="restart"]')).to_be_visible()
    row.locator('[data-manage="devin-web"]').click()
    expect(page.locator('.dialog-text')).to_contain_text('시작 완료 · 활성 상태 유지')
    page.keyboard.press('Escape')


def disk_history_fixture() -> dict:
    """Three days of C: history; the middle day failed and must read '—'."""
    data = fixtures()['/api/disks']

    def day(date: str, used_gib, ok: bool = True) -> dict:
        return {'date': date, 'disks': [{
            'id': 'C:', 'name': 'C:', 'status': 'ok' if ok else 'error',
            'used_bytes': used_gib * 2**30 if ok else None,
            'total_bytes': 500 * 2**30 if ok else None,
            'available_bytes': (500 - used_gib) * 2**30 if ok else None,
            'available_percent': (500 - used_gib) / 5 if ok else None,
            'observed_at': time.time()}]}

    data['history'] = [day('2026-10-01', 460), day('2026-10-02', None, ok=False),
                       day('2026-10-03', 470)]
    return data


def hover_chart(page, svg, fraction: float) -> None:
    svg.scroll_into_view_if_needed()
    box = svg.bounding_box()
    assert box is not None, 'chart has no rendered box'
    page.mouse.move(box['x'] + box['width'] * fraction, box['y'] + box['height'] / 2)


def check_disk_chart_hover(page, overrides: dict, max_viewbox: int | None = None) -> None:
    """The daily disk chart shows the shared hover tooltip with usage only."""
    overrides['/api/disks'] = disk_history_fixture()
    try:
        view_settled(page, 'disks')
        page.reload()
        expect(page.locator('#refresh')).to_be_enabled()
        page.wait_for_timeout(400)  # let a pending ResizeObserver re-render settle
        svg = page.locator('#view-disks svg.chart')
        tip = page.locator('#view-disks .chart-tip')
        expect(svg).to_have_count(1)
        if max_viewbox is not None:
            box = svg.get_attribute('viewBox') or ''
            assert int(box.split()[2]) <= max_viewbox, f'disk viewBox must shrink, got: {box}'
        # No native <title> tooltips stacking on top of the custom one.
        assert svg.locator('title').count() == 0
        hover_chart(page, svg, 0.99)
        expect(tip).to_be_visible()
        expect(tip).to_contain_text('2026-10-03')
        expect(tip).to_contain_text('C: 470 GiB')
        expect(svg.locator('.chart-cursor')).to_have_attribute('visibility', 'visible')
        hover_chart(page, svg, 0.5)
        expect(tip).to_contain_text('2026-10-02')
        expect(tip).to_contain_text('C: — GiB')
        assert '여유' not in (tip.text_content() or ''), 'tooltip shows usage only'
        page.mouse.move(1, 1)
        expect(tip).to_be_hidden()
    finally:
        overrides.pop('/api/disks', None)


def check_memory_chart_hover(page) -> None:
    """Regression: time-based charts keep their HH:MM tooltip."""
    view_settled(page, 'services')
    page.wait_for_timeout(400)
    svg = page.locator('#service-memory-chart svg.chart')
    hover_chart(page, svg, 0.99)
    tip = page.locator('#service-memory-chart .chart-tip')
    expect(tip).to_be_visible()
    expect(tip.locator('.tip-time')).to_have_text(re.compile(r'^\d{2}:\d{2}$'))
    expect(tip).to_contain_text('GiB')
    page.mouse.move(1, 1)


def check_chart_tap_tooltip(browser, overrides: dict, posts: list, gets: list) -> None:
    """Touch has no hover: a tap opens the tooltip, a tap elsewhere closes it."""
    ctx = browser.new_context(viewport={'width': 390, 'height': 800},
                              has_touch=True, is_mobile=True)
    page = ctx.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route(ORIGIN + '/**', lambda route: serve(route, posts, gets, overrides))
    overrides['/api/disks'] = disk_history_fixture()
    try:
        for view, sel, text in [('disks', '#view-disks', 'C: 470 GiB'),
                                ('services', '#service-memory-chart', 'Demo')]:
            view_settled(page, view)
            page.wait_for_timeout(400)
            svg = page.locator(f'{sel} svg.chart')
            svg.scroll_into_view_if_needed()
            box = svg.bounding_box()
            assert box is not None, f'{view} chart has no rendered box'
            page.touchscreen.tap(box['x'] + box['width'] * 0.97, box['y'] + box['height'] / 2)
            tip = page.locator(f'{sel} .chart-tip')
            expect(tip).to_be_visible()
            expect(tip).to_contain_text(text)
            page.touchscreen.tap(5, box['y'] + box['height'] + 30)
            expect(tip).to_be_hidden()
    finally:
        overrides.pop('/api/disks', None)
        ctx.close()
    assert not errors, errors


def check_psi_availability(browser) -> None:
    """Zero, missing, errors and partial buckets remain distinct on PC/mobile."""
    for mobile in (False, True):
        ctx = browser.new_context(viewport={'width': 390 if mobile else 1280, 'height': 900},
                                  has_touch=mobile, is_mobile=mobile)
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        monitor = fixtures()['/api/monitor']
        monitor.update(severity='normal', reasons=[], pending_reasons=[])
        rows = history_fixture(5)
        for row in rows:
            row['metrics'].update(psi_status='normal', psi_some_avg60=0, psi_full_avg60=0)
        rows[1]['metrics'].update(psi_status='missing', psi_some_avg60=None, psi_full_avg60=None)
        rows[2]['metrics'].update(psi_status='error', psi_some_avg60=None, psi_full_avg60=None)
        rows[3]['metrics'].update(psi_status='partial', psi_some_avg60=1, psi_full_avg60=0)
        rows[4]['metrics'].pop('psi_status')
        overrides = {'/api/monitor': monitor, '/api/history': rows}
        page.route(ORIGIN + '/**', lambda route: serve(route, [], [], overrides))
        try:
            for status, label in [('missing', '미제공'), ('error', '수집 오류'), ('normal', '수집 정상')]:
                monitor['metrics'].update(psi_status=status,
                                          psi_some_avg60=0 if status == 'normal' else None,
                                          psi_full_avg60=0 if status == 'normal' else None)
                page.goto(ORIGIN + '/#overview')
                page.reload()
                card = page.locator('#view-overview .metric', has_text='PSI')
                expect(card).to_contain_text(label)
                if status == 'normal':
                    expect(card.locator('.metric-value')).to_contain_text('0.00')
                else:
                    expect(card.locator('.metric-value')).to_contain_text('관측 불가')
                    expect(card.locator('.meter')).to_have_count(0)
                    expect(card.locator('.metric-dot')).to_have_count(0)
                    assert 'good' not in (card.get_attribute('class') or '').split()
            view_settled(page, 'history')
            table = page.locator('#history-table')
            for label in ('관측 불가', '미제공', '수집 오류', '일부 관측', '과거 수집 상태 미확인'):
                expect(table).to_contain_text(label)
            svg = page.locator('svg.chart[aria-label="메모리 대기 압력 · PSI"]')
            expect(svg).to_have_count(1)
            path = svg.locator('.series-line').first.get_attribute('d')
            assert path.count('M') == 2, path
            svg.scroll_into_view_if_needed()
            # Use the actual chart geometry to target the missing and partial rows.
            for index, label in [(1, '관측 불가'), (3, '일부 관측')]:
                point = svg.evaluate("""(e, i) => {
                    const c = e._chart, g = c.geo, r = c.rows[i];
                    const x = g.left + (r.timestamp - g.t0) / g.span * (g.w - g.left - g.right);
                    const p = new DOMPoint(x, g.top + 20).matrixTransform(e.getScreenCTM());
                    return {x:p.x, y:p.y};
                }""", index)
                if mobile:
                    page.touchscreen.tap(point['x'], point['y'])
                else:
                    page.mouse.move(point['x'], point['y'])
                expect(svg.locator('..').locator('.chart-tip')).to_contain_text(label)
            assert not errors, errors
        finally:
            ctx.close()


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        check_psi_availability(browser)
        page = browser.new_page()
        errors: list[str] = []
        page.on('pageerror', lambda error: errors.append(f'{error}\n{error.stack}'))
        # The served CSP is strict; a violation surfaces as a console error.
        page.on('console', lambda msg: errors.append(f'console: {msg.text}')
                if 'Content Security Policy' in msg.text else None)
        posts: list = []
        gets: list = []
        overrides: dict = {}
        page.route(ORIGIN + '/**', lambda route: serve(route, posts, gets, overrides))
        check_session_kill_posts_exact_pid(page, posts)
        check_service_session_cannot_be_killed(page)
        check_refresh_preserves_expanded_details(page, gets)
        check_service_chart_breaks_on_missing_sample(page)
        check_chart_breaks_on_time_gap(page, overrides)
        check_partial_refresh_keeps_loaded_sections(page, overrides)
        check_non_json_response_shows_http_status(page, overrides)
        check_forced_refresh_is_queued(page, overrides, gets)
        check_history_shows_loading_until_data_arrives(page, overrides)
        check_table_sorting(page)
        check_nav_aria_current(page)
        check_shell_navigation(page)
        check_overview_stale_and_channel_chips(page, overrides)
        check_overview_alert_banner(page)
        check_legend_toggles_series(page)
        check_quick_service_action(page, posts)
        check_oneshot_status(page)
        check_service_limits_health_and_dialog(page)
        check_log_viewer(page, gets)
        posts.clear()
        check_settings_view(page)
        assert ('/api/settings/change', {'key': 'warning_available_gib', 'value': 6.5}) in posts, posts
        posts.clear()
        check_alert_episodes_view(page)
        check_no_text_below_11px(page)
        check_service_memory_cell_not_clipped(page)
        check_skip_link_contrast(page)
        check_memory_chart_hover(page)
        check_disk_chart_hover(page, overrides)
        check_disk_docker_and_vhd_guide(page, overrides)
        check_history_tools(page, overrides)
        check_snooze_and_bulk_stale_kill(page, posts, overrides)
        check_live_summary_stream(browser)
        # Narrow screens must not shrink chart text: viewBox follows the width.
        mobile = browser.new_page(viewport={'width': 390, 'height': 800})
        mobile_errors: list[str] = []
        mobile.on('pageerror', lambda error: mobile_errors.append(str(error)))
        mobile.on('console', lambda msg: mobile_errors.append(f'console: {msg.text}')
                  if 'Content Security Policy' in msg.text else None)
        mobile.route(ORIGIN + '/**',
                     lambda route: serve(route, posts, gets, overrides))
        mobile.goto(ORIGIN + '/#services')
        box = mobile.locator('#service-memory-chart svg.chart').get_attribute('viewBox')
        assert box and int(box.split()[2]) <= 420, f'mobile viewBox must shrink, got: {box}'
        # Narrow screens stack table rows into labelled cards, the top bar
        # stays sticky, and touch targets reach 44px.
        check_mobile_layout(mobile, overrides, gets)
        check_oneshot_status(mobile)
        check_disk_chart_hover(mobile, overrides, max_viewbox=420)
        assert not mobile_errors, mobile_errors
        mobile.close()
        check_chart_tap_tooltip(browser, overrides, posts, gets)
        tablet = browser.new_page(viewport={'width': 768, 'height': 900})
        tablet_errors: list[str] = []
        tablet.on('pageerror', lambda error: tablet_errors.append(str(error)))
        tablet.on('console', lambda msg: tablet_errors.append(f'console: {msg.text}')
                  if 'Content Security Policy' in msg.text else None)
        tablet.route(ORIGIN + '/**',
                     lambda route: serve(route, posts, gets, overrides))
        check_tablet_card_mode(tablet)
        assert not tablet_errors, tablet_errors
        tablet.close()
        wide = browser.new_page(viewport={'width': 1440, 'height': 1000})
        wide_errors: list[str] = []
        wide.on('pageerror', lambda error: wide_errors.append(str(error)))
        wide.on('console', lambda msg: wide_errors.append(f'console: {msg.text}')
                if 'Content Security Policy' in msg.text else None)
        wide.route(ORIGIN + '/**',
                   lambda route: serve(route, posts, gets, overrides))
        check_desktop_history_not_collapsed(wide, overrides)
        check_legend_shown_count(wide, overrides)
        assert not wide_errors, wide_errors
        wide.close()
        browser.close()
    assert not errors, errors
    print('offline dashboard checks passed')


if __name__ == '__main__':
    main()
