"""Single pending request, exact outcomes, and transport error contracts."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import pwd
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wsl_resource_guard.config import Settings
from wsl_resource_guard.daemon import apply_config_request
from wsl_resource_guard.owner_worker import OwnerData, dispatch
from wsl_resource_guard.service_control import Controller, ControlError, ControlHandler, atomic_json
from wsl_resource_guard.services import ServiceError, request_control
from wsl_resource_guard.webapp import create_app


class ConfigRequestsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = self.root / 'home'
        config = home / '.config/wsl-resource-guard/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text(f'state_dir = "{self.root / "state"}"\nwarning_available_gib = 4.0\n')
        self.settings = Settings.load(config)
        registry = self.root / 'registry.json'
        atomic_json(registry, {'owner': pwd.getpwuid(os.getuid()).pw_name, 'services': []})
        self.controllers = [Controller(registry, run=Mock(side_effect=AssertionError('No commands')),
                                       shared=self.root / 'shared') for _ in range(2)]
        for c in self.controllers:
            c._owner_read = lambda op, args=None: dispatch(op, args or {}, OwnerData(home))
        self.c = self.controllers[0]

    def apply(self, **kwargs):
        return apply_config_request(self.settings, self.c.shared, **kwargs)

    def test_pending_request_is_not_overwritten_then_next_request_can_apply(self):
        first = self.c.request_config_change('warning_available_gib', 6)
        with self.assertRaises(ControlError) as error:
            self.c.request_config_change('email_heartbeat_minute', 30)
        self.assertEqual(error.exception.code, 'config_pending')
        self.assertEqual(self.c.config_request_status(first['id'])['state'], 'pending')
        self.assertEqual(self.apply()['id'], first['id'])
        second = self.c.request_config_change('email_heartbeat_minute', 30)
        # Last completed result remains addressable while the next request waits.
        self.assertEqual(self.c.config_request_status(first['id'])['state'], 'applied')
        self.assertEqual(self.c.config_request_status(second['id'])['state'], 'pending')
        self.assertEqual(self.apply()['id'], second['id'])
        saved = Settings.load(self.settings.config_path)
        self.assertEqual((saved.warning_available_gib, saved.email_heartbeat_minute), (6, 30))
        with self.assertRaises(ControlError) as gone:
            self.c.config_request_status(first['id'])
        self.assertEqual(gone.exception.code, 'config_not_found')

    def test_two_controllers_accept_exactly_one_concurrent_request(self):
        barrier = threading.Barrier(2)
        def submit(c):
            barrier.wait(timeout=3)
            try:
                return c.request_config_change('warning_available_gib', 6)
            except ControlError as exc:
                return exc.code
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(submit, self.controllers))
        accepted = [r for r in results if isinstance(r, dict)]
        self.assertEqual(len(accepted), 1, results)
        self.assertIn('config_pending', results)
        self.assertEqual(self.apply()['id'], accepted[0]['id'])

    def test_guard_stopped_request_expires_only_when_guard_handles_it(self):
        with patch('wsl_resource_guard.service_control.time.time', return_value=1000):
            first = self.c.request_config_change('warning_available_gib', 6)
        with patch('wsl_resource_guard.service_control.time.time', return_value=2000):
            with self.assertRaises(ControlError):
                self.c.request_config_change('email_heartbeat_minute', 30)
        self.assertEqual(self.c.config_request_status(first['id'])['state'], 'pending')
        self.assertFalse(self.apply(now=2000)['ok'])
        self.assertEqual(self.c.config_request_status(first['id'])['state'], 'failed')
        self.c.request_config_change('email_heartbeat_minute', 30)

    def test_unreadable_or_malformed_request_never_becomes_an_empty_slot(self):
        self.c.shared.mkdir()
        path = self.c.shared / 'config-request.json'
        for raw in ('not json', '[]', '{}', '{"id": "bad"}'):
            path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(ControlError):
                self.c.request_config_change('warning_available_gib', 6)
            self.assertEqual(path.read_text(), raw)
        path.unlink()
        path.mkdir()
        with self.assertRaises(ControlError):
            self.c.request_config_change('warning_available_gib', 6)

    def test_result_read_failure_does_not_release_slot(self):
        first = self.c.request_config_change('warning_available_gib', 6)
        read = self.c._owner_read
        self.c._owner_read = lambda op, args=None: (_ for _ in ()).throw(ControlError('read failed')) if op == 'config-result' else read(op, args)
        with self.assertRaises(ControlError):
            self.c.request_config_change('email_heartbeat_minute', 30)
        self.assertEqual(self.c.read_shared('config-request.json')['id'], first['id'])

    def test_status_includes_timing_and_rejects_invalid_ids(self):
        request = self.c.request_config_change('warning_available_gib', 6)
        status = self.c.config_request_status(request['id'])
        self.assertEqual(status['id'], request['id'])
        self.assertEqual(status['interval_seconds'], 15)
        self.assertIsNone(status['daemon_updated_at'])
        for invalid in ('../state', '', 123, 'f' * 100):
            with self.subTest(invalid=invalid), self.assertRaises(ControlError):
                self.c.config_request_status(invalid)


class ConfigTransportTests(unittest.TestCase):
    def test_control_handler_and_client_preserve_only_known_error_codes(self):
        for code in ('config_pending', 'config_not_found', 'config_unavailable'):
            handler = object.__new__(ControlHandler)
            handler.request = Mock()
            import struct
            handler.request.getsockopt.return_value = struct.pack('3i', 1, 1000, 1000)
            handler.server = SimpleNamespace(allowed_uids={1000}, web_uid=1000,
                controller=Mock(dispatch=Mock(side_effect=ControlError('fixture', code=code))))
            handler.rfile = io.BytesIO(b'{"op":"config-change"}\n')
            handler.wfile = io.BytesIO()
            handler.handle()
            response = handler.wfile.getvalue()
            self.assertEqual(json.loads(response)['code'], code)
            connection = Mock()
            connection.__enter__ = Mock(return_value=connection)
            connection.__exit__ = Mock(return_value=False)
            connection.makefile.return_value = io.BytesIO(response)
            with patch('wsl_resource_guard.services.socket.socket', return_value=connection):
                with self.assertRaises(ServiceError) as error:
                    request_control({'op': 'config-change'})
            self.assertEqual(error.exception.code, code)

    def test_http_exact_status_and_request_id_contract(self):
        control = Mock(return_value={})
        origin = 'https://fixture.example'
        app = create_app({'origin': origin, 'allowed_logins': ['owner@example.com'], 'secret_key': 'fixture'}, control)
        client = app.test_client()
        environ = {'gunicorn.socket': SimpleNamespace(family=socket.AF_UNIX)}
        headers = {'Tailscale-User-Login': 'owner@example.com'}
        token = client.get('/api/bootstrap', base_url=origin, headers=headers, environ_overrides=environ).json['csrf']
        for code, status in (('config_pending', 409), ('config_not_found', 404), ('config_unavailable', 503)):
            control.side_effect = ServiceError('fixture', code=code)
            reply = client.get('/api/settings/request?id=0123456789abcdef', base_url=origin,
                               headers=headers, environ_overrides=environ)
            self.assertEqual((reply.status_code, reply.json['code']), (status, code))
            control.assert_called_with({'op': 'config-request', 'id': '0123456789abcdef'})
        control.side_effect = ServiceError('pending', code='config_pending')
        reply = client.post('/api/settings/change', base_url=origin, environ_overrides=environ,
                            headers={**headers, 'Origin': origin, 'X-CSRF-Token': token},
                            json={'key': 'warning_available_gib', 'value': 6})
        self.assertEqual(reply.status_code, 409)


if __name__ == '__main__':
    unittest.main()
