"""Boot recovery and Compose projects bound to an owner startup unit."""
import json
import os
from pathlib import Path
import pwd
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from wsl_resource_guard import service_control
from wsl_resource_guard.service_control import Controller, ControlError, atomic_json

OWNER = pwd.getpwuid(os.getuid()).pw_name
UNIT = 'demo-startup.service'


def container(identifier='a', running=True, policy='unless-stopped', health=None, project='demo'):
    state = {'Status': 'running' if running else 'exited'}
    if health:
        state['Health'] = {'Status': health}
    return {'id': identifier * 64, 'name': f'{project}-{identifier}', 'state': state, 'policy': policy,
            'labels': {'com.docker.compose.project': project}}


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'registry' / 'registry.json'
        self.entry = {'id': 'compose-demo', 'key': 'compose:demo', 'name': 'Demo', 'target': 'demo',
                      'kind': 'compose', 'category': 'app', 'owned': False, 'autostart': True,
                      'launcher': UNIT}
        self.data = {'owner': OWNER, 'origin': 'https://test:9443', 'services': [self.entry]}
        atomic_json(self.path, self.data)
        self.unit = {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'exited', 'Result': 'success',
                     'Type': 'oneshot', 'RemainAfterExit': 'yes', 'ConditionResult': 'yes'}
        self.after_start = {'ActiveState': 'active', 'Result': 'success'}
        self.calls = []
        self.controller = Controller(self.path, self.fake_run, shared=self.root / 'shared')
        self.controller.launch_runtime = self.root / 'run' / 'launch'
        self.controller.linger_dir = self.root / 'linger'
        self.controller.linger_dir.mkdir()
        (self.controller.linger_dir / OWNER).touch()
        self.controller.containers = Mock(return_value=[container()])
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active', 'UnitFileState': 'enabled'})
        autostart, start, stop = self.controller._launch_paths('compose-demo')
        self.conditions = [['ConditionPathExists', True, False, str(autostart)],
                           ['ConditionPathExists', True, False, str(start)],
                           ['ConditionPathExists', False, True, str(stop)]]

    def fake_run(self, *args, timeout=70, check=True):
        self.calls.append((args, timeout))
        if args[:2] == ('systemctl', '--user'):
            verb = args[3]
            if verb == 'show':
                return '\n'.join(f'{k}={v}' for k, v in self.unit.items())
            if verb in ('start', 'restart'):
                self.unit.update(self.after_start)
            return ''
        if args[0] == 'busctl':
            if 'call' in args:
                return json.dumps({'type': 'o', 'data': ['/org/freedesktop/systemd1/unit/demo_2dstartup_2eservice']})
            return json.dumps({'type': 'a(sbbsi)', 'data': [[*c, 0] for c in self.conditions]})
        return ''

    def commands(self):
        return [args for args, _ in self.calls]

    def user_calls(self, verb=None):
        return [args for args in self.commands()
                if args[:2] == ('systemctl', '--user') and args[3] != 'show' and (verb is None or args[3] == verb)]

    def docker_calls(self):
        return [args[1:] for args in self.commands() if args[0] == 'docker']

    def flag(self):
        return self.controller._launch_paths('compose-demo')[0]

    def marker(self, kind):
        return self.controller._launch_paths('compose-demo')[1 if kind == 'start' else 2]

    def candidate(self):
        candidate = {'key': 'compose:demo', 'id': 'compose-demo', 'kind': 'compose', 'target': 'demo',
                     'name': 'demo', 'category': 'app', 'notice': '', 'owned': False, 'autostart': True}
        self.controller._bind_launcher(candidate, OWNER)
        return candidate

    # Discovery and registration
    def test_gated_startup_unit_is_bound_by_project_name(self):
        candidate = self.candidate()
        self.assertEqual(candidate['launcher'], UNIT)
        self.assertNotIn('launcher_error', candidate)
        self.assertIn(('systemctl', '--user', f'--machine={OWNER}@', 'show', UNIT,
                       '--property=LoadState,ActiveState,SubState,Result,Type,RemainAfterExit,ConditionResult'),
                      self.commands())

    def test_project_without_startup_unit_stays_plain_compose(self):
        self.unit = {'LoadState': 'not-found'}
        candidate = self.candidate()
        self.assertNotIn('launcher', candidate)
        self.assertNotIn('launcher_error', candidate)

    def test_ungated_or_widened_startup_unit_is_refused(self):
        for conditions in ([], self.conditions[:2],
                           self.conditions + [['ConditionPathExists', True, False, '/tmp']]):
            with self.subTest(conditions=conditions):
                self.conditions = conditions
                self.assertIn('보장할 수 없습니다', self.candidate()['launcher_error'])

    def test_non_oneshot_startup_unit_is_refused(self):
        self.unit['RemainAfterExit'] = 'no'
        self.assertIn('oneshot', self.candidate()['launcher_error'])

    def test_unreachable_owner_manager_blocks_binding(self):
        self.controller.run = Mock(side_effect=ControlError('bus unavailable'))
        self.assertIn('linger', self.candidate()['launcher_error'])

    def test_registration_refuses_ungated_unit_with_drop_in_hint(self):
        self.data['services'] = []
        atomic_json(self.path, self.data)
        self.conditions = []
        self.controller.discover = Mock(return_value=[self.candidate()])
        with self.assertRaises(ControlError) as caught:
            self.controller.register('compose:demo', 'Demo')
        self.assertIn(f'{UNIT}.d/wrg-launch.conf', str(caught.exception))
        self.assertIn(f'ConditionPathExists=!{self.marker("stop")}', str(caught.exception))
        self.assertEqual(self.controller.load()['services'], [])
        self.assertFalse(self.flag().exists())

    def test_registration_keeps_state_and_mirrors_autostart_flag(self):
        self.data['services'] = []
        atomic_json(self.path, self.data)
        self.controller.discover = Mock(return_value=[self.candidate()])
        self.calls.clear()
        result = self.controller.register('compose:demo', 'Demo')
        self.assertIn(UNIT, result['message'])
        self.assertEqual(self.controller.load()['services'][0]['launcher'], UNIT)
        self.assertTrue(self.flag().exists())
        self.assertEqual(oct(self.flag().parent.stat().st_mode & 0o777), '0o755')
        self.assertEqual(self.user_calls(), [])
        self.assertEqual(self.docker_calls(), [])

    def test_drop_in_matches_the_conditions_checked(self):
        lines = self.controller.launch_dropin('compose-demo').splitlines()
        self.assertEqual(lines[0], '[Unit]')
        parsed = []
        for line in lines[1:]:
            value = line.split('=', 1)[1]
            parsed.append(['ConditionPathExists', value.startswith('|'), value.startswith('!'), value.lstrip('|!')])
        self.assertEqual(parsed, self.conditions)

    # Actions
    def test_autostart_toggle_moves_flag_and_restart_policy_only(self):
        self.controller.act('compose-demo', 'autostart-off')
        self.assertFalse(self.flag().exists())
        self.assertEqual(self.docker_calls(), [('update', '--restart=no', 'a' * 64)])
        self.controller.act('compose-demo', 'autostart-on')
        self.assertTrue(self.flag().exists())
        self.assertEqual(self.user_calls(), [])

    def test_start_uses_startup_unit_never_docker_start(self):
        self.controller.containers.return_value = [container(running=False)]
        result = self.controller.act('compose-demo', 'start')
        self.assertIn('요청', result['message'])
        self.assertTrue(self.marker('start').exists())
        self.assertFalse(self.marker('stop').exists())
        self.assertEqual(self.user_calls(), [('systemctl', '--user', f'--machine={OWNER}@', 'restart', '--no-block', UNIT)])
        self.assertEqual(self.docker_calls(), [])
        self.assertIn(('systemctl', 'start', f'user@{os.getuid()}.service'), self.commands())

    def test_start_without_containers_is_left_to_the_startup_unit(self):
        self.controller.containers.return_value = []
        self.controller.act('compose-demo', 'start')
        self.assertEqual(len(self.user_calls('restart')), 1)

    def test_start_is_a_no_op_while_running_or_already_starting(self):
        self.controller.act('compose-demo', 'start')
        self.assertEqual(self.user_calls(), [])
        self.unit['ActiveState'] = 'activating'
        self.controller.containers.return_value = [container(running=False)]
        self.assertIn('진행 중', self.controller.act('compose-demo', 'start')['message'])
        self.assertEqual(self.user_calls(), [])

    def test_restart_stops_containers_then_reruns_procedure(self):
        self.controller.act('compose-demo', 'restart')
        self.assertEqual(self.docker_calls(), [('stop', 'a' * 64)])
        self.assertEqual(len(self.user_calls('restart')), 1)

    def test_stop_blocks_pull_ins_and_keeps_completed_unit_state(self):
        self.controller._permit_launch(self.entry, True)
        self.controller.act('compose-demo', 'stop')
        self.assertTrue(self.marker('stop').exists())
        self.assertFalse(self.marker('start').exists())
        self.assertEqual(self.docker_calls(), [('stop', 'a' * 64)])
        # An active (exited) unit is left alone: a Requires= pull-in is then a no-op.
        self.assertEqual(self.user_calls(), [])

    def test_stop_cancels_an_in_progress_startup(self):
        self.unit['ActiveState'] = 'activating'
        self.controller.act('compose-demo', 'stop')
        self.assertEqual(self.user_calls(), [('systemctl', '--user', f'--machine={OWNER}@', 'stop', UNIT)])
        self.assertEqual(self.docker_calls(), [('stop', 'a' * 64)])

    def test_remove_clears_autostart_and_explains_drop_in(self):
        self.controller._sync_launch_autostart(self.entry)
        result = self.controller.act('compose-demo', 'remove', confirmed=True)
        self.assertIn('drop-in', result['message'])
        self.assertFalse(self.flag().exists())
        self.assertTrue(self.marker('stop').exists())
        self.assertEqual(self.controller.load()['services'], [])

    def test_start_reports_busy_startup_lock(self):
        self.controller.containers.return_value = [container(running=False)]
        with self.controller._launcher_lock(self.entry, wait=0):
            with self.assertRaisesRegex(ControlError, '진행 중'):
                self.controller.act('compose-demo', 'start')
        self.assertEqual(self.user_calls(), [])

    def test_snapshot_shows_unit_state_and_flag_drift(self):
        row = self.controller.snapshot()['services'][0]
        self.assertIn('시작 절차 active', row['detail'])
        self.assertTrue(row['drift'])  # Autostart is on but the flag is missing.
        self.controller._sync_launch_autostart(self.entry)
        self.controller._invalidate_snapshot()
        row = self.controller.snapshot()['services'][0]
        self.assertFalse(row['drift'])
        self.assertEqual(row['error'], '')

    def test_snapshot_reports_a_removed_gate(self):
        self.conditions = []
        self.assertIn('보장할 수 없습니다', self.controller.snapshot()['services'][0]['error'])

    # Boot recovery
    def test_autostart_off_without_containers_still_clears_flag(self):
        self.entry['autostart'] = False
        atomic_json(self.path, self.data)
        self.flag().parent.mkdir(parents=True)
        self.flag().touch()
        self.controller.containers.return_value = []
        self.controller.restore(wait=True)
        self.assertFalse(self.flag().exists())
        self.assertEqual(self.user_calls(), [])

    def test_boot_restore_runs_unit_once_and_waits(self):
        self.unit.update(ActiveState='inactive', ConditionResult='no')
        self.controller._permit_launch(self.entry, False)  # A stale stop marker from this boot.
        result = self.controller.restore(wait=True)
        self.assertTrue(self.flag().exists())
        self.assertFalse(self.marker('stop').exists())
        self.assertEqual(self.docker_calls(), [('update', '--restart=unless-stopped', 'a' * 64)])
        restarts = [(args, timeout) for args, timeout in self.calls if args[3:4] == ('restart',)]
        self.assertEqual(restarts, [(('systemctl', '--user', f'--machine={OWNER}@', 'restart', UNIT), 660)])
        self.assertIn(f'Demo: {UNIT} 시작 절차 완료', result['message'])

    def test_restore_joins_a_running_start_job(self):
        self.unit['ActiveState'] = 'activating'
        self.controller.restore(wait=True)
        self.assertEqual(self.user_calls(), [('systemctl', '--user', f'--machine={OWNER}@', 'start', UNIT)])

    def test_restore_skips_completed_unit_with_running_containers(self):
        result = self.controller.restore(wait=True)
        self.assertEqual(self.user_calls(), [])
        self.assertIn('다시 실행하지 않았습니다', result['message'])

    def test_restore_reports_unit_failure_and_condition_skip(self):
        for after, reason in (({'ActiveState': 'failed', 'Result': 'exit-code'}, 'exit-code'),
                              ({'ActiveState': 'inactive', 'ConditionResult': 'no'}, '실행 조건 미충족')):
            with self.subTest(reason=reason):
                self.unit.update(ActiveState='inactive', Result='success', ConditionResult='yes')
                self.after_start = after
                with self.assertRaisesRegex(ControlError, reason):
                    self.controller.restore(wait=True)

    def test_restore_with_autostart_off_never_invokes_unit(self):
        self.entry['autostart'] = False
        atomic_json(self.path, self.data)
        self.flag().parent.mkdir(parents=True)
        self.flag().touch()
        self.controller._permit_launch(self.entry, True)
        result = self.controller.restore(wait=True)
        self.assertFalse(self.flag().exists())
        self.assertFalse(self.marker('start').exists())
        self.assertEqual(self.docker_calls(), [('update', '--restart=no', 'a' * 64), ('stop', 'a' * 64)])
        self.assertEqual(self.user_calls(), [])
        self.assertIn('자동 실행 꺼짐', result['message'])

    def test_restore_requires_owner_linger(self):
        (self.controller.linger_dir / OWNER).unlink()
        self.unit['ActiveState'] = 'inactive'
        with self.assertRaisesRegex(ControlError, 'linger'):
            self.controller.restore(wait=True)
        self.assertEqual(self.user_calls(), [])

    def test_concurrent_restore_waits_then_finds_it_done(self):
        self.unit['ActiveState'] = 'inactive'
        holding, release = threading.Event(), threading.Event()

        def other_restore():
            with self.controller._launcher_lock(self.entry, wait=0):
                holding.set()
                release.wait(5)
                self.unit['ActiveState'] = 'active'  # The other run finished the start.

        worker = threading.Thread(target=other_restore)
        worker.start()
        holding.wait(2)
        threading.Timer(0.4, release.set).start()
        result = self.controller.restore(wait=True)
        worker.join()
        self.assertEqual(self.user_calls(), [])
        self.assertIn('다시 실행하지 않았습니다', result['message'])

    def test_socket_restore_does_not_wait_for_a_busy_startup(self):
        self.unit['ActiveState'] = 'inactive'
        with self.controller._launcher_lock(self.entry, wait=0):
            with self.assertRaisesRegex(ControlError, '진행 중'):
                self.controller.restore()
        self.assertEqual(self.user_calls(), [])


class RestoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'registry.json'
        self.entries = [{'id': f'compose-{name}', 'key': f'compose:{name}', 'name': name.title(), 'target': name,
                         'kind': 'compose', 'category': 'app', 'owned': False, 'autostart': True}
                        for name in ('alpha', 'beta')]
        atomic_json(self.path, {'owner': OWNER, 'origin': 'https://test:9443', 'services': self.entries})
        self.run = Mock(return_value='')
        self.controller = Controller(self.path, self.run, shared=Path(temp.name) / 'shared')
        self.groups = {'alpha': [container('a', project='alpha')], 'beta': [container('b', project='beta')]}
        self.controller.containers = Mock(side_effect=lambda target=None: self.groups[target])
        self.docker = {'ActiveState': 'active', 'UnitFileState': 'enabled'}
        self.controller.unit_state = Mock(side_effect=lambda target: self.docker)

    def commands(self):
        return [call.args for call in self.run.call_args_list]

    def test_enabled_docker_that_is_not_ready_is_started_first(self):
        self.docker = {'ActiveState': 'activating', 'UnitFileState': 'enabled'}

        def run(*args, **kwargs):
            if args[:2] == ('systemctl', 'start'):
                self.docker = {'ActiveState': 'active', 'UnitFileState': 'enabled'}
            return ''
        self.run.side_effect = run
        result = self.controller.restore(wait=True)
        self.assertEqual(self.run.call_args_list[0].args, ('systemctl', 'start', 'docker.service'))
        self.assertEqual(self.run.call_args_list[0].kwargs, {'timeout': service_control.DOCKER_START_TIMEOUT})
        self.assertIn('Docker: 시작함', result['message'])
        self.assertIn('Alpha: 실행 중 유지', result['message'])

    def test_docker_start_failure_names_the_unrecovered_projects(self):
        self.docker = {'ActiveState': 'failed', 'UnitFileState': 'enabled'}
        self.run.side_effect = ControlError('Job for docker.service failed')
        with self.assertRaisesRegex(ControlError, 'Alpha, Beta'):
            self.controller.restore(wait=True)
        self.controller.containers.assert_not_called()

    def test_disabled_docker_is_reported_as_skipped(self):
        self.docker = {'ActiveState': 'inactive', 'UnitFileState': 'disabled'}
        result = self.controller.restore(wait=True)
        self.assertIn('건너뛰었습니다', result['message'])
        self.run.assert_not_called()

    def test_one_failing_project_does_not_stop_the_others(self):
        def containers(target=None):
            if target == 'alpha':
                raise ControlError('inspect failed')
            return [container('b', running=False, project='beta')] if not started else self.groups['beta']
        started = []
        self.controller.containers.side_effect = containers
        self.run.side_effect = lambda *args, **kwargs: started.append(args) or ''
        with self.assertRaises(ControlError) as caught:
            self.controller.restore(wait=True)
        text = str(caught.exception)
        self.assertIn('Alpha: inspect failed', text)
        self.assertIn('Beta: 기존 컨테이너 1개 시작', text)
        self.assertIn(('docker', 'start', 'b' * 64), self.commands())

    def test_autostart_project_without_containers_is_a_failure(self):
        self.groups['alpha'] = []
        with self.assertRaisesRegex(ControlError, 'Alpha: 기존 컨테이너가 없습니다'):
            self.controller.restore()
        self.assertIn(('docker', 'update', '--restart=unless-stopped', 'b' * 64), self.commands())

    def test_disabled_project_without_containers_is_not_a_failure(self):
        self.entries[0]['autostart'] = False
        atomic_json(self.path, {'owner': OWNER, 'origin': 'https://test:9443', 'services': self.entries})
        self.groups['alpha'] = []
        self.controller.restore()

    def test_health_wait_reports_unhealthy_project_by_deadline(self):
        self.groups['beta'] = [container('b', health='starting', project='beta')]
        with (patch.object(service_control, 'RESTORE_HEALTH_WAIT_SECONDS', 0.2),
              patch.object(service_control.time, 'sleep', lambda _: None)):
            with self.assertRaises(ControlError) as caught:
                self.controller.restore(wait=True)
        self.assertIn('Beta: 0.2초 안에 정상 상태가 되지 않았습니다 (1/1 실행, 미준비: beta-b)', str(caught.exception))
        self.assertNotIn('Alpha: 0.2초', str(caught.exception))

    def test_health_wait_accepts_late_healthy_container(self):
        states = iter(['starting', 'healthy'])
        self.controller.containers.side_effect = lambda target=None: (
            [container('b', health=next(states, 'healthy'), project='beta')] if target == 'beta' else self.groups[target])
        with patch.object(service_control.time, 'sleep', lambda _: None):
            self.controller.restore(wait=True)

    def test_socket_restore_does_not_wait_for_health(self):
        self.groups['beta'] = [container('b', health='unhealthy', project='beta')]
        self.controller.restore()


if __name__ == '__main__':
    unittest.main()
