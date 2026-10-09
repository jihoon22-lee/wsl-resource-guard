"""Usability acceptance checks: only fixture traffic and temporary settings.

Run: .venv/bin/python tests/browser_review_scenarios.py --browser chromium
Also supports --browser webkit. A successful exit means correct behavior.
"""
import argparse
import sys
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_offline import ORIGIN, fixtures, serve, history_fixture


@contextmanager
def scenario(browser, width, view, overrides=None, intercept=None):
    # Workers are tested separately; block them here so every engine's
    # requests remain intercepted by the offline fixture router.
    context = browser.new_context(viewport={'width': width, 'height': 900}, service_workers='block',
                                  is_mobile=width < 768, has_touch=width < 768)
    page = context.new_page()
    posts, gets, errors = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("localStorage.setItem('wrg-refresh', '0')")

    def route(request):
        if intercept and intercept(request):
            return
        serve(request, posts, gets, overrides or {})

    page.route(ORIGIN + '/**', route)
    try:
        page.goto(ORIGIN + '/#' + view)
        yield page, posts, gets, errors
    finally:
        context.close()


def check_optional_failure(browser, width):
    with scenario(browser, width, 'sessions', {'/api/session-history': 'bad-gateway'}) as (page, _, _, errors):
        expect(page.locator('#refresh')).to_be_enabled()
        expect(page.locator('#sessions-table a[href^="#target?id="]')).to_have_count(2)
        expect(page.locator('#session-history')).to_contain_text('갱신 실패')
        expect(page.locator('#last-updated')).to_contain_text('일부 갱신 실패')
        expect(page.locator('#notice')).not_to_be_visible()
        assert not errors, errors
        print(f'PASS U1 {width}px: section failure and retry visible')


def check_hung_request(browser, width):
    pending = []

    def intercept(route):
        if route.request.url.endswith('/api/disks'):
            pending.append(route)
            return True
        return False

    with scenario(browser, width, 'overview', intercept=intercept) as (page, _, gets, errors):
        expect(page.locator('#refresh')).to_be_disabled()
        page.wait_for_timeout(500)
        assert '/api/monitor' in gets and '/api/services' in gets
        expect(page.locator('#view-overview .metric').first).to_be_visible()
        # Real UI navigation, available on both desktop and mobile.
        page.locator('[data-view="services"]:visible').first.click()
        before = gets.count('/api/services')
        page.wait_for_timeout(500)
        expect(page.locator('#refresh')).to_be_enabled()
        assert gets.count('/api/services') >= 2
        pending.pop().fulfill(json=fixtures()['/api/disks'])
        expect(page.locator('#refresh')).to_be_enabled()
        expect(page.locator('#services-list [data-manage="demo"]')).to_be_visible()
        assert not errors, errors
        print(f'PASS U2 {width}px: ready sections visible; navigation independent of disk request')


def check_log_race(browser, width):
    pending = []

    def intercept(route):
        if '/logs?' in route.request.url:
            pending.append(route)
            return True
        return False

    with scenario(browser, width, 'services', intercept=intercept) as (page, _, _, errors):
        page.locator('[data-manage="demo"]').click()
        page.locator('#show-logs').click()
        expect(page.locator('#log-content')).to_contain_text('불러오는 중')
        page.locator('#dialog-close').click()
        page.locator('[data-manage="devin-web"]').click()
        page.locator('#show-logs').click()
        page.wait_for_timeout(200)
        assert len(pending) == 2
        pending[1].fulfill(json={'text': 'DEVIN actual logs'})
        expect(page.locator('#log-content')).to_have_text('DEVIN actual logs')
        pending[0].fulfill(json={'text': 'DEMO old logs'})
        expect(page.locator('#dialog-title')).to_contain_text('Devin Web')
        page.wait_for_timeout(100)
        expect(page.locator('#log-content')).to_have_text('DEVIN actual logs')
        assert not errors, errors
        print(f'PASS U3 {width}px: old log response cannot replace current target')


def check_discover_race(browser, width):
    pending = []

    def intercept(route):
        if route.request.url.endswith('/api/discover'):
            pending.append(route)
            return True
        return False

    with scenario(browser, width, 'services', intercept=intercept) as (page, _, _, errors):
        page.locator('#register').click()
        expect(page.locator('#dialog-content')).to_contain_text('검색하고 있습니다')
        page.locator('#dialog-close').click()
        page.locator('[data-manage="demo"]').click()
        page.locator('[data-action="stop"]').click()
        expect(page.locator('#confirm-action')).to_be_visible()
        pending[0].fulfill(body='[]', content_type='application/json')
        page.wait_for_timeout(100)
        expect(page.locator('#candidate-search')).to_have_count(0)
        expect(page.locator('#confirm-action')).to_be_visible()
        expect(page.locator('#dialog-title')).to_contain_text('Demo')
        assert not errors, errors
        print(f'PASS U3-discover {width}px: late discovery cannot replace confirmation')


