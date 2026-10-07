"""Private Tailscale dashboard; service execution is delegated over a Unix socket."""
from __future__ import annotations

import hmac
import json
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, jsonify, render_template, request, session,
                   stream_with_context)
from werkzeug.exceptions import HTTPException

from .services import ServiceError, ServiceUnavailable, request_control

# Strict policy: no inline styles or scripts; markup must not emit style=
# attributes. JS el.style.* property assignment is not governed by CSP.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")


# gunicorn runs 4 threads; each live stream holds one, so at most two
# streams run at once and the rest of the clients fall back to polling.
STREAM_SLOTS = 2
STREAM_SECONDS = 50
STREAM_POLL_SECONDS = 3


def create_app(config: dict | None = None, control=request_control,
               sleep=time.sleep, stream_seconds: float = STREAM_SECONDS) -> Flask:
    settings = config if config is not None else json.loads(Path('/etc/wsl-resource-guard/web.json').read_text())
    origin = settings['origin'].rstrip('/')
    allowed_host = urlsplit(origin).netloc.lower()
    app = Flask(__name__, template_folder='web', static_folder='web', static_url_path='/assets')
    app.config.update(SECRET_KEY=settings['secret_key'], MAX_CONTENT_LENGTH=16384,
                      SESSION_COOKIE_NAME='wrg_session', SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='Strict',
                      PERMANENT_SESSION_LIFETIME=3600)
    allowed_logins = set(settings['allowed_logins'])
    stream_slots = threading.BoundedSemaphore(STREAM_SLOTS)

    @app.before_request
    def authorize():
        if request.path == '/healthz' and request.remote_addr in ('127.0.0.1', '::1'):
            return None
        # Only the permission-protected Unix listener is trusted to carry Serve
        # identity headers. A local TCP client cannot impersonate the owner.
        incoming_socket = request.environ.get('gunicorn.socket')
        # Serve's Unix-socket proxy rewrites Host to localhost. Mutating requests
        # still require the exact public Origin and a session CSRF token.
        if getattr(incoming_socket, 'family', None) != socket.AF_UNIX or request.host.lower() not in (allowed_host, 'localhost'):
            abort(403, description=f'Tailscale에 연결한 뒤 {origin} 주소로 접속하세요.')
        login = request.headers.get('Tailscale-User-Login', '')
        if login not in allowed_logins:
            abort(403, description='본인 Tailscale 계정으로 연결한 기기에서만 사용할 수 있습니다.')
        if session.get('login') != login:
            session.clear()
            session['login'] = login
            session['csrf'] = secrets.token_urlsafe(32)
            session.permanent = True
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.headers.get('X-CSRF-Token', '')
            if (request.headers.get('Origin') != origin or not request.is_json
                    or not hmac.compare_digest(token, session.get('csrf', ''))):
                abort(403, description='요청을 확인할 수 없습니다. 화면을 새로고침한 뒤 다시 시도하세요.')

    @app.after_request
    def response_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = CSP
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith('/api/'):
            return jsonify(error=error.description), error.code
        return render_template('access.html', message=error.description, origin=origin), error.code

    @app.errorhandler(ServiceUnavailable)
    def control_unavailable(error):
        return jsonify(error=str(error)), 503

    @app.errorhandler(ServiceError)
    def control_error(error):
        return jsonify(error=str(error)), 400

    @app.get('/healthz')
    def health():
        return jsonify(ok=True)

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/sw.js')
    def service_worker():
        # Served from the root so its scope covers the whole dashboard.
        response = app.send_static_file('sw.js')
        response.headers['Content-Type'] = 'text/javascript; charset=utf-8'
        return response

    @app.get('/api/bootstrap')
    def bootstrap():
        return jsonify(csrf=session['csrf'], login=session['login'], origin=origin, refresh_seconds=30)

    @app.get('/api/services')
    def service_list():
        return jsonify(control({'op': 'list'}))

    @app.get('/api/discover')
    def discover():
        return jsonify(control({'op': 'discover'}))

    @app.get('/api/monitor')
    def monitor():
        return jsonify(control({'op': 'monitor', 'force': request.args.get('force') == '1'}))

    @app.get('/api/history')
    def history():
        return jsonify(control({'op': 'history',
                                'range': request.args.get('range', '3h')}))

    @app.get('/api/attribution')
    def attribution():
        return jsonify(control({'op': 'attribution', 'range': request.args.get('range', '3h')}))

    @app.get('/api/session-history')
    def session_history():
        return jsonify(control({'op': 'session-history'}))

    @app.get('/api/alerts')
    def alerts():
        return jsonify(control({'op': 'alerts'}))

    @app.get('/api/settings')
    def settings():
        return jsonify(control({'op': 'settings'}))

    @app.get('/api/disks')
    def disks():
        return jsonify(control({'op': 'disks', 'force': request.args.get('force') == '1'}))

    @app.get('/api/docker-df')
    def docker_df():
        return jsonify(control({'op': 'docker-df'}))

    @app.get('/api/service-memory')
    def service_memory():
        return jsonify(control({'op': 'service-memory'}))

    @app.get('/api/audit')
    def audit():
        return jsonify(control({'op': 'audit'}))

    @app.get('/api/summary')
    def summary():
        return jsonify(control({'op': 'summary'}))

    @app.get('/api/stream')
    def stream():
        """Server-sent summary updates; the client reconnects after each window."""
        if not stream_slots.acquire(blocking=False):
            # 204 tells EventSource to stop without retrying or logging an
            # error; that tab simply keeps its regular polling.
            return Response(status=204)

        def events():
            try:
                yield 'retry: 3000\n\n'
                last, started = None, time.monotonic()
                quiet_since = started
                while time.monotonic() - started < stream_seconds:
                    try:
                        payload = json.dumps(control({'op': 'summary'}), ensure_ascii=False,
                                             sort_keys=True)
                    except Exception as exc:  # The stream reports, then retries.
                        payload = json.dumps({'error': str(exc)}, ensure_ascii=False)
                    if payload != last:
                        last, quiet_since = payload, time.monotonic()
                        yield f'event: summary\ndata: {payload}\n\n'
                    elif time.monotonic() - quiet_since > 15:
                        quiet_since = time.monotonic()
                        yield ': keep-alive\n\n'
                    sleep(STREAM_POLL_SECONDS)
            finally:
                stream_slots.release()

        return Response(stream_with_context(events()), mimetype='text/event-stream',
                        headers={'X-Accel-Buffering': 'no'})

    @app.post('/api/alerts/snooze')
    def snooze():
        body = request.get_json()
        if not isinstance(body, dict) or isinstance(body.get('minutes'), bool) \
                or not isinstance(body.get('minutes'), int):
            abort(400, description='일시 정지 시간을 선택하세요.')
        return jsonify(control({'op': 'snooze', 'minutes': body['minutes'],
                                'actor': session['login']}))

    @app.get('/api/push/key')
    def push_key():
        return jsonify(control({'op': 'push-key'}))

    @app.post('/api/push/subscribe')
    def push_subscribe():
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('subscription'), dict):
            abort(400, description='푸시 구독 정보가 필요합니다.')
        return jsonify(control({'op': 'push-subscribe', 'subscription': body['subscription'],
                                'label': str(body.get('label', ''))[:80], 'actor': session['login']}))

    @app.post('/api/push/status')
    def push_status():
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('subscription'), dict):
            abort(400, description='확인할 푸시 구독 정보가 필요합니다.')
        return jsonify(control({'op': 'push-status', 'subscription': body['subscription']}))

    @app.post('/api/push/unsubscribe')
    def push_unsubscribe():
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('endpoint'), str):
            abort(400, description='해제할 구독이 필요합니다.')
        return jsonify(control({'op': 'push-unsubscribe', 'endpoint': body['endpoint'],
                                'actor': session['login']}))

    @app.post('/api/push/test')
    def push_test():
        return jsonify(control({'op': 'push-test', 'actor': session['login']}))

    @app.post('/api/settings/change')
    def settings_change():
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('key'), str) \
                or isinstance(body.get('value'), bool) or not isinstance(body.get('value'), (int, float)):
            abort(400, description='바꿀 설정과 숫자 값을 입력하세요.')
        return jsonify(control({'op': 'config-change', 'key': body['key'], 'value': body['value'],
                                'actor': session['login']}))

    @app.post('/api/sessions/kill-stale')
    def kill_stale():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or body.get('confirmed') is not True or not isinstance(body.get('pids'), list):
            abort(400, description='종료 확인이 필요합니다.')
        return jsonify(control({'op': 'kill-stale', 'pids': body['pids'], 'confirmed': True,
                                'actor': session['login']}))

    @app.get('/api/services/<identifier>/logs')
    def logs(identifier):
        try:
            lines = int(request.args.get('lines', '200'))
        except ValueError:
            lines = 200
        return jsonify(control({'op': 'logs', 'id': identifier, 'lines': lines}))

    @app.post('/api/services/register')
    def register():
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('key'), str):
            abort(400, description='등록 대상을 선택하세요.')
        payload = {'op': 'register', 'key': body['key'], 'actor': session['login']}
        for field in ('name', 'url'):
            if field in body:
                payload[field] = body[field]
        return jsonify(control(payload))

    @app.post('/api/services/<identifier>/action')
    def action(identifier):
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('action'), str):
            abort(400, description='작업을 선택하세요.')
        return jsonify(control({'op': 'action', 'id': identifier, 'action': body['action'],
                                'confirmed': body.get('confirmed') is True, 'actor': session['login']}))

    @app.post('/api/sessions/<int:pid>/kill')
    def kill_session(pid):
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or body.get('confirmed') is not True:
            abort(400, description='종료 확인이 필요합니다.')
        return jsonify(control({'op': 'kill', 'pid': pid, 'confirmed': True,
                                'actor': session['login']}))

    return app
