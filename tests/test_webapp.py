import unittest
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

try:
    from wsl_resource_guard.webapp import create_app
except ImportError:
    create_app = None


@unittest.skipIf(create_app is None, 'Install requirements-web.txt to test the dashboard')
class WebTests(unittest.TestCase):
    def setUp(self):
        self.control = Mock(return_value={'services': [], 'origin': 'https://pc.example.ts.net:9443'})
        self.origin = 'https://pc.example.ts.net:9443'
        self.app = create_app({'origin': self.origin, 'allowed_logins': ['owner@example.com'],
                               'secret_key': 'test-only-not-a-production-key'}, self.control)
        self.app.testing = True
        self.client = self.app.test_client()
        self.headers = {'Tailscale-User-Login': 'owner@example.com'}
        self.environ = {'REMOTE_ADDR': '', 'gunicorn.socket': SimpleNamespace(family=socket.AF_UNIX)}

    def get(self, path, headers=None, environ=None):
        return self.client.get(path, base_url=self.origin, headers=self.headers if headers is None else headers,
                               environ_overrides=self.environ if environ is None else environ)

    def token(self):
        return self.get('/api/bootstrap').json['csrf']

    def test_missing_identity_is_denied(self):
        self.assertEqual(self.get('/api/services', headers={}).status_code, 403)
        self.control.assert_not_called()

    def test_other_tailnet_user_is_denied(self):
        self.assertEqual(self.get('/api/services', headers={'Tailscale-User-Login': 'other@example.com'}).status_code, 403)
        self.control.assert_not_called()

    def test_direct_network_requests_cannot_spoof_headers(self):
        self.assertEqual(self.get('/api/services', environ={'REMOTE_ADDR':'100.64.0.10'}).status_code, 403)

    def test_local_tcp_client_cannot_spoof_owner_identity(self):
        self.assertEqual(self.get('/api/services', environ={'REMOTE_ADDR':'127.0.0.1',
            'gunicorn.socket':SimpleNamespace(family=socket.AF_INET)}).status_code,403)
        self.control.assert_not_called()

    def test_serve_unix_proxy_localhost_host_is_supported(self):
        result = self.client.get('/api/services', base_url='https://localhost', headers=self.headers,
                                 environ_overrides=self.environ)
        self.assertEqual(result.status_code, 200)

    def test_wrong_host_is_denied(self):
        result = self.client.get('/api/services', base_url='https://evil.example', headers=self.headers,
                                 environ_overrides=self.environ)
        self.assertEqual(result.status_code, 403)

    def test_missing_csrf_is_denied(self):
        result = self.client.post('/api/services/compose-demo/action', base_url=self.origin,
                                  headers={**self.headers, 'Origin':self.origin}, json={'action':'stop'},
                                  environ_overrides=self.environ)
        self.assertEqual(result.status_code, 403)
        self.control.assert_not_called()

    def test_wrong_origin_is_denied_even_with_csrf(self):
        token = self.token()
        result = self.client.post('/api/services/demo/action', base_url=self.origin,
                                  headers={**self.headers,'Origin':'https://evil.example','X-CSRF-Token':token},
                                  json={'action':'stop'}, environ_overrides=self.environ)
        self.assertEqual(result.status_code, 403)
        self.control.assert_not_called()

    def test_authenticated_confirmed_action_uses_fixed_operation(self):
        token = self.token()
        result = self.client.post('/api/services/demo/action', base_url=self.origin,
                                  headers={**self.headers,'Origin':self.origin,'X-CSRF-Token':token},
                                  json={'action':'stop','confirmed':True,'op':'shell','source':'cli'},
                                  environ_overrides=self.environ)
        self.assertEqual(result.status_code, 200)
        self.control.assert_called_once_with({'op':'action','id':'demo','action':'stop','confirmed':True,'actor':'owner@example.com'})

    def test_confirmed_string_is_not_confirmation(self):
        token = self.token()
        self.client.post('/api/services/demo/action', base_url=self.origin,
                         headers={**self.headers,'Origin':self.origin,'X-CSRF-Token':token},
                         json={'action':'remove','confirmed':'false'}, environ_overrides=self.environ)
        self.assertFalse(self.control.call_args.args[0]['confirmed'])

    def test_kill_endpoints_reject_every_nonobject_json_without_control(self):
        token = self.token()
        self.control.reset_mock()
        for endpoint in ('/api/sessions/1234/kill', '/api/sessions/kill-stale'):
            for body in ([], [1], 'text', 42, True, None):
                with self.subTest(endpoint=endpoint, body=body):
                    result = self.client.post(endpoint, base_url=self.origin,
                        headers={**self.headers, 'Origin': self.origin, 'X-CSRF-Token': token},
                        data=json.dumps(body), content_type='application/json', environ_overrides=self.environ)
                    self.assertEqual(result.status_code, 400)
                    self.control.assert_not_called()

    def test_session_kill_forwards_exact_pid(self):
        token = self.token()
        result = self.client.post('/api/sessions/1234567/kill', base_url=self.origin,
                                  headers={**self.headers, 'Origin': self.origin, 'X-CSRF-Token': token},
                                  json={'confirmed': True}, environ_overrides=self.environ)
        self.assertEqual(result.status_code, 200)
        self.control.assert_called_once_with({'op': 'kill', 'pid': 1234567, 'confirmed': True,
                                              'actor': 'owner@example.com'})

    def test_session_kill_rejects_formatted_pid_and_missing_confirmation(self):
        token = self.token()
        headers = {**self.headers, 'Origin': self.origin, 'X-CSRF-Token': token}
        for path, body in (('/api/sessions/12,345/kill', {'confirmed': True}),
                           ('/api/sessions/NaN/kill', {'confirmed': True}),
                           ('/api/sessions/12345/kill', {'confirmed': 'true'})):
            with self.subTest(path=path, body=body):
                result = self.client.post(path, base_url=self.origin, headers=headers, json=body,
                                          environ_overrides=self.environ)
                self.assertIn(result.status_code, (400, 404))
        self.control.assert_not_called()

    def test_controller_unreachable_is_503_not_400(self):
        from wsl_resource_guard.services import ServiceError, ServiceUnavailable
        self.control.side_effect = ServiceUnavailable('socket missing')
        result = self.get('/api/services')
        self.assertEqual(result.status_code, 503)
        self.assertIn('socket missing', result.json['error'])
        self.control.side_effect = ServiceError('등록된 서비스를 찾을 수 없습니다.')
        result = self.get('/api/services')
        self.assertEqual(result.status_code, 400)

    def test_logs_forwards_lines_to_controller(self):
        self.assertEqual(self.get('/api/services/demo/logs?lines=500').status_code, 200)
        self.control.assert_called_with({'op': 'logs', 'id': 'demo', 'lines': 500})
        self.get('/api/services/demo/logs?lines=abc')
        self.control.assert_called_with({'op': 'logs', 'id': 'demo', 'lines': 200})

    def test_read_only_monitor_routes(self):
        for path, payload in [('/api/monitor',{'op':'monitor','force':False}),
                              ('/api/monitor?force=1',{'op':'monitor','force':True}),
                              ('/api/history',{'op':'history','range':'3h'}),
                              ('/api/history?range=7d',{'op':'history','range':'7d'}),
                              ('/api/alerts',{'op':'alerts'}),
                              ('/api/settings',{'op':'settings'})]:
            self.assertEqual(self.get(path).status_code, 200)
            self.control.assert_called_with(payload)

    def test_disk_routes_are_authenticated_read_only(self):
        self.assertEqual(self.get('/api/disks?force=1').status_code, 200)
        self.control.assert_called_with({'op': 'disks', 'force': True})
        self.assertEqual(self.get('/api/disks', headers={}).status_code, 403)

    def test_security_headers_and_cookie(self):
        response = self.get('/api/bootstrap')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        cookie = response.headers['Set-Cookie']
        for value in ('Secure','HttpOnly','SameSite=Strict'):
            self.assertIn(value,cookie)

    def test_csp_has_no_unsafe_inline(self):
        response = self.get('/')
        csp = response.headers['Content-Security-Policy']
        self.assertNotIn('unsafe-inline', csp)
        self.assertNotIn('style-src-attr', csp)

    def test_identity_is_checked_for_each_request(self):
        self.token()
        self.assertEqual(self.get('/api/services',headers={'Tailscale-User-Login':'other@example.com'}).status_code,403)

    def test_large_body_is_rejected(self):
        token = self.token()
        response = self.client.post('/api/services/demo/action',base_url=self.origin,
                                    headers={**self.headers,'Origin':self.origin,'X-CSRF-Token':token},
                                    json={'action':'x'*20000},environ_overrides=self.environ)
        self.assertEqual(response.status_code,413)

    def test_page_and_assets_require_identity(self):
        self.assertEqual(self.get('/').status_code,200)
        with self.get('/assets/app.js') as response:
            self.assertEqual(response.status_code,200)
        self.assertEqual(self.get('/assets/app.js',headers={}).status_code,403)


