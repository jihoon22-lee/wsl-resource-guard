"""Disposable lifecycle test for a Compose project bound to an owner startup unit.

Uses only fixture names: an alpine container without network, mounts or ports,
throwaway owner user units and a scratch registry. Run the phases in order:

  python3 tests/integration_launcher.py setup                    # as the owner
  sudo systemd-run --wait --pipe -p ProtectSystem=strict -p ProtectHome=read-only \\
      -p NoNewPrivileges=yes -p ReadWritePaths=/var/tmp/wrg-launcher-probe \\
      -p WorkingDirectory="$PWD" /usr/bin/python3 tests/integration_launcher.py run --owner "$USER"
  python3 tests/integration_launcher.py cleanup                  # as the owner

The run phase uses the boot recovery unit's sandbox, so it also proves that the
owner's manager can be driven without write access to the owner's home.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path('/var/tmp/wrg-launcher-probe')
STATE = ROOT / 'root'
PROJECT, BROKEN = 'wrgprobe', 'wrgprobe-broken'
UNIT, HEALTH = f'{PROJECT}-startup.service', f'{PROJECT}-health.service'
IMAGE = 'alpine:3.23'
UNIT_DIR = Path.home() / '.config/systemd/user'
LOG = ROOT / 'startup.log'


def sh(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True).stdout.strip()


def controller():
    from wsl_resource_guard.service_control import Controller
    c = Controller(STATE / 'registry.json', shared=STATE / 'shared')
    c.launch_runtime = STATE / 'run'
    return c


def setup():
    assert subprocess.run(['docker', 'inspect', f'{PROJECT}-app'], capture_output=True).returncode, 'fixture exists'
    ROOT.mkdir(mode=0o755)
    for name, health in ((PROJECT, 'true'), (BROKEN, 'false')):
        sh('docker', 'run', '-d', '--name', f'{name}-app', '--network', 'none', '--memory', '16m',
           '--pids-limit', '8', '--label', f'com.docker.compose.project={name}',
           '--label', 'com.docker.compose.service=app', '--restart', 'unless-stopped',
           '--health-cmd', health, '--health-interval', '2s', '--health-retries', '1',
           IMAGE, 'sleep', '3600')
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / UNIT).write_text(
        '[Unit]\nDescription=WRG launcher probe startup\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\n'
        f"ExecStart=/bin/sh -c 'echo run >> {LOG}; docker start {PROJECT}-app >/dev/null'\n\n"
        '[Install]\nWantedBy=default.target\n')
    # Same shape as a scheduled check that Requires= the startup unit.
    (UNIT_DIR / HEALTH).write_text(f'[Unit]\nRequires={UNIT}\nAfter={UNIT}\n\n[Service]\nType=oneshot\n'
                                   'ExecStart=/bin/true\n')
    dropin = UNIT_DIR / f'{UNIT}.d'
    dropin.mkdir(exist_ok=True)
    (dropin / 'wrg-launch.conf').write_text(controller().launch_dropin('compose-' + PROJECT))
    sh('systemctl', '--user', 'daemon-reload')
    print('Fixture ready')


def runs():
    return LOG.read_text().count('run') if LOG.exists() else 0


def running():
    return sh('docker', 'inspect', '--format', '{{.State.Running}}', f'{PROJECT}-app') == 'true'


def user(owner, *args):
    return sh('systemctl', '--user', f'--machine={owner}@', *args, check=False)


def condition_result(owner):
    return user(owner, 'show', UNIT, '--property=ConditionResult', '--value')


def run(owner):
    from wsl_resource_guard import service_control
    from wsl_resource_guard.service_control import ControlError, atomic_json
    STATE.mkdir(mode=0o755)
    try:
        c = controller()
        atomic_json(c.registry, {'version': 1, 'owner': owner, 'origin': 'https://example.test:9443', 'services': []})
        c.register(f'compose:{PROJECT}', 'Probe')
        c.register(f'compose:{BROKEN}', 'Broken')
        saved = {s['id']: s for s in c.load()['services']}
        assert saved['compose-' + PROJECT]['launcher'] == UNIT and 'launcher' not in saved['compose-' + BROKEN]
        identity = sh('docker', 'inspect', '--format', '{{.Id}}', f'{PROJECT}-app')

        # Cold boot with autostart on; the other project's failure is reported, not fatal.
        sh('docker', 'stop', f'{PROJECT}-app')
        user(owner, 'stop', UNIT)
        with patch.object(service_control, 'RESTORE_HEALTH_WAIT_SECONDS', 20):
            try:
                c.restore(wait=True)
                raise AssertionError('Unhealthy project was accepted')
            except ControlError as exc:
                text = str(exc)
        assert f'Probe: {UNIT} 시작 절차 완료' in text and 'Broken: 20초 안에 정상 상태가 되지 않았습니다' in text, text
        assert running() and runs() == 1
        c.act('compose-' + BROKEN, 'disable')

        # Two concurrent recoveries run the startup unit once.
        sh('docker', 'stop', f'{PROJECT}-app')
        user(owner, 'stop', UNIT)
        results = []
        workers = [threading.Thread(target=lambda: results.append(c.restore(wait=True)['message']))
                   for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert len(results) == 2 and running() and runs() == 2, (results, runs())

        # Autostart off survives a "reboot" and a Requires= pull-in.
        c.act('compose-' + PROJECT, 'autostart-off')
        c.restore(wait=True)
        assert not running()
        user(owner, 'stop', UNIT)
        user(owner, 'start', HEALTH)
        assert condition_result(owner) == 'no' and not running() and runs() == 2

        # An explicit start works while autostart stays off.
        print(c.act('compose-' + PROJECT, 'start')['message'])
        for _ in range(30):
            if running() and user(owner, 'is-active', UNIT) == 'active':
                break
            time.sleep(1)
        assert running() and runs() == 3

        # With autostart on, a stop still holds for this boot after the unit becomes inactive.
        c.act('compose-' + PROJECT, 'autostart-on')
        c.act('compose-' + PROJECT, 'stop')
        user(owner, 'stop', UNIT)
        user(owner, 'start', HEALTH)
        assert condition_result(owner) == 'no' and not running() and runs() == 3

        # Recovery re-applies autostart and supersedes this boot's stop.
        c.restore(wait=True)
        assert running() and runs() == 4
        assert sh('docker', 'inspect', '--format', '{{.Id}}', f'{PROJECT}-app') == identity
        print('Launcher integration passed: binding, cold restore, partial failure, single run under '
              'concurrency, autostart-off and stop against Requires= pull-ins, explicit start')
    finally:
        shutil.rmtree(STATE)


def cleanup():
    for name in (PROJECT, BROKEN):
        subprocess.run(['docker', 'rm', '-f', f'{name}-app'], capture_output=True)
    subprocess.run(['systemctl', '--user', 'stop', HEALTH, UNIT], capture_output=True)
    shutil.rmtree(UNIT_DIR / f'{UNIT}.d', ignore_errors=True)
    for name in (UNIT, HEALTH):
        (UNIT_DIR / name).unlink(missing_ok=True)
    sh('systemctl', '--user', 'daemon-reload')
    subprocess.run(['systemctl', '--user', 'reset-failed', UNIT, HEALTH], capture_output=True)
    shutil.rmtree(ROOT, ignore_errors=True)
    print('Fixture removed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('setup', 'run', 'cleanup'))
    parser.add_argument('--owner')
    args = parser.parse_args()
    if args.phase == 'run':
        if os.geteuid() != 0 or not args.owner:
            parser.error('run needs root and --owner')
        run(args.owner)
    elif os.geteuid() == 0:
        parser.error(f'{args.phase} runs as the owner')
    else:
        os.environ.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
        {'setup': setup, 'cleanup': cleanup}[args.phase]()


if __name__ == '__main__':
    main()
