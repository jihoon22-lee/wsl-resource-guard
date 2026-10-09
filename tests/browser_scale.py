"""Offline scale acceptance and timings; budgets are reported, not CI gates.

Run: .venv/bin/python tests/browser_scale.py --iterations 20
Use --browser webkit for a second engine; only Chromium supports CPU throttling.
Temporary history inputs are removed automatically. Output is JSON on stdout.
"""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_offline import ORIGIN, fixtures, serve, session_row
from wsl_resource_guard.history import read_history_downsampled, MAX_RESULT_BYTES
from wsl_resource_guard.owner_worker import OwnerData, dispatch
from wsl_resource_guard.service_control import Controller
from wsl_resource_guard.webapp import create_app


def history_input(interval):
    """Use the actual bounded streaming reader, not a mocked aggregation."""
    base = int(time.time() // 3600) * 3600 - 14 * 86400
    metrics = fixtures()['/api/monitor']['metrics']
    nominal = 14 * 86400 // interval
    hole = base + 120 * 3600
    with tempfile.TemporaryDirectory(prefix='wrg-scale-history-') as temporary:
        root = Path(temporary)
        handles = {}
        written = 0
        try:
            for index in range(nominal):
                timestamp = base + index * interval
                if hole <= timestamp < hole + 3600:
                    continue
                missing = base + 48 * 3600 <= timestamp < base + 49 * 3600
                row = {'timestamp': timestamp, 'severity': 'critical' if index == nominal - 2 else 'normal',
                       'reasons': ['fixture RAM pressure'] if index == nominal - 2 else [],
                       'metrics': {**metrics, 'psi_status': 'missing' if missing else 'normal',
                                   'psi_some_avg60': None if missing else 0.1,
                                   'psi_full_avg60': None if missing else 0.0}}
                name = datetime.fromtimestamp(timestamp).strftime('history-%Y-%m-%d.jsonl')
                if name not in handles:
                    handles[name] = (root / name).open('w')
                handles[name].write(json.dumps(row) + '\n')
                written += 1
        finally:
            for handle in handles.values():
                handle.close()
        started = time.perf_counter()
        rows = read_history_downsampled(root, base, 3600)
        cold_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        again = read_history_downsampled(root, base, 3600)
        warm_ms = (time.perf_counter() - started) * 1000
        size = len(json.dumps(rows).encode())
        assert rows == again and len(rows) == 335
        assert not any(hole <= r['timestamp'] < hole + 3600 for r in rows)
        assert rows[-1]['severity'] == 'critical'
        assert any(r['metrics']['psi_some_avg60'] is None and r['metrics']['psi_status'] == 'missing' for r in rows)
        assert size < MAX_RESULT_BYTES
        # Also measure Flask -> Controller -> actual OwnerData against these
        # files. The transport/identity and owner privilege boundary are
        # fixtures here; this is not real Tailscale/Gunicorn latency.
        home = root / 'home'
        config = home / '.config/wsl-resource-guard/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text(f'state_dir = "{root}"\n')
        reader = OwnerData(home)
        controller = Controller(root / 'registry.json', run=Mock(side_effect=AssertionError('No service commands')),
                                shared=root / 'shared')
        controller._owner_read = lambda op, args=None: dispatch(op, args or {}, reader)
        origin = 'https://fixture.example'
        app = create_app({'origin': origin, 'allowed_logins': ['owner@example.com'], 'secret_key': 'fixture'},
                         control=lambda request: controller.dispatch(request, uid=1000, web_uid=1000))
        client = app.test_client()
        timings = []
        for _ in range(2):
            started = time.perf_counter()
            response = client.get('/api/history?range=14d', base_url=origin,
                                  headers={'Tailscale-User-Login': 'owner@example.com'},
                                  environ_overrides={'gunicorn.socket': SimpleNamespace(family=socket.AF_UNIX)})
            timings.append(round((time.perf_counter() - started) * 1000, 1))
            assert response.status_code == 200 and response.json[-1]['severity'] == 'critical'
            assert len(response.data) < MAX_RESULT_BYTES
        return rows, {'interval_seconds': interval, 'nominal_records': nominal,
                      'stored_records': written, 'buckets': len(rows), 'bytes': size,
                      'cold_ms': round(cold_ms, 1), 'warm_ms': round(warm_ms, 1),
                      'api_fixture_cold_ms': timings[0], 'api_fixture_warm_ms': timings[1]}


def scale_fixtures(service_count, session_count, history):
    data = fixtures()
    template = data['/api/services']['services'][0]
    data['/api/services']['services'] = [
        {**template, 'id': f'app-{i}', 'name': f'Scenario app {i} ' + '긴이름-' * 10,
         'target': f'app-{i}.service', 'health': {'ok': True, 'status': 200},
         'error': 'fixture 상세 진단 메시지 ' * 8 if i % 10 == 0 else '',
         'memory_bytes': (i + 1) * 1024 * 1024}
        for i in range(service_count)]
    now = int(time.time())
    data['/api/service-memory'] = [
        {'timestamp': now - (287 - i) * 300,
         'services': {f'app-{j}': (j + 1) * 1024 * 1024 for j in range(service_count)}}
        for i in range(288)]
    data['/api/monitor']['sessions'] = [
        {**session_row(2000000 + i), 'project': f'scenario-{i}', 'rss_kib': i * 1024}
        for i in range(session_count)]
    data['/api/session-history'] = []
    data['/api/history'] = history
    data['/api/attribution'] = {'keys': [], 'rows': []}
    return data


def p95(values):
    return round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 1)


