"""Disposable Docker lifecycle test against the installed controller.

Uses a preinstalled UBI image, no network, mounts, published ports, or app data.
"""
import json
from pathlib import Path
import subprocess
import tempfile

from wsl_resource_guard.service_control import Controller, atomic_json
from wsl_resource_guard.services import request_control

NAME = 'wrg-management-smoke'
PROJECT = 'wrgsmoke'
IDENTIFIER = 'compose-' + PROJECT


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def state():
    return json.loads(docker('inspect', '--format', '{{json .State}}', NAME))


def main():
    exists = subprocess.run(['docker', 'inspect', NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert exists.returncode != 0, 'Fixture name is already in use'
    identifier = docker('run', '-d', '--name', NAME, '--network', 'none', '--memory', '16m', '--pids-limit', '8',
                        '--label', 'com.docker.compose.project=' + PROJECT,
                        '--label', 'com.docker.compose.service=app', '--restart', 'unless-stopped',
                        '--entrypoint', '/usr/bin/sleep', 'registry.access.redhat.com/ubi8/ubi:8.10', '3600')
    try:
        before = state()['StartedAt']
        request_control({'op':'register','key':'compose:'+PROJECT})
        request_control({'op':'action','id':IDENTIFIER,'action':'autostart-off'})
        assert state()['Running'] and state()['StartedAt'] == before
        assert docker('inspect','--format','{{.HostConfig.RestartPolicy.Name}}',NAME) == 'no'
        request_control({'op':'action','id':IDENTIFIER,'action':'disable'})
        assert not state()['Running']
        snapshot = request_control({'op':'list'})
        row = next(s for s in snapshot['services'] if s['id'] == IDENTIFIER)
        assert row['autostart'] is False and row['state'] == 'inactive'
        # Simulate recovery using an isolated registry containing only this fixture.
        docker('start', NAME)
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory)/'registry.json'
            atomic_json(registry,{'services':[row]})
            Controller(registry).restore()
            assert not state()['Running'], 'Recovery must enforce disabled state'
            row['autostart'] = True
            atomic_json(registry,{'services':[row]})
            Controller(registry).restore()
            assert state()['Running']
        request_control({'op':'action','id':IDENTIFIER,'action':'enable'})
        assert docker('inspect','--format','{{.HostConfig.RestartPolicy.Name}}',NAME) == 'unless-stopped'
        request_control({'op':'action','id':IDENTIFIER,'action':'restart'})
        assert state()['Running']
        request_control({'op':'action','id':IDENTIFIER,'action':'remove','confirmed':True})
        assert not state()['Running']
        assert docker('inspect','--format','{{.Id}}',NAME) == identifier
        assert docker('inspect','--format','{{json .Mounts}}',NAME) == '[]'
        assert not any(s['id']==IDENTIFIER for s in request_control({'op':'list'})['services'])
        print('Docker integration passed: adoption, toggles, lifecycle, isolated recovery, removal preserves container/data')
    finally:
        if any(s['id']==IDENTIFIER for s in request_control({'op':'list'})['services']):
            request_control({'op':'action','id':IDENTIFIER,'action':'remove','confirmed':True})
        docker('rm','-f',NAME)


if __name__ == '__main__':
    main()
