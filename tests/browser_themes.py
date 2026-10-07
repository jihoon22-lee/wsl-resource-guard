"""Read-only theme checks on the private dashboard; --preview uses local assets."""
import os
import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

URL = os.environ['WRG_TEST_BASE_URL']
ASSETS = Path(__file__).resolve().parents[1] / 'wsl_resource_guard/web'


def preview(context):
    # Preserve the real response status, security headers and authentication.
    context.route(URL + '/', lambda route: route.fulfill(
        response=route.fetch(), body=(ASSETS / 'index.html').read_text()))
    # The deployed backend may not yet serve newly added endpoints.
    context.route(URL + '/api/service-memory', lambda route: route.fulfill(
        body='[]', content_type='application/json'))
    stubs = {'/api/attribution': '{"keys": [], "rows": []}', '/api/session-history': '[]',
             '/api/docker-df': '{"available": false, "rows": []}',
             '/api/push/key': '{"available": false, "reason": "preview", "subscriptions": 0}'}
    for path, body in stubs.items():
        context.route(URL + path + '*', lambda route, _request, body=body: route.fulfill(
            body=body, content_type='application/json'))
    context.route(URL + '/api/stream', lambda route: route.fulfill(
        body='retry: 600000\n\n', content_type='text/event-stream'))
    context.route(URL + '/sw.js', lambda route: route.fulfill(
        body=(ASSETS / 'sw.js').read_text(), content_type='text/javascript'))
    for name in ('app.js', 'theme.js', 'style.css', 'sw.js'):
        context.route(URL + '/assets/' + name, lambda route, _request, name=name: route.fulfill(
            body=(ASSETS / name).read_text(),
            content_type='text/css' if name.endswith('.css') else 'text/javascript'))