@unittest.skipIf(create_app is None, 'Install requirements-web.txt to test the dashboard')
class NewRouteTests(unittest.TestCase):
    setUp, get, token = WebTests.setUp, WebTests.get, WebTests.token

    def post(self, path, body):
        return self.client.post(path, base_url=self.origin, json=body, environ_overrides=self.environ,
                                headers={**self.headers, 'Origin': self.origin, 'X-CSRF-Token': self.token()})

    def test_service_worker_is_served_from_root_as_javascript(self):
        result = self.get('/sw.js')
        self.assertEqual(result.status_code, 200)
        self.assertIn('javascript', result.headers['Content-Type'])
        self.assertIn('showNotification', result.get_data(as_text=True))

    def test_new_writes_validate_types_before_reaching_the_controller(self):
        for path, body in (('/api/alerts/snooze', {'minutes': True}),
                           ('/api/alerts/snooze', {'minutes': '60'}),
                           ('/api/settings/change', {'key': 'x', 'value': True}),
                           ('/api/sessions/kill-stale', {'pids': [1]}),
                           ('/api/push/subscribe', {'subscription': 'x'}),
                           ('/api/push/unsubscribe', {})):
            self.control.reset_mock()
            self.assertEqual(self.post(path, body).status_code, 400, path)
            self.control.assert_not_called()
        self.post('/api/alerts/snooze', {'minutes': 60})
        self.assertEqual(self.control.call_args.args[0]['op'], 'snooze')
        self.assertEqual(self.control.call_args.args[0]['actor'], 'owner@example.com')

    def test_new_writes_require_csrf(self):
        result = self.client.post('/api/push/test', base_url=self.origin, json={},
                                  headers={**self.headers, 'Origin': self.origin},
                                  environ_overrides=self.environ)
        self.assertEqual(result.status_code, 403)
        self.control.assert_not_called()