def browser_case(browser, engine, mobile, service_count, session_count, history, iterations):
    data = scale_fixtures(service_count, session_count, history)
    context = browser.new_context(viewport={'width': 390 if mobile else 1440, 'height': 900}, service_workers='block',
                                  is_mobile=mobile, has_touch=mobile)
    context.add_init_script("localStorage.setItem('wrg-refresh','0');localStorage.setItem('wrg-history-range','14d')")
    page = context.new_page()
    posts, gets, errors = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route(ORIGIN + '/**', lambda route: serve(route, posts, gets, data))
    if engine == 'chromium' and mobile:
        context.new_cdp_session(page).send('Emulation.setCPUThrottlingRate', {'rate': 4})
    results = []
    try:
        for view in ('services', 'sessions', 'history'):
            page.goto(ORIGIN + '/#' + view)
            expect(page.locator('#view-' + view)).to_be_visible()
            expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
            page.wait_for_timeout(500)
            assert not errors, errors
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (view, mobile)
            if view == 'services':
                expect(page.locator('[data-manage]')).to_have_count(service_count)
            if view == 'sessions':
                expect(page.locator('[data-kill]')).to_have_count(session_count)
            measured = page.evaluate('''async ({iterations, view}) => {
              const renderMs = [], actionMs = [];
              const frame = () => new Promise(r => requestAnimationFrame(r));
              for (let i = 0; i <= iterations; i++) {
                await frame();
                let start = performance.now();
                render();
                document.querySelector('#view-' + view).getBoundingClientRect();
                await frame(); await frame();
                if (i) renderMs.push(performance.now() - start);
                start = performance.now();
                if (view === 'sessions') {
                  const input = document.querySelector('#session-search');
                  input.value = i % 2 ? 'scenario-19' : '';
                  input.dispatchEvent(new Event('input', {bubbles:true}));
                } else {
                  document.querySelector('#view-' + view + ' .th-sort')?.click();
                }
                document.querySelector('#view-' + view).getBoundingClientRect();
                await frame(); await frame();
                if (i) actionMs.push(performance.now() - start);
              }
              return {renderMs, actionMs};
            }''', {'iterations': iterations, 'view': view})
            render_p95, action_p95 = p95(measured['renderMs']), p95(measured['actionMs'])
            item = {'engine': engine, 'profile': 'mobile-4x' if mobile and engine == 'chromium' else 'mobile' if mobile else 'desktop',
                    'services': service_count, 'sessions': session_count, 'view': view,
                    'render_p95_ms': render_p95, 'action_p95_ms': action_p95,
                    'within_target': render_p95 <= (3000 if mobile else 1000) and action_p95 <= (1000 if mobile else 300)}
            assert not errors, errors
            # Exercise a real UI confirmation and verify its fixed target.
            if view == 'services':
                page.locator('[data-manage="app-0"]').click()
                page.locator('[data-action="restart"]').click()
                page.locator('#confirm-action').click()
                expect(page.locator('#dialog')).not_to_be_visible()
                assert posts[-1] == ('/api/services/app-0/action', {'action': 'restart', 'confirmed': True})
            elif view == 'sessions':
                page.locator('#session-search').fill('scenario-199')
                page.locator('[data-kill="2000199"]').click()
                page.locator('#confirm-action').click()
                expect(page.locator('#dialog')).not_to_be_visible()
                assert posts[-1] == ('/api/sessions/2000199/kill', {'confirmed': True})
            results.append(item)
            print(json.dumps(item), flush=True)
    finally:
        context.close()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', choices=('chromium', 'webkit'), default='chromium')
    parser.add_argument('--iterations', type=int, default=20)
    parser.add_argument('--history-only', action='store_true', help='measure the temporary history/API path without a browser')
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('iterations must be positive')
    for interval in (60, 10):
        history, result = history_input(interval)
        print(json.dumps({'backend_history': result}), flush=True)
    if args.history_only:
        return
    with sync_playwright() as playwright:
        browser = getattr(playwright, args.browser).launch()
        print(json.dumps({'browser': args.browser, 'version': browser.version, 'iterations': args.iterations}), flush=True)
        try:
            for mobile in (False, True):
                for services, sessions in ((50, 200), (500, 2000)):
                    browser_case(browser, args.browser, mobile, services, sessions, history, args.iterations)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
