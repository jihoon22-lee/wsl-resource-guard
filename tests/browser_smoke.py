"""Manual browser smoke check. Optional --fixture-unit must name a disposable unit."""
import argparse
from pathlib import Path
import subprocess
import time

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url')
    parser.add_argument('--fixture-unit')
    parser.add_argument('--chromium', help='optional installed Chromium executable')
    args = parser.parse_args()
    url = args.url or subprocess.check_output(['wrg', 'web'], text=True).strip()
    artifact = Path('artifacts')
    artifact.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        cached = sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux64/chrome'))
        executable = args.chromium or (str(cached[-1]) if cached else None)
        browser = playwright.chromium.launch(executable_path=executable)
        page = browser.new_page(viewport={'width':1440,'height':1060}, device_scale_factor=1)
        page.on('pageerror', lambda error: errors.append(str(error)))
        response = page.goto(url)
        assert response.status == 200, response.status
        expect(page.locator('#view-overview .cards')).to_be_visible()
        expect(page.locator('#last-updated')).to_contain_text('마지막 갱신')
        page.screenshot(path=str(artifact/'dashboard-desktop.png'),full_page=True)
        for view in ('services','disks','top','sessions','mcp','history','audit'):
            page.locator(f'.sidebar [data-view="{view}"]').click()
            expect(page.locator('#view-'+view)).to_be_visible()
            expect(page.locator('#refresh')).to_be_enabled(timeout=15000)
            assert not page.locator('#notice').is_visible(), page.locator('#notice').inner_text()
        page.locator('.sidebar [data-view="sessions"]').click()
        expect(page.locator('#refresh')).to_be_enabled()
        page.locator('#session-stale').check()
        page.locator('#session-stale').uncheck()
        page.locator('.sidebar [data-view="mcp"]').click()
        page.locator('#mcp-search').fill('this-name-does-not-exist')
        expect(page.locator('#mcp-table')).to_contain_text('표시할 항목이 없습니다')
        page.locator('#mcp-search').fill('')
        page.locator('#refresh-interval').select_option('60')
        assert page.evaluate("localStorage.getItem('wrg-refresh')") == '60'
        page.locator('#refresh').click()
        expect(page.locator('#refresh')).to_be_enabled(timeout=15000)
        if args.fixture_unit:
            name = args.fixture_unit.removesuffix('.service')
            page.locator('.sidebar [data-view="services"]').click()
            expect(page.locator('#refresh')).to_be_enabled()
            page.locator('#register').click()
            page.locator('#candidate-search').fill(name)
            page.locator('.candidate').filter(has_text=name).get_by_role('button',name='등록',exact=True).click()
            expect(page.locator('#dialog')).not_to_be_visible()
            expect(page.locator('#refresh')).to_be_enabled()
            for action in ('enable','autostart-off','disable','start','restart','remove'):
                row = page.locator('#services-list tr').filter(has_text=name)
                row.get_by_role('button',name='관리 ⋯').click()
                page.locator(f'[data-action="{action}"]').click()
                page.locator('#confirm-action').click()
                expect(page.locator('#dialog')).not_to_be_visible(timeout=20000)
                expect(page.locator('#refresh')).to_be_enabled(timeout=20000)
                expect(page.locator('#notice')).to_have_class('notice success')
                if action == 'enable':
                    expect(row).to_contain_text('실행 중')
                    expect(row).to_contain_text('켜짐')
                if action == 'autostart-off':
                    expect(row).to_contain_text('실행 중')
                    expect(row).to_contain_text('꺼짐')
                if action == 'disable':
                    expect(row).to_contain_text('중지됨')
                if action == 'remove':
                    expect(row).to_have_count(0)
        page.locator('.sidebar [data-view="services"]').click()
        expect(page.locator('#refresh')).to_be_enabled()
        page.screenshot(path=str(artifact/'services-desktop.png'),full_page=True)
        page.locator('.sidebar [data-view="overview"]').click()
        expect(page.locator('#refresh')).to_be_enabled()
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(artifact/'dashboard-mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile page overflows'
        page.locator('#refresh-interval').select_option('30')
        page.clock.install()
        page.locator('#refresh-interval').select_option('60')
        page.locator('#refresh-interval').select_option('30')
        calls = []
        page.on('request', lambda req: calls.append(req.url) if '/api/monitor' in req.url else None)
        with page.expect_response(lambda response: '/api/monitor' in response.url and response.status == 200):
            page.clock.fast_forward(31000)
        expect(page.locator('#refresh')).to_be_enabled()
        assert len(calls) == 1, '30-second refresh must collect only once'
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
        page.clock.fast_forward(120000)
        assert len(calls) == 1, 'Hidden tab must not poll'
        page.locator('#refresh-interval').select_option('0')
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))")
        with page.expect_response(lambda response: '/api/monitor' in response.url and response.status == 200):
            page.locator('#refresh').click()
        expect(page.locator('#refresh')).to_be_enabled()
        assert len(calls) == 2, 'Manual refresh must work with auto refresh off'
        page.clock.fast_forward(120000)
        assert len(calls) == 2, 'Disabled auto refresh must stay off'
        page.evaluate('Promise.all([refresh(true), refresh(true), refresh()])')
        # wait_for_function is CSP-blocked; poll via evaluate instead.
        deadline = time.monotonic() + 10
        while page.evaluate('loading') and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        # F3: a forced refresh during an in-flight one is queued once, so the
        # burst adds the in-flight request plus exactly one queued rerun.
        assert len(calls) == 4, f'expected queued forced refresh, got {len(calls)}'
        browser.close()
    assert not errors, errors
    print('Browser smoke passed: all views, filters, desktop/mobile, auto/manual refresh, hidden-tab pause, no overlapping requests' + (', fixture lifecycle' if args.fixture_unit else ''))


if __name__ == '__main__':
    main()