@unittest.skipIf(create_app is None, 'Install requirements-web.txt to test the dashboard')
class StreamTests(unittest.TestCase):
    def setUp(self):
        self.origin = 'https://pc.example.ts.net:9443'
        self.summaries = [{'severity': 'normal'}, {'severity': 'normal'}, {'severity': 'warning'}]
        self.control = Mock(side_effect=lambda req: self.summaries.pop(0) if self.summaries else {'severity': 'warning'})
        self.ticks = []
        self.app = create_app({'origin': self.origin, 'allowed_logins': ['owner@example.com'],
                               'secret_key': 'test-only-not-a-production-key'}, self.control,
                              sleep=self.ticks.append, stream_seconds=0.05)
        self.app.testing = True
        self.client = self.app.test_client()
        self.headers = {'Tailscale-User-Login': 'owner@example.com'}
        self.environ = {'REMOTE_ADDR': '', 'gunicorn.socket': SimpleNamespace(family=socket.AF_UNIX)}

    def open(self):
        return self.client.get('/api/stream', base_url=self.origin, headers=self.headers,
                               environ_overrides=self.environ, buffered=True)

    def test_stream_sends_only_changes_and_frees_its_slot(self):
        import time as _time
        self.app.config['STREAM_STARTED'] = _time.monotonic()
        result = self.open()
        self.assertEqual(result.mimetype, 'text/event-stream')
        body = result.get_data(as_text=True)
        self.assertTrue(body.startswith('retry: 3000'))
        events = [line for line in body.splitlines() if line.startswith('data: ')]
        # normal, normal (unchanged: not repeated), warning ...
        self.assertEqual(events[0], 'data: {"severity": "normal"}')
        self.assertEqual(events[1], 'data: {"severity": "warning"}')
        # Slots are released: more streams than slots can run one after another.
        for _ in range(3):
            self.assertEqual(self.open().status_code, 200)

    def test_streams_over_the_cap_get_204_instead_of_an_error(self):
        import threading as _threading
        slots = [cell.cell_contents for cell in self.app.view_functions['stream'].__closure__
                 if isinstance(cell.cell_contents, _threading.BoundedSemaphore)][0]
        while slots.acquire(blocking=False):
            pass
        self.assertEqual(self.open().status_code, 204)

    def test_stream_requires_identity(self):
        result = self.client.get('/api/stream', base_url=self.origin, headers={},
                                 environ_overrides=self.environ)
        self.assertEqual(result.status_code, 403)
