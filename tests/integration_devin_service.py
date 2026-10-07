"""Exercise the packaged unit with disposable processes and a user systemd manager.

Run explicitly: .venv/bin/python tests/integration_devin_service.py
No production service, state directory, port, or root permission is used.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import subprocess
import tempfile
import threading
import time
import uuid


class Health(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *args):
        pass


def ctl(*args, check=True):
    return subprocess.run(['systemctl', '--user', *args], check=check,
                          capture_output=True, text=True, timeout=30)


def main():
    name = f'wrg-devin-test-{uuid.uuid4().hex}.service'
    link = Path.home() / '.config/systemd/user' / name
    server = ThreadingHTTPServer(('127.0.0.1', 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix='wrg-devin-') as directory:
        root = Path(directory)
        fake = root / 'ctl'
        fake.write_text('''#!/usr/bin/python3
from pathlib import Path
import subprocess, sys
root = Path(__file__).parent
if sys.argv[1] == 'start':
    child = subprocess.Popen(['/usr/bin/sleep', '3600'], start_new_session=True)
    (root / 'pid').write_text(str(child.pid))
    sys.exit(1 if (root / 'fail-start').exists() else 0)
else:
    with (root / 'stops').open('a') as log:
        log.write(' '.join(sys.argv[1:]) + '\\n')
    # Deliberately leave the detached child for systemd's cgroup cleanup.
''')
        fake.chmod(0o755)
        template = (Path(__file__).resolve().parents[1] / 'packaging/examples/devin-web.service.in').read_text()
        template = template.replace('@OWNER@', 'demo').replace('@GROUP@', 'demo')
        unit = template.replace('@HOME@/projects/devin-web/bin/devin-web-ctl', str(fake))
        unit = unit.replace('@HOME@/projects/devin-web', directory)
        unit = '\n'.join(line for line in unit.splitlines()
                         if not line.startswith(('User=', 'Group='))) + '\n'
        unit = unit.replace('@HOME@', str(Path.home()))
        unit = unit.replace('127.0.0.1:7100', f'127.0.0.1:{server.server_port}')
        unit = unit.replace('TimeoutStopSec=60', 'TimeoutStopSec=2')
        path = root / name
        path.write_text(unit)

        def pid():
            return int((root / 'pid').read_text())

        def gone(value):
            for _ in range(50):
                stat = Path(f'/proc/{value}/stat')
                if not stat.exists() or stat.read_text().split(') ')[1].startswith('Z '):
                    return
                time.sleep(0.1)
            raise AssertionError(f'detached child survived: {value}')

        try:
            ctl('link', str(path))
            ctl('daemon-reload')
            ctl('start', name)
            first = pid()
            ctl('restart', name)
            second = pid()
            assert first != second
            gone(first)
            ctl('stop', name)
            gone(second)
            assert set((root / 'stops').read_text().splitlines()) == {'stop --all'}
            print('PASS: restart replaces detached child; stop clears child; full-stop arguments')

            for failure in ('http', 'start'):
                Health.status = 503 if failure == 'http' else 200
                if failure == 'start':
                    (root / 'fail-start').touch()
                stops_before = len((root / 'stops').read_text().splitlines())
                result = ctl('start', name, check=False)
                assert result.returncode != 0, f'{failure} failure reported success'
                gone(pid())
                assert len((root / 'stops').read_text().splitlines()) > stops_before
                assert ctl('is-failed', name, check=False).stdout.strip() == 'failed'
                ctl('reset-failed', name)
                print(f'PASS: {failure} failure is reported and cleaned via ExecStopPost')
        finally:
            ctl('stop', name, check=False)
            ctl('reset-failed', name, check=False)
            link.unlink(missing_ok=True)
            ctl('daemon-reload')
            server.shutdown()
            server.server_close()
            thread.join()
    assert not link.exists()


if __name__ == '__main__':
    main()