def contrast(page):
    return page.evaluate('''() => {
      const style = getComputedStyle(document.documentElement);
      const color = name => style.getPropertyValue(name).trim();
      const luminance = hex => {
        const c = hex.slice(1).match(/../g).map(v => parseInt(v, 16) / 255)
          .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
        return c[0] * .2126 + c[1] * .7152 + c[2] * .0722;
      };
      const pairs = [
        ['--ink', '--surface'], ['--muted', '--surface'],
        ['--muted', '--bg'], ['--muted', '--neutral-bg'],
        ['--good', '--good-bg'], ['--amber', '--warning-bg'],
        ['--red', '--critical-bg'], ['--accent', '--surface'],
        ...[1, 2, 3, 4].map(i => ['--chart-' + i, '--surface']),
        ['--badge-warn-fg', '--badge-warn-bg'], ['--badge-bad-fg', '--badge-bad-bg'],
      ];
      // Lines and areas are graphics: WCAG 1.4.11 asks 3:1, marked by a 3.
      const graphics = [
        ...[5, 6, 7, 8].map(i => ['--chart-' + i, '--surface', 3]),
        ['--badge-warn-bg', '--nav', 3], ['--badge-bad-bg', '--nav', 3],
      ];
      return [...pairs, ...graphics].map(([fg, bg, min = 4.5]) => {
        const a = luminance(color(fg)), b = luminance(color(bg));
        return [fg + '/' + bg, (Math.max(a,b)+.05)/(Math.min(a,b)+.05), min];
      });
    }''')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    artifacts = Path('artifacts/themes')
    artifacts.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        cached = sorted((Path.home() / '.cache/ms-playwright').glob('chromium-*/chrome-linux64/chrome'))
        browser = p.chromium.launch(executable_path=str(cached[-1]) if cached else None)
        context = browser.new_context(viewport={'width': 1440, 'height': 1080}, color_scheme='light')
        if args.preview:
            preview(context)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        # The existing dashboard has no favicon; still report asset/CSP errors.
        page.on('console', lambda msg: errors.append(msg.text)
                if msg.type == 'error' and msg.location.get('url') != URL + '/favicon.ico' else None)
        page.goto(URL + '/#overview')
        expect(page.locator('#disk-overview .disk-row')).to_have_count(4)
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        expect(page.locator('#theme-select')).to_have_value('system')
        minimum = 100
        for theme in ('light', 'dark', 'warm'):
            page.locator('#theme-select').select_option(theme)
            page.reload()
            expect(page.locator('#theme-select')).to_have_value(theme)
            expect(page.locator('html')).to_have_attribute('data-theme', theme)
            for pair, ratio, required in contrast(page):
                assert ratio >= required, (theme, pair, ratio)
                if required >= 4.5:
                    minimum = min(minimum, ratio)
            for view in ('overview', 'services', 'disks', 'top', 'sessions', 'mcp', 'history', 'audit'):
                page.locator(f'.sidebar [data-view="{view}"]').click()
                expect(page.locator('#refresh')).to_be_enabled()
                expect(page.locator('#view-' + view)).to_be_visible()
                assert not page.locator('#notice').is_visible()
                if view == 'services':
                    page.get_by_role('button', name='관리 ⋯').first.click()
                    expect(page.locator('#dialog')).to_be_visible()
                    assert page.locator('#dialog').evaluate('(e)=>getComputedStyle(e).backgroundColor') == page.locator('.metric').first.evaluate('(e)=>getComputedStyle(e).backgroundColor')
                    page.locator('#dialog-close').click()
                if view == 'disks':
                    expect(page.locator('#view-disks .chart circle').first).to_be_attached()
                    stroke = page.locator('#view-disks .chart circle').first.evaluate('(e)=>getComputedStyle(e).fill')
                    legend = page.locator('#view-disks .series-0').evaluate('(e)=>getComputedStyle(e).color')
                    assert stroke == legend
                for width, height in ((390, 844), (1440, 1080)):
                    page.set_viewport_size({'width': width, 'height': height})
                    expect(page.locator('#theme-select')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, view, width)
            page.locator('.sidebar [data-view="overview"]').click()
            expect(page.locator('#refresh')).to_be_enabled()
            page.screenshot(path=str(artifacts / f'{theme}-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('#theme-select').select_option(theme)
            page.screenshot(path=str(artifacts / f'{theme}-mobile.png'), full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 1080})

        # Manual preference survives OS changes; automatic mode follows them live.
        page.emulate_media(color_scheme='dark')
        expect(page.locator('html')).to_have_attribute('data-theme', 'warm')
        page.locator('#theme-select').select_option('system')
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        page.emulate_media(color_scheme='light')
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')

        other = context.new_page()
        other.goto(URL + '/')
        expect(other.locator('#theme-select')).to_have_value('system')
        page.locator('#theme-select').select_option('dark')
        expect(other.locator('html')).to_have_attribute('data-theme', 'dark')
        expect(other.locator('#theme-select')).to_have_value('dark')
        page.evaluate('localStorage.setItem("wrg-theme", "invalid")')
        page.reload()
        expect(page.locator('#theme-select')).to_have_value('system')
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')

        blocked = browser.new_context(color_scheme='dark')
        if args.preview:
            preview(blocked)
        blocked.add_init_script('Object.defineProperty(window,"localStorage",{get(){throw new Error("Storage disabled")}})')
        private = blocked.new_page()
        private.on('pageerror', lambda error: errors.append(str(error)))
        private.goto(URL + '/')
        expect(private.locator('html')).to_have_attribute('data-theme', 'dark')
        private.locator('#theme-select').select_option('warm')
        expect(private.locator('html')).to_have_attribute('data-theme', 'warm')
        expect(private.locator('#disk-overview .disk-row')).to_have_count(4)
        assert not errors, errors
        browser.close()
    print(f'Themes passed: all views desktop/mobile, persistence, live system changes, cross-tab sync, storage fallback, dialogs/charts; minimum checked text contrast {minimum:.2f}:1')


if __name__ == '__main__':
    main()