def check_registration_retry(browser, width):
    candidate = {'key': 'systemd:new.service', 'kind': 'systemd', 'name': 'New App', 'target': 'new.service'}

    attempts = []
    def intercept(route):
        if route.request.url.endswith('/api/services/register'):
            attempts.append(route.request.post_data_json)
            if len(attempts) == 1:
                route.fulfill(status=400, json={'error': '접속 URL을 확인하세요.'})
            else:
                route.fulfill(json={'message': '등록 완료'})
            return True
        return False

    with scenario(browser, width, 'services', {'/api/discover': [candidate]}, intercept) as (page, _, _, errors):
        page.locator('#register').click()
        button = page.locator('[data-key="systemd:new.service"]')
        button.click()
        expect(page.locator('#dialog-error')).to_contain_text('접속 URL')
        expect(page.locator('#refresh')).to_be_enabled()
        page.locator('#register-url').fill('https://new.example.test')
        expect(button).to_be_enabled()
        button.click()
        expect(page.locator('#dialog')).not_to_be_visible()
        assert len(attempts) == 2 and attempts[1]['url'] == 'https://new.example.test'
        assert not errors, errors
        print(f'PASS U4 {width}px: corrected registration retries with retained input')


def check_config_failure_false_success(browser, width):
    overrides = {}

    def intercept(route):
        if route.request.url.endswith('/api/settings/change'):
            overrides['/api/settings/request'] = 'bad-gateway'
            route.fulfill(json={'id': '0000000000000099', 'message': '요청 접수'})
            return True
        return False

    with scenario(browser, width, 'settings', overrides, intercept) as (page, _, _, errors):
        page.locator('[data-config-key="warning_available_gib"]').click()
        page.locator('#config-value').fill('7')
        page.locator('#confirm-action').click()
        expect(page.locator('#config-tracking')).to_contain_text('502')
        expect(page.locator('#config-tracking')).to_contain_text('적용 여부 확인 필요')
        assert '변경을 적용했습니다' not in page.locator('#notice').inner_text()
        expect(page.locator('[data-config-key="warning_available_gib"]')).to_be_disabled()
        # Request tracking survives navigation independently of view refresh.
        page.locator('[data-view="services"]:visible').first.click()
        overrides['/api/settings/request'] = {'id': '0000000000000099', 'key': 'warning_available_gib',
                                              'value': 7, 'state': 'applied', 'interval_seconds': 15}
        page.locator('#config-check').click()
        expect(page.locator('#notice')).to_contain_text('변경을 적용했습니다')
        assert not errors, errors
        print(f'PASS U5 {width}px: no cached success; exact result confirmed after navigation')


def check_modal_validation(browser, width):
    with scenario(browser, width, 'settings') as (page, _, _, errors):
        page.locator('[data-config-key="email_heartbeat_minute"]').click()
        page.locator('#config-value').fill('60')
        page.locator('#confirm-action').click()
        expect(page.locator('#dialog-error')).to_contain_text('0 ~ 59')
        expect(page.locator('#dialog')).to_be_visible()
        expect(page.locator('#config-value')).to_be_focused()
        expect(page.locator('#config-value')).to_have_attribute('aria-invalid', 'true')
        expect(page.locator('#config-value')).to_have_attribute('aria-describedby', 'dialog-error')
        assert not errors, errors
        print(f'PASS U7 {width}px: inline error associated with focused input')


