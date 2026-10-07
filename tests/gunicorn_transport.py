"""Real Gunicorn transport checks; uses disposable sockets and a fake controller only."""
import http.client
import json
import os
import re
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://pc.example.ts.net:9443'
IDENTITY = {'Host': 'localhost', 'Tailscale-User-Login': 'owner@example.com'}


def fixture_app():
    from wsl_resource_guard.webapp import create_app
    return create_app({'origin': ORIGIN, 'allowed_logins': ['owner@example.com'],
                       'secret_key': 'isolated-transport-fixture'},
                      control=lambda request: {'fixture': True, 'op': request['op']},
                      sleep=lambda seconds: time.sleep(.05), stream_seconds=.2)


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self.host)


class GunicornTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='wrg-transport-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.path = Path(cls.temporary.name)
        cls.log = (cls.path / 'gunicorn.log').open('w+')
        cls.addClassCleanup(cls.log.close)
        cls.server = subprocess.Popen([
            sys.executable, '-m', 'gunicorn', '--no-control-socket', '--workers', '1',
            '--threads', '4', '--timeout', '10', '--graceful-timeout', '3', '--umask', '0077',
            '--bind', '127.0.0.1:0', '--bind', f'unix:{cls.path}/http.sock',
            '--pythonpath', str(ROOT / 'tests'), 'gunicorn_transport:fixture_app()',
        ], cwd=ROOT, stdout=cls.log, stderr=cls.log,
            start_new_session=True)
        cls.addClassCleanup(cls.stop_server)
        for _ in range(100):
            if cls.server.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError(cls.log.read())
            match = re.search(r'http://127\.0\.0\.1:(\d+)', (cls.path / 'gunicorn.log').read_text())
            if not match:
                time.sleep(.05)
                continue
            cls.port = int(match[1])
            connection = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=.1)
            try:
                connection = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=.1)
                connection.request('GET', '/healthz')
                response = connection.getresponse()
                response.read()
                connection.close()
                if response.status == 200:
                    return
            except OSError:
                time.sleep(.05)
            finally:
                connection.close()
        raise RuntimeError('isolated Gunicorn did not become healthy')

    @classmethod
    def stop_server(cls):
        if cls.server.poll() is None:
            cls.server.send_signal(signal.SIGTERM)
            try:
                cls.server.wait(timeout=6)
            except subprocess.TimeoutExpired:
                os.killpg(cls.server.pid, signal.SIGKILL)
                cls.server.wait(timeout=3)
                raise AssertionError('Gunicorn failed graceful shutdown')
        if cls.server.returncode != 0:
            raise AssertionError(f'Gunicorn exit: {cls.server.returncode}')

    def request(self, path, *, unix=True, method='GET', headers=None, body=None):
        connection = (UnixConnection(str(self.path / 'http.sock')) if unix else
                      http.client.HTTPConnection('127.0.0.1', self.port, timeout=5))
        try:
            connection.request(method, path, body=body, headers=IDENTITY if headers is None else headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_health_and_identity_boundary(self):
        self.assertEqual(self.request('/healthz', unix=False)[0], 200)
        self.assertEqual(self.request('/api/services', unix=False)[0], 403)
        self.assertEqual(self.request('/api/services', headers={'Host': 'localhost'})[0], 403)
        self.assertEqual(self.request('/api/services', headers={**IDENTITY, 'Host': 'evil.example'})[0], 403)
        status, _, payload = self.request('/api/services')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {'fixture': True, 'op': 'list'})
        self.assertEqual((self.path / 'http.sock').stat().st_mode & 0o777, 0o700)

    def test_csrf_and_stream_survive_real_server(self):
        status, headers, payload = self.request('/api/bootstrap')
        self.assertEqual(status, 200)
        token = json.loads(payload)['csrf']
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        post = {**IDENTITY, 'Cookie': cookie, 'Origin': ORIGIN, 'Content-Type': 'application/json'}
        self.assertEqual(self.request('/api/alerts/snooze', method='POST', headers=post,
                                      body='{"minutes":5}')[0], 403)
        post['X-CSRF-Token'] = token
        self.assertEqual(self.request('/api/alerts/snooze', method='POST', headers=post,
                                      body='{"minutes":5}')[0], 200)
        status, headers, payload = self.request('/api/stream')
        self.assertEqual(status, 200)
        self.assertIn('text/event-stream', headers['Content-Type'])
        self.assertIn(b'event: summary', payload)
        self.assertIn(b'"fixture": true', payload)

    def test_ambiguous_http_framing_is_rejected(self):
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as connection:
            connection.sendall(b'POST /healthz HTTP/1.1\r\nHost: localhost\r\n'
                               b'Content-Length: 2\r\nContent-Length: 3\r\n\r\nabc')
            self.assertIn(b' 400 ', connection.recv(4096).split(b'\r\n')[0])


if __name__ == '__main__':
    unittest.main()
