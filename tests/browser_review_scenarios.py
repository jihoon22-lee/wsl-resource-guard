"""Diagnostic reproductions for the 2026-10-09 usability review.

Run explicitly: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/browser_review_scenarios.py
Assertions confirm the reviewed defects, NOT acceptance of correct behavior.
All browser traffic is routed to fixtures; no real service actions are sent.
Remove/convert each reproduction to a regression test when its finding is fixed.
"""
import os
import pwd
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_offline import ORIGIN, fixtures, serve
from wsl_resource_guard.config import Settings
from wsl_resource_guard.daemon import apply_config_request
from wsl_resource_guard.service_control import Controller, atomic_json


@contextmanager
def scenario(browser, width, view, overrides=None, intercept=None):
    context = browser.new_context(viewport={'width': width, 'height': 900})
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
        expect(page.locator('#sessions-table [data-kill]')).to_have_count(1)
        expect(page.locator('#session-history')).to_contain_text('불러오는 중')
        expect(page.locator('#last-updated')).to_contain_text('마지막 갱신')
        expect(page.locator('#notice')).not_to_be_visible()
        assert not errors, errors
        print(f'U1 {width}px: session-history HTTP 502 hidden; loading remains; refresh marked successful')


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
        expect(page.locator('#view-overview')).to_contain_text('불러오는 중')
        # Real UI navigation, available on both desktop and mobile.
        page.locator('[data-view="services"]:visible').first.click()
        before = gets.count('/api/services')
        page.wait_for_timeout(500)
        expect(page.locator('#refresh')).to_be_disabled()
        assert gets.count('/api/services') == before
        assert page.evaluate('loading') is True
        pending.pop().fulfill(json=fixtures()['/api/disks'])
        expect(page.locator('#refresh')).to_be_enabled()
        expect(page.locator('#services-list [data-manage="demo"]')).to_be_visible()
        assert not errors, errors
        print(f'U2 {width}px: pending disk request blocks successful data and next view until released')


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
        expect(page.locator('#log-content')).to_have_text('DEMO old logs')
        assert not errors, errors
        print(f'U3 {width}px: Devin log dialog overwritten by earlier Demo response')


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
        expect(page.locator('#candidate-search')).to_be_visible()
        expect(page.locator('#confirm-action')).to_have_count(0)
        expect(page.locator('#dialog-title')).to_contain_text('Demo')
        assert not errors, errors
        print(f'U3-discover {width}px: discovery replaces an unrelated action confirmation')


def check_registration_retry(browser, width):
    candidate = {'key': 'systemd:new.service', 'kind': 'systemd', 'name': 'New App', 'target': 'new.service'}

    def intercept(route):
        if route.request.url.endswith('/api/services/register'):
            route.fulfill(status=400, json={'error': '접속 URL을 확인하세요.'})
            return True
        return False

    with scenario(browser, width, 'services', {'/api/discover': [candidate]}, intercept) as (page, _, _, errors):
        page.locator('#register').click()
        button = page.locator('[data-key="systemd:new.service"]')
        button.click()
        expect(page.locator('#notice')).to_contain_text('접속 URL')
        expect(page.locator('#refresh')).to_be_enabled()
        page.locator('#register-url').fill('https://new.example.test')
        expect(button).to_be_disabled()
        assert page.evaluate("document.querySelector('#dialog').open && !document.querySelector('#dialog').contains(document.querySelector('#notice'))")
        assert not errors, errors
        print(f'U4 {width}px: corrected URL cannot be retried; button disabled and error outside modal')


def check_config_failure_false_success(browser, width):
    overrides = {}

    def intercept(route):
        if route.request.url.endswith('/api/settings/change'):
            overrides['/api/settings'] = 'bad-gateway'
            route.fulfill(json={'id': 'new-request', 'message': '요청 접수'})
            return True
        return False

    with scenario(browser, width, 'settings', overrides, intercept) as (page, _, _, errors):
        page.locator('[data-config-key="warning_available_gib"]').click()
        page.locator('#config-value').fill('7')
        page.locator('#confirm-action').click()
        expect(page.locator('#notice')).to_contain_text('변경을 적용했습니다')
        expect(page.locator('#config-request')).to_contain_text('6')
        expect(page.locator('#last-updated')).to_contain_text('일부 갱신 실패')
        assert not errors, errors
        print(f'U5 {width}px: POST accepted, status GET 502; old applied result becomes new success toast')


def check_modal_validation(browser, width):
    with scenario(browser, width, 'settings') as (page, _, _, errors):
        page.locator('[data-config-key="email_heartbeat_minute"]').click()
        page.locator('#config-value').fill('60')
        page.locator('#confirm-action').click()
        expect(page.locator('#notice')).to_contain_text('0 ~ 59')
        expect(page.locator('#dialog')).to_be_visible()
        assert '0 ~ 59 범위의 정수' not in page.locator('#dialog').inner_text()
        assert not errors, errors
        print(f'U7 {width}px: validation error lives outside active modal; no inline error')


def check_config_overwrite():
    with tempfile.TemporaryDirectory(prefix='wrg-review-config-') as tmp:
        root = Path(tmp)
        registry = root / 'registry.json'
        atomic_json(registry, {'owner': pwd.getpwuid(os.getuid()).pw_name, 'services': []})
        controller = Controller(registry, run=Mock(side_effect=AssertionError('No service commands')), shared=root / 'shared')
        settings = Settings(config_path=root / 'config.toml', state_dir=str(root / 'state'))
        controller._owner_settings = Mock(return_value=settings)
        first = controller.request_config_change('warning_available_gib', 6.0)
        second = controller.request_config_change('email_heartbeat_minute', 30)
        result = apply_config_request(settings, controller.shared)
        assert result['id'] == second['id'] and result['ok']
        assert result['id'] != first['id']
        assert apply_config_request(settings, controller.shared) is None
        text = settings.config_path.read_text()
        assert 'email_heartbeat_minute = 30' in text and 'warning_available_gib' not in text
        print('U6: two accepted configuration requests; only second applied; first lost')


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


def main():
    check_config_overwrite()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            check_empty_and_layout(browser)
            for width in (1440, 390):
                for check in (check_optional_failure, check_hung_request, check_log_race,
                              check_discover_race, check_registration_retry,
                              check_config_failure_false_success, check_modal_validation):
                    check(browser, width)
        finally:
            browser.close()
    print('Review reproductions confirmed (these are defects, not passing product acceptance).')


if __name__ == '__main__':
    main()