def check_empty_and_layout(browser):
    empty = fixtures()
    empty['/api/services']['services'] = []
    for key in ('sessions', 'mcp', 'top', 'top_other', 'leaks'):
        empty['/api/monitor'][key] = []
    empty['/api/session-history'] = []
    empty['/api/alerts'] = {'episodes': [], 'reasons_7d': []}
    empty['/api/disks']['disks'] = []
    empty['/api/service-memory'] = []
    empty['/api/docker-df'] = {'available': False, 'error': 'Docker 미설치', 'rows': []}
    empty['/api/push/key'] = {'available': False, 'reason': '시험 환경에서 사용 안 함'}
    views = ('overview', 'services', 'disks', 'top', 'sessions', 'mcp', 'history', 'alerts', 'settings', 'audit')
    for width in (320, 768, 1440):
        with scenario(browser, width, 'overview', empty) as (page, posts, _, errors):
            expect(page.locator('#view-overview .metric').first).to_be_visible()
            for view in views:
                # Hash navigation covers bookmarked entry points and mobile More views.
                page.evaluate('(view) => { location.hash = view; }', view)
                expect(page.locator('#view-' + view)).to_be_visible()
                expect(page.locator('#refresh')).to_be_enabled()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, view)
                expect(page.locator('#notice')).not_to_be_visible()
            page.locator('#shortcut-help').click()
            expect(page.locator('#dialog')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('#dialog')).not_to_be_visible()
            assert not posts and not errors, (posts, errors)
            print(f'PASS {width}px: 10 empty-state views, no page overflow/errors, keyboard Escape')


def check_retry_timeout_and_preservation(browser, width):
    overrides = {}
    pending = []
    def intercept(route):
        if '/api/session-history' in route.request.url and overrides.get('hold'):
            pending.append(route)
            return True
        return False
    with scenario(browser, width, 'sessions', overrides, intercept) as (page, _, _, errors):
        expect(page.locator('#session-history')).to_contain_text('gone-project')
        page.locator('#session-search').fill('demo')
        details = page.locator('#sessions-table details').first
        details.locator('summary').click()
        overrides['/api/session-history'] = 'bad-gateway'
        page.locator('#refresh').click()
        expect(page.locator('[data-section="sessionHistory"]')).to_contain_text('이전 값')
        expect(page.locator('[data-section="sessionHistory"]')).to_contain_text('마지막 성공')
        expect(page.locator('#session-search')).to_have_value('demo')
        assert details.evaluate('el => el.open')
        page.locator('#session-search').fill('')
        expect(page.locator('#session-history')).to_contain_text('gone-project')
        expect(page.locator('[data-retry-section="sessionHistory"]')).to_be_visible()
        overrides.pop('/api/session-history')
        page.locator('[data-retry-section="sessionHistory"]').click()
        expect(page.locator('[data-section="sessionHistory"]')).to_have_count(0)
        overrides['hold'] = True
        page.evaluate('readTimeoutMs = 150')
        page.locator('#refresh').click()
        expect(page.locator('[data-section="sessionHistory"]')).to_contain_text('시간이 초과')
        expect(page.locator('#refresh')).to_be_enabled()
        assert pending and not errors, errors
        overrides['hold'] = False
        for route in pending:
            route.fulfill(json=fixtures()['/api/session-history'])
        page.wait_for_timeout(100)
        print(f'PASS {width}px: old data marked, retry recovers, timeout releases read; input/details preserved')


def check_config_reload_and_mismatch(browser, width):
    identifier = '0000000000000088'
    status = {'id': identifier, 'key': 'warning_available_gib', 'value': 7,
              'state': 'pending', 'interval_seconds': 120, 'daemon_updated_at': None}
    overrides = {'/api/settings/request': status}
    writes = []
    def intercept(route):
        if route.request.url.endswith('/api/settings/change'):
            writes.append(route.request.post_data_json)
            route.fulfill(json={**status, 'message': '접수'})
            return True
        return False
    with scenario(browser, width, 'settings', overrides, intercept) as (page, _, _, errors):
        page.locator('[data-config-key="warning_available_gib"]').click()
        page.locator('#config-value').fill('7')
        page.locator('#confirm-action').click()
        expect(page.locator('#config-tracking')).to_contain_text('수집 주기 120초')
        assert page.evaluate('configDeadline - performance.now()') > 100000
        page.reload()
        expect(page.locator('#config-tracking')).to_contain_text('적용 대기')
        assert len(writes) == 1
        # Another request's outcome cannot prove our request completed.
        overrides['/api/settings/request'] = {**status, 'id': '0000000000000077', 'state': 'applied'}
        page.locator('#config-check').click()
        expect(page.locator('#config-tracking')).to_contain_text('결과를 확인하지 못했습니다')
        assert '변경을 적용했습니다' not in page.locator('#notice').inner_text()
        overrides['/api/settings/request'] = {**status, 'state': 'failed', 'message': '요청이 만료됐습니다.'}
        page.locator('#config-check').click()
        expect(page.locator('#config-tracking')).to_contain_text('설정 변경 실패')
        expect(page.locator('#notice')).to_contain_text('만료')
        expect(page.locator('[data-config-key="warning_available_gib"]')).to_be_enabled()
        assert page.evaluate("sessionStorage.getItem('wrg-config-request')") is None
        assert not errors, errors
        print(f'PASS {width}px: exact request survives reload; mismatch rejected; terminal failure releases UI')


def check_write_timeout_and_csrf(browser, width):
    held, writes = [], []
    mode = {'value': 'hold'}
    def intercept(route):
        if route.request.url.endswith('/api/services/demo/action'):
            writes.append(route.request.post_data_json)
            if mode['value'] == 'hold':
                held.append(route)
            elif mode['value'] == 'csrf':
                mode['value'] = 'ok'
                route.fulfill(status=403, json={'error': 'token expired'})
            else:
                route.fulfill(json={'message': '설정을 적용했습니다.'})
            return True
        return False
    with scenario(browser, width, 'services', intercept=intercept) as (page, _, gets, errors):
        page.evaluate('writeTimeoutMs = 150')
        page.locator('[data-manage="demo"]').click()
        page.locator('[data-action="restart"]').click()
        page.locator('#confirm-action').click()
        expect(page.locator('#notice')).to_contain_text('실제 적용됐을 수')
        expect(page.locator('#dialog')).not_to_be_visible()
        page.wait_for_timeout(200)
        assert len(writes) == 1, writes
        held[0].fulfill(json={'message': 'late reply'})
        page.wait_for_timeout(100)
        expect(page.locator('#notice')).to_contain_text('실제 적용됐을 수')
        expect(page.locator('#refresh')).to_be_enabled()
        mode['value'] = 'csrf'
        page.evaluate('writeTimeoutMs = 130000')
        page.locator('[data-manage="demo"]').click()
        page.locator('[data-action="restart"]').click()
        page.locator('#confirm-action').click()
        expect(page.locator('#notice')).to_contain_text('설정을 적용했습니다')
        assert len(writes) == 3 and gets.count('/api/bootstrap') == 2
        assert not errors, errors
        print(f'PASS {width}px: uncertain POST not replayed; explicit CSRF rejection retries once')


def check_registration_single_flight(browser, width):
    candidates = [{'key': f'systemd:{name}.service', 'kind': 'systemd', 'name': name,
                   'target': f'{name}.service'} for name in ('alpha', 'beta')]
    pending = []
    def intercept(route):
        if route.request.url.endswith('/api/services/register'):
            pending.append(route)
            return True
        return False
    with scenario(browser, width, 'services', {'/api/discover': candidates}, intercept) as (page, _, _, errors):
        page.locator('#register').click()
        page.locator('#register-name').fill('사용자 이름')
        page.locator('[data-key="systemd:alpha.service"]').click()
        expect(page.locator('[data-key="systemd:beta.service"]')).to_be_disabled()
        page.locator('#candidate-search').fill('beta')
        expect(page.locator('[data-key="systemd:beta.service"]')).to_be_disabled()
        page.keyboard.press('Escape')
        expect(page.locator('#dialog')).to_be_visible()
        assert len(pending) == 1
        pending[0].fulfill(status=503, json={'error': '일시적인 서비스 오류'})
        expect(page.locator('#dialog-error')).to_contain_text('일시적인')
        expect(page.locator('#register-name')).to_have_value('사용자 이름')
        expect(page.locator('#candidate-search')).to_have_value('beta')
        expect(page.locator('[data-key="systemd:beta.service"]')).to_be_enabled()
        assert not errors, errors
        print(f'PASS {width}px: search cannot bypass registration lock; 503 preserves inputs')


def check_mobile_rotation_and_visibility(browser):
    status = {'id': '0000000000000066', 'key': 'warning_available_gib', 'value': 7,
              'state': 'pending', 'interval_seconds': 15}
    settings = fixtures()['/api/settings']
    settings['config_request'] = status
    overrides = {'/api/settings': settings, '/api/settings/request': status}
    with scenario(browser, 390, 'settings', overrides) as (page, _, gets, errors):
        expect(page.locator('#config-tracking')).to_contain_text('적용 대기')
        # Controlled visibility events exercise lifecycle logic; this is not
        # evidence of a physical phone's OS background suspension behavior.
        page.evaluate("""() => { window.reviewHidden = true;
          Object.defineProperty(document, 'hidden', {configurable:true, get:()=>window.reviewHidden});
          document.dispatchEvent(new Event('visibilitychange')); }""")
        before = gets.count('/api/settings/request')
        page.clock.install()
        page.clock.fast_forward(10000)
        page.wait_for_timeout(100)
        assert gets.count('/api/settings/request') == before
        overrides['/api/settings/request'] = {**status, 'state': 'applied'}
        page.evaluate("window.reviewHidden=false; document.dispatchEvent(new Event('visibilitychange'))")
        expect(page.locator('#notice')).to_contain_text('변경을 적용했습니다')
        for width, height in ((844, 390), (390, 844)):
            page.set_viewport_size({'width': width, 'height': height})
            page.wait_for_timeout(300)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('[data-view="services"]:visible').first.tap()
        page.locator('[data-manage="demo"]').tap()
        expect(page.locator('#blocked-start')).to_contain_text('이미 실행 중')
        assert not errors, errors
        print('PASS mobile: rotation/tap; controlled background events suspend/resume request tracking')


def check_range_and_log_order(browser, width):
    held = []
    latest = history_fixture(4)
    latest[0]['review_marker'] = 'new range'
    def intercept(route):
        url = route.request.url
        if '/api/history?range=3h' in url or '/logs?lines=200' in url:
            held.append(route)
            return True
        if '/api/history?range=14d' in url:
            route.fulfill(json=latest)
            return True
        if '/logs?lines=50' in url:
            route.fulfill(json={'text': 'new 50 lines'})
            return True
        return False
    with scenario(browser, width, 'history', intercept=intercept) as (page, _, _, errors):
        expect(page.locator('#refresh')).to_be_disabled()
        page.locator('[data-range="14d"]').click()
        expect(page.locator('#refresh')).to_be_enabled()
        held.pop(0).fulfill(json=history_fixture(2))
        page.wait_for_timeout(100)
        assert page.evaluate('historyData[0].review_marker') == 'new range'
        page.locator('[data-view="services"]:visible').first.click()
        page.locator('[data-manage="demo"]').click()
        page.locator('#show-logs').click()
        page.locator('#log-lines').select_option('50')
        expect(page.locator('#log-content')).to_have_text('new 50 lines')
        held.pop(0).fulfill(json={'text': 'old 200 lines'})
        page.wait_for_timeout(100)
        expect(page.locator('#log-content')).to_have_text('new 50 lines')
        assert not errors, errors
        print(f'PASS {width}px: history range and log size responses cannot revert latest selection')


def check_optional_sections_and_missing_result(browser, width):
    overrides = {}
    with scenario(browser, width, 'settings', overrides) as (page, _, _, errors):
        expect(page.locator('#settings-groups')).to_contain_text('임계값')
        overrides['/api/push/key'] = 'bad-gateway'
        page.locator('#refresh').click()
        expect(page.locator('#push-device [data-section="pushKey"]')).to_contain_text('502')
        page.wait_for_timeout(200)
        expect(page.locator('#push-device [data-retry-section="pushKey"]')).to_be_visible()
        overrides.pop('/api/push/key')
        page.locator('[data-retry-section="pushKey"]').click()
        expect(page.locator('[data-section="pushKey"]')).to_have_count(0)
        # Backend forgot an old request: report unknown, never infer success.
        page.route(ORIGIN + '/api/settings/request*', lambda r: r.fulfill(status=404,
                   json={'code': 'config_not_found', 'error': '보관된 결과 없음'}))
        page.evaluate("trackConfigRequest({id:'0000000000000033',key:'warning_available_gib',value:7,interval_seconds:15})")
        expect(page.locator('#config-tracking')).to_contain_text('적용 여부 확인 필요')
        assert '변경을 적용했습니다' not in page.locator('#notice').inner_text()
        assert not errors, errors
        print(f'PASS {width}px: push-key error survives async rendering; missing result remains unknown')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', choices=('chromium', 'webkit'), default='chromium')
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = getattr(playwright, args.browser).launch()
        try:
            check_empty_and_layout(browser)
            for width in (1440, 390):
                for check in (check_optional_failure, check_hung_request, check_log_race,
                              check_discover_race, check_registration_retry,
                              check_config_failure_false_success, check_modal_validation,
                              check_retry_timeout_and_preservation, check_config_reload_and_mismatch,
                              check_write_timeout_and_csrf, check_registration_single_flight,
                              check_range_and_log_order, check_optional_sections_and_missing_result):
                    check(browser, width)
            check_mobile_rotation_and_visibility(browser)
        finally:
            browser.close()
    print('Usability acceptance checks passed')


if __name__ == '__main__':
    main()
