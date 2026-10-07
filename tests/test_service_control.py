from contextlib import redirect_stderr
from datetime import datetime
import io
import json
import os
from pathlib import Path
import pwd
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from wsl_resource_guard import service_control
from wsl_resource_guard.service_control import Controller, ControlError, atomic_json


def container(identifier='a', running=True, policy='unless-stopped'):
    return {'id': identifier * 64, 'name': 'demo-app', 'state': {'Status': 'running' if running else 'exited'},
            'policy': policy, 'labels': {'com.docker.compose.project': 'demo'}}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'registry.json'
        self.entry = {'id': 'compose-demo', 'key': 'compose:demo', 'name': 'Demo', 'target': 'demo',
                      'kind': 'compose', 'category': 'app', 'owned': False, 'autostart': True}
        self.data = {'owner': pwd.getpwuid(os.getuid()).pw_name, 'origin': 'https://test:9443', 'services': [self.entry]}
        atomic_json(self.path, self.data)
        self.run = Mock(return_value='')
        self.controller = Controller(self.path, self.run)
        self.controller.containers = Mock(return_value=[container()])
        # Exercise reader behavior in-process using a temporary owner home;
        # production always uses the credential-dropping subprocess seam.
        from wsl_resource_guard.owner_worker import OwnerData, dispatch
        self.owner_home = Path(self.temp.name) / 'home'
        self.owner_home.mkdir()
        self.controller._owner_read = lambda operation, args=None: dispatch(
            operation, args or {}, OwnerData(self.owner_home))

    def commands(self):
        return [call.args for call in self.run.call_args_list]

    def test_disable_persists_and_clears_docker_restart_then_stops(self):
        self.controller.act('compose-demo', 'disable')
        self.assertFalse(self.controller.load()['services'][0]['autostart'])
        self.assertEqual([c[:3] for c in self.commands()], [('docker', 'update', '--restart=no'), ('docker', 'stop', 'a'*64)])

    def test_autostart_off_preserves_current_running_state(self):
        self.controller.act('compose-demo', 'autostart-off')
        self.assertFalse(self.controller.load()['services'][0]['autostart'])
        self.assertEqual(len(self.commands()), 1)
        self.assertEqual(self.commands()[0][:3], ('docker', 'update', '--restart=no'))

    def test_start_disabled_project_keeps_autostart_disabled(self):
        self.data['services'][0]['autostart'] = False
        atomic_json(self.path, self.data)
        self.controller.act('compose-demo', 'start')
        self.assertFalse(self.controller.load()['services'][0]['autostart'])
        self.assertEqual(self.commands(), [('docker', 'start', 'a'*64)])

    def test_partial_disable_keeps_intent_and_registration(self):
        self.run.side_effect = ControlError('update failed')
        with self.assertRaises(ControlError):
            self.controller.act('compose-demo', 'remove', confirmed=True)
        self.assertEqual(len(self.controller.load()['services']), 1)
        self.assertFalse(self.controller.load()['services'][0]['autostart'])
        self.assertFalse(any('stop' in c for c in self.commands()))

    def test_failed_stop_prevents_unregister(self):
        self.run.side_effect = ['', ControlError('stop failed')]
        with self.assertRaises(ControlError):
            self.controller.act('compose-demo', 'remove', confirmed=True)
        self.assertEqual(len(self.controller.load()['services']), 1)

    def test_snapshot_caches_unit_queries_briefly(self):
        self.data['services'].append(dict(self.entry, id='systemd-x', key='systemd:x.service',
                                          kind='systemd', target='x.service'))
        atomic_json(self.path, self.data)
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        self.controller.snapshot()
        self.controller.snapshot()
        self.assertEqual(self.controller.unit_state.call_count, 1)
        self.controller._snapshot_at = 0  # expired
        self.controller.snapshot()
        self.assertEqual(self.controller.unit_state.call_count, 2)

    def test_systemd_row_carries_memory_limits(self):
        self.data['services'] = [dict(self.entry, id='devin-web', kind='systemd',
                                      target='devin-web.service')]
        atomic_json(self.path, self.data)
        self.controller.unit_state = Mock(return_value={
            'ActiveState': 'active', 'MemoryCurrent': str(6 * 2**30),
            'MemoryHigh': str(6 * 2**30), 'MemoryMax': 'infinity'})
        row = self.controller.snapshot()['services'][0]
        self.assertEqual(row['memory_high'], 6 * 2**30)
        self.assertIsNone(row['memory_max'])

    def test_health_probe_results_attach_to_matching_url_only(self):
        self.data['services'] = [dict(self.entry, url='https://test:8080/')]
        atomic_json(self.path, self.data)
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        self.controller.check_health(probe=lambda url: {'ok': False, 'status': 502,
                                                        'error': 'HTTP 502', 'checked_at': 1})
        row = self.controller.snapshot()['services'][0]
        self.assertEqual(row['health']['status'], 502)
        self.assertNotIn('url', row['health'])
        # A re-registered URL must not show the old URL's verdict.
        self.data['services'][0]['url'] = 'https://test:9090/'
        atomic_json(self.path, self.data)
        self.controller._invalidate_snapshot()
        self.assertNotIn('health', self.controller.snapshot()['services'][0])

    def test_health_probe_never_leaves_the_dashboard_host(self):
        self.data['services'] = [dict(self.entry, url='http://169.254.169.254/latest'),
                                 dict(self.entry, id='own', url='https://test:7100/')]
        atomic_json(self.path, self.data)
        probed = []
        self.controller.check_health(probe=lambda url: probed.append(url) or {'ok': True})
        self.assertEqual(probed, ['https://test:7100/'])

    def test_probe_url_classifies_answers(self):
        import urllib.error

        class Response:
            def __init__(self, status):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        self.assertTrue(service_control.probe_url('https://x/', lambda *a, **k: Response(200))['ok'])

        def forbidden(*a, **k):
            raise urllib.error.HTTPError('https://x/', 403, 'Forbidden', {}, None)
        self.assertTrue(service_control.probe_url('https://x/', forbidden)['ok'])

        def broken(*a, **k):
            raise urllib.error.HTTPError('https://x/', 503, 'Down', {}, None)
        self.assertFalse(service_control.probe_url('https://x/', broken)['ok'])

        def refused(*a, **k):
            raise urllib.error.URLError(ConnectionRefusedError('refused'))
        result = service_control.probe_url('https://x/', refused)
        self.assertFalse(result['ok'])
        self.assertIn('ConnectionRefusedError', result['error'])

    def test_docker_df_parses_rows_and_skips_stopped_docker(self):
        self.controller.unit_state = Mock(return_value={'ActiveState': 'inactive'})
        self.assertFalse(self.controller.docker_df()['available'])
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        self.run.return_value = ('{"Type":"Images","TotalCount":"5","Active":"2",'
                                 '"Size":"3.1GB","Reclaimable":"1.2GB (38%)"}\nnot json')
        result = self.controller.docker_df()
        self.assertEqual(result['rows'][0]['Reclaimable'], '1.2GB (38%)')
        self.assertEqual(self.commands()[-1][:3], ('docker', 'system', 'df'))

    def test_snooze_writes_a_shared_file_and_rejects_odd_durations(self):
        self.controller.shared = Path(self.temp.name) / 'shared'
        result = self.controller.snooze(60, 'owner@example.com')
        data = json.loads((self.controller.shared / 'alert-snooze.json').read_text())
        self.assertAlmostEqual(data['until'], time.time() + 3600, delta=5)
        self.assertEqual(oct((self.controller.shared / 'alert-snooze.json').stat().st_mode & 0o777), '0o640')
        self.assertGreater(self.controller.snooze_until(), 0)
        self.assertIn('1시간', result['message'])
        for bad in (7, -60, True, '60', 10**9):
            with self.assertRaises(ControlError):
                self.controller.snooze(bad)
        self.controller.snooze(0)
        self.assertEqual(self.controller.snooze_until(), 0)

    def test_web_can_snooze_through_dispatch_and_it_is_audited(self):
        self.controller.shared = Path(self.temp.name) / 'shared'
        self.controller.dispatch({'op': 'snooze', 'minutes': 240, 'actor': 'web'}, uid=999, web_uid=999)
        record = json.loads((self.path.parent / 'audit.jsonl').read_text().splitlines()[-1])
        self.assertEqual((record['op'], record['detail']), ('snooze', {'minutes': 240}))

    def test_push_subscriptions_are_validated_and_capped(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest('python3-cryptography is not installed in this interpreter')
        from wsl_resource_guard.config import Settings
        self.controller.shared = Path(self.temp.name) / 'shared'
        state = Path(self.temp.name) / 'state'
        sub = {'endpoint': 'https://fcm.googleapis.com/fcm/send/abc',
               'keys': {'p256dh': 'BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4',
                        'auth': 'BTBZMqHH6r4Tts7J_aSIgg'}}
        with patch('wsl_resource_guard.config.Settings.load', return_value=Settings(state_dir=str(state))):
            key = self.controller.push_key()
            self.assertTrue(key['available'])
            self.assertEqual(self.controller.push_key()['public_key'], key['public_key'])  # stable
            vapid = json.loads((self.controller.shared / 'push-vapid.json').read_text())
            self.assertEqual(vapid['subject'], 'https://test:9443')
            with self.assertRaises(ControlError):
                self.controller.push_subscribe({**sub, 'endpoint': 'https://127.0.0.1/x'})
            self.controller.push_subscribe(sub, 'phone')
            self.controller.push_subscribe(sub, 'phone again')  # same endpoint: replaced
            self.assertEqual(self.controller.push_key()['subscriptions'], 1)
            # Endpoints the daemon saw expire no longer count.
            state.mkdir()
            (state / 'push-expired.json').write_text(json.dumps([sub['endpoint']]))
            self.assertEqual(self.controller.push_key()['subscriptions'], 0)
            with self.assertRaises(ControlError):
                self.controller.push_test()

    def test_config_change_requests_are_validated_and_queued(self):
        from wsl_resource_guard.config import Settings
        self.controller.shared = Path(self.temp.name) / 'shared'
        with patch('wsl_resource_guard.config.Settings.load', return_value=Settings()):
            result = self.controller.request_config_change('warning_available_gib', 5, 'web')
            for key, value in (('gmail_enabled', False), ('alert_quiet_hours', '23-08'),
                               ('warning_available_gib', 1.0), ('retention_days', 9999),
                               ('nope', 1)):
                with self.assertRaises(ControlError):
                    self.controller.request_config_change(key, value)
        request = json.loads((self.controller.shared / 'config-request.json').read_text())
        self.assertEqual((request['key'], request['value']), ('warning_available_gib', 5.0))
        self.assertEqual(request['id'], result['id'])

    def test_bulk_kill_audit_names_requested_and_signalled_pids(self):
        with patch.object(self.controller, 'kill_stale',
                          return_value={'message': 'ok', 'killed': [111], 'skipped': [222]}):
            self.controller.dispatch({'op': 'kill-stale', 'pids': [111, 222], 'confirmed': True},
                                     uid=999, web_uid=999)
        record = json.loads((self.path.parent / 'audit.jsonl').read_text().splitlines()[-1])
        self.assertEqual(record['detail'], {'pids': [111, 222], 'killed': [111]})

    def test_kill_stale_signals_only_still_stale_requested_sessions(self):
        with self.assertRaises(ControlError):
            self.controller.kill_stale([])
        with self.assertRaises(ControlError):
            self.controller.kill_stale(['12'])
        stale = Mock(root_pid=111)
        fresh = Mock(root_pid=222)
        snapshot = Mock(sessions=[stale, fresh], processes={})
        with (patch('wsl_resource_guard.processes.build_snapshot', return_value=snapshot),
              patch('wsl_resource_guard.processes.is_stale_session', side_effect=lambda s, h: s is stale),
              patch('wsl_resource_guard.processes.kill_block_reason', return_value=''),
              patch.object(self.controller, 'kill_tree', return_value={'message': 'ok'}) as kill):
            result = self.controller.kill_stale([111, 222, 333])
        kill.assert_called_once_with(111)
        self.assertEqual(result['killed'], [111])
        self.assertEqual(result['skipped'], [222, 333])

    def test_summary_counts_problems_and_disk_alerts(self):
        from wsl_resource_guard.config import Settings
        state_dir = Path(self.temp.name) / 'state'
        state_dir.mkdir()
        (state_dir / 'state.json').write_text(json.dumps({
            'severity': 'critical', 'updated_at': 5, 'reasons': ['x'],
            'condition_since': {'critical.disk.E:': 1, 'warning.available_ram': 1}}))
        self.controller.shared = Path(self.temp.name) / 'shared'
        self.controller.containers.return_value = [container('a', False)]
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        with patch('wsl_resource_guard.config.Settings.load',
                   return_value=Settings(state_dir=str(state_dir))):
            summary = self.controller.summary()
        self.assertEqual(summary['disk_alerts'], ['E:'])
        self.assertTrue(summary['disk_critical'])
        self.assertEqual(summary['service_problems'], 1)  # autostart on, not running
        self.assertEqual(summary['snooze_until'], 0)

    def test_stale_health_on_stopped_containers_is_not_degraded(self):
        stopped = [container('a', False), container('b', False)]
        for c in stopped:
            c['state']['Health'] = {'Status': 'unhealthy'}
        self.controller.containers.return_value = stopped
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        self.assertEqual(self.controller.snapshot()['services'][0]['state'], 'inactive')
        running = container('a', True)
        running['state']['Health'] = {'Status': 'unhealthy'}
        self.controller.containers.return_value = [running]
        self.controller._invalidate_snapshot()
        self.assertEqual(self.controller.snapshot()['services'][0]['state'], 'degraded')

    def test_active_oneshot_detail_does_not_imply_the_app_exited(self):
        self.data['services'] = [dict(self.entry, id='devin-web', kind='systemd',
                                      target='devin-web.service')]
        atomic_json(self.path, self.data)
        self.controller.unit_state = Mock(return_value={
            'ActiveState': 'active', 'SubState': 'exited', 'Type': 'oneshot',
            'RemainAfterExit': 'yes', 'LoadState': 'loaded', 'UnitFileState': 'enabled'})
        row = self.controller.snapshot()['services'][0]
        self.assertEqual(row['state'], 'active')
        self.assertEqual(row['detail'], '시작 완료 · 활성 상태 유지')
        self.assertEqual(row['systemd_substate'], 'exited')

    def test_oneshot_label_does_not_hide_other_unit_states(self):
        base = {'ActiveState': 'active', 'SubState': 'exited',
                'Type': 'oneshot', 'RemainAfterExit': 'yes'}
        for changes in ({'ActiveState': 'inactive', 'SubState': 'dead'},
                        {'ActiveState': 'failed', 'SubState': 'failed'},
                        {'SubState': 'running', 'Type': 'simple'},
                        {'Type': 'simple'}, {'RemainAfterExit': 'no'},
                        {'Type': ''}):
            with self.subTest(changes=changes):
                state = dict(base, **changes)
                self.assertEqual(service_control.unit_detail(state), state['SubState'])
        self.assertEqual(service_control.unit_detail({}), '')

    def test_mutations_invalidate_the_snapshot_cache(self):
        self.data['services'].append(dict(self.entry, id='systemd-x', key='systemd:x.service',
                                          kind='systemd', target='x.service'))
        atomic_json(self.path, self.data)
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        self.controller.snapshot()
        self.controller.act('compose-demo', 'restart')
        self.controller.snapshot()
        self.assertEqual(self.controller.unit_state.call_count, 2)

    def test_remove_preserves_containers_and_volumes(self):
        self.controller.act('compose-demo', 'remove', confirmed=True)
        self.assertEqual(self.controller.load()['services'], [])
        self.assertTrue(all(c[1] in ('update', 'stop') for c in self.commands()))

    def test_remove_requires_confirmation(self):
        with self.assertRaises(ControlError):
            self.controller.act('compose-demo', 'remove')
        self.run.assert_not_called()

    def test_missing_containers_never_trigger_recreation(self):
        self.controller.containers.return_value = []
        with self.assertRaises(ControlError):
            self.controller.act('compose-demo', 'enable')
        self.run.assert_not_called()

    def test_restore_does_not_restart_running_apps(self):
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        self.controller.restore()
        self.assertEqual([c[1] for c in self.commands()], ['update'])

    def test_restore_stops_disabled_and_preserves_setting(self):
        self.data['services'][0]['autostart'] = False
        atomic_json(self.path, self.data)
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        self.controller.restore()
        self.assertEqual([c[1] for c in self.commands()], ['update', 'stop'])
        self.assertFalse(self.controller.load()['services'][0]['autostart'])

    def test_restore_respects_disabled_docker(self):
        self.controller.unit_state = Mock(return_value={'ActiveState': 'inactive'})
        self.controller.restore()
        self.run.assert_not_called()

    def test_restore_starts_existing_stopped_enabled_containers_only(self):
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        self.controller.containers.return_value = [container('a', True), container('b', False)]
        self.controller.restore()
        self.assertEqual(self.commands()[-1], ('docker', 'start', 'b'*64))

    def test_unknown_action_or_service_cannot_execute(self):
        for identifier, action in [('compose-demo', 'rm -rf /'), ('$(touch /tmp/invalid)', 'start')]:
            with self.assertRaises(ControlError):
                self.controller.act(identifier, action)
        self.run.assert_not_called()

    def test_register_only_adopts_discovered_candidate(self):
        self.controller.discover = Mock(return_value=[])
        with self.assertRaises(ControlError):
            self.controller.register('systemd:../../tmp/evil.service')
        self.run.assert_not_called()

    def test_registration_preserves_existing_state(self):
        candidate = dict(self.entry, id='compose-another', key='compose:another', target='another', autostart=False)
        self.controller.discover = Mock(return_value=[candidate])
        self.controller.register('compose:another')
        self.run.assert_not_called()
        self.assertFalse(self.controller.load()['services'][1]['autostart'])

    def test_concurrent_mutation_rejected(self):
        with self.controller.mutation():
            with self.assertRaisesRegex(ControlError, '다른 작업'):
                self.controller.act('compose-demo', 'stop')
        self.run.assert_not_called()

    def test_restore_waits_for_a_running_operation(self):
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        holding = threading.Event()

        def hold_lock():
            with self.controller.mutation():
                holding.set()
                time.sleep(0.5)

        worker = threading.Thread(target=hold_lock)
        worker.start()
        holding.wait(2)
        with patch.object(service_control, 'RESTORE_LOCK_WAIT_SECONDS', 5):
            result = self.controller.restore()
        worker.join()
        self.assertIn('적용', result['message'])

    def test_restore_gives_up_after_the_wait(self):
        with self.controller.mutation():
            with patch.object(service_control, 'RESTORE_LOCK_WAIT_SECONDS', 0.3):
                with self.assertRaisesRegex(ControlError, '다른 작업'):
                    self.controller.restore()

    def test_restore_command_reports_failure_without_traceback(self):
        err = io.StringIO()
        with (patch.object(service_control.Controller, 'restore', side_effect=ControlError('FamilyCare: port busy')),
              patch('sys.argv', ['service_control', 'restore']), redirect_stderr(err)):
            self.assertEqual(service_control.main(), 1)
        self.assertIn('복구 실패: FamilyCare: port busy', err.getvalue())

    def test_web_cannot_disconnect_tailscale_even_if_source_forged(self):
        self.data['services'] = [dict(self.entry, id='tailscaled', target='tailscaled.service', kind='systemd',
                                     category='foundation', notice='connection interrupted')]
        atomic_json(self.path, self.data)
        with self.assertRaisesRegex(ControlError, '터미널'):
            self.controller.dispatch({'op': 'action', 'id': 'tailscaled', 'action': 'stop',
                                      'source': 'cli', 'confirmed': True}, uid=999, web_uid=999)
        self.run.assert_not_called()

    def test_web_cannot_disable_tailscale_autostart(self):
        self.data['services'] = [dict(self.entry, id='tailscaled', target='tailscaled.service', kind='systemd',
                                     category='foundation', notice='connection interrupted')]
        atomic_json(self.path, self.data)
        for action in ('autostart-off', 'disable', 'stop', 'restart', 'remove'):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ControlError, '터미널'):
                    self.controller.act('tailscaled', action, confirmed=True, source='web')
        self.run.assert_not_called()

    def test_terminal_can_still_change_tailscale_autostart(self):
        self.data['services'] = [dict(self.entry, id='tailscaled', target='tailscaled.service', kind='systemd',
                                     category='foundation', notice='connection interrupted')]
        atomic_json(self.path, self.data)
        self.controller.act('tailscaled', 'autostart-off', confirmed=True, source='cli')
        self.assertFalse(self.controller.load()['services'][0]['autostart'])
        self.assertEqual(self.commands(), [('systemctl', 'disable', 'tailscaled.service')])

    def test_compose_autostart_intent_survives_stopped_docker(self):
        self.controller.containers = Mock(side_effect=ControlError('Docker가 중지되어 있습니다.'))
        self.controller.act('compose-demo', 'autostart-off')
        self.assertFalse(self.controller.load()['services'][0]['autostart'])
        self.run.assert_not_called()
        self.controller.act('compose-demo', 'autostart-on')
        self.assertTrue(self.controller.load()['services'][0]['autostart'])
        for action in ('enable', 'disable', 'start', 'stop', 'restart', 'remove'):
            with self.subTest(action=action):
                with self.assertRaises(ControlError):
                    self.controller.act('compose-demo', action, confirmed=True)

    def test_audit_log_rotates_over_512kib(self):
        audit_path = Path(self.temp.name) / 'audit.jsonl'
        audit_path.write_text(''.join(json.dumps({'n': i, 'pad': 'x' * 900}) + '\n' for i in range(600)))
        self.assertGreater(audit_path.stat().st_size, 512 * 1024)
        self.controller.audit({'op': 'action', 'id': 'x', 'action': 'stop'}, uid=1)
        kept = audit_path.read_text().splitlines()
        self.assertEqual(len(kept), 201)
        self.assertEqual(json.loads(kept[0])['n'], 400)
        self.assertEqual(json.loads(kept[-1])['op'], 'action')

    def test_monitor_exposes_channel_errors(self):
        from wsl_resource_guard.config import Settings
        from wsl_resource_guard.processes import ProcessSnapshot
        settings = Settings(state_dir=self.temp.name)
        state_file = Path(self.temp.name) / 'state.json'
        state_file.write_text('{"severity":"normal","metrics":{"mem_total_kib":100},'
                              '"last_channel_errors":{"gmail":"smtp down"}}')
        snapshot = ProcessSnapshot({}, [], [], {}, {})
        with patch('wsl_resource_guard.config.Settings.load', return_value=settings):
            with patch('wsl_resource_guard.processes.build_snapshot', return_value=snapshot):
                with patch('wsl_resource_guard.notifications.Notifier') as notifier:
                    notifier.return_value.channel_status.return_value = {}
                    result = self.controller.monitor()
        self.assertEqual(result['alerts']['channel_errors'], {'gmail': 'smtp down'})

    def test_settings_info_reads_owner_secrets_without_leaking(self):
        # The controller runs as root; the settings view must read the
        # registered owner's config and secrets, never root's.
        home = Path(self.temp.name) / 'owner-home'
        config_dir = home / '.config/wsl-resource-guard'
        config_dir.mkdir(parents=True)
        fixture_value = 'test-only-gmail-app-password'
        (config_dir / 'config.toml').write_text('email_heartbeat_minute = 30\n')
        (config_dir / 'secrets.json').write_text(json.dumps(
            {'gmail_user': 'sender@example.test', 'gmail_app_password': fixture_value,
             'gmail_to': 'receiver@example.test'}))
        self.owner_home = home
        result = self.controller.dispatch({'op': 'settings'}, uid=1, web_uid=999)
        self.assertEqual(result['channels']['gmail'], 'configured')
        rules = {rule['key']: rule for rule in result['rules']}
        # A 0 bound is real for numeric rules; bool/quiet rules have none.
        self.assertEqual(rules['email_heartbeat_minute']['min'], 0)
        self.assertEqual(rules['email_heartbeat_minute']['max'], 59)
        self.assertIsNone(rules['gmail_enabled']['min'])
        self.assertIsNone(rules['alert_quiet_hours']['max'])
        payload = json.dumps(result)
        self.assertNotIn(fixture_value, payload)
        self.assertNotIn('sender@example.test', payload)

    def test_settings_info_carries_group_and_defaults(self):
        from wsl_resource_guard.config import CONFIG_GROUPS, CONFIG_RULES, Settings
        defaults = Settings()
        group_of = {key: title for title, keys in CONFIG_GROUPS for key in keys}
        home = Path(self.temp.name) / 'owner-home'
        home.mkdir()
        self.owner_home = home
        result = self.controller.dispatch({'op': 'settings'}, uid=1, web_uid=999)
        self.assertEqual(result['groups'], [title for title, _ in CONFIG_GROUPS])
        self.assertEqual({rule['key'] for rule in result['rules']}, set(CONFIG_RULES))
        for rule in result['rules']:
            with self.subTest(key=rule['key']):
                self.assertEqual(rule['group'], group_of[rule['key']])
                # Type-consistent defaults straight from Settings().
                self.assertEqual(rule['default'], getattr(defaults, rule['key']))
                self.assertIs(type(rule['default']), type(getattr(defaults, rule['key'])))

    def test_alerts_op_returns_episodes_and_7d_reason_rollup(self):
        from wsl_resource_guard.config import Settings
        now = time.time()
        day = datetime.now().astimezone().strftime('%Y-%m-%d')
        lines = [
            {'timestamp': now - 120, 'severity': 'warning',
             'reasons': ['가용 RAM 1.5 GiB', 'swap-out 300 MiB/min']},
            {'timestamp': now - 60, 'severity': 'warning',
             'reasons': ['가용 RAM 1.4 GiB']},
            {'timestamp': now - 30, 'severity': 'normal', 'reasons': []},
            {'timestamp': now - 10, 'severity': 'critical',
             'reasons': ['메모리 완전 정체 PSI 25%']},
        ]
        path = Path(self.temp.name) / f'history-{day}.jsonl'
        path.write_text('\n'.join(json.dumps(r) for r in lines) + '\n')
        settings = Settings(state_dir=self.temp.name)
        with patch('wsl_resource_guard.config.Settings.load', return_value=settings):
            result = self.controller.dispatch({'op': 'alerts'}, uid=1, web_uid=999)
        self.assertEqual(len(result['episodes']), 2)
        self.assertTrue(result['episodes'][1]['ongoing'])
        rollup = {r['label']: r for r in result['reasons_7d']}
        # Each episode counts a label once, no matter how often it recurred.
        self.assertEqual(rollup['가용 RAM'],
                         {'label': '가용 RAM', 'episodes': 1, 'total_seconds': 60})
        self.assertEqual(rollup['swap-out']['episodes'], 1)
        self.assertEqual(rollup['메모리 완전 정체 PSI']['episodes'], 1)

    def test_history_and_alerts_read_files_outside_the_monitor_lock(self):
        from wsl_resource_guard.config import Settings
        settings = Settings(state_dir=self.temp.name)
        seen = []

        def fake_read(*_args, **_kwargs):
            # Reading/parsing must not hold _monitor_lock: monitor() waits on it.
            acquired = self.controller._monitor_lock.acquire(blocking=False)
            seen.append(acquired)
            if acquired:
                self.controller._monitor_lock.release()
            return []

        with (patch('wsl_resource_guard.config.Settings.load', return_value=settings),
              patch('wsl_resource_guard.history.read_history', side_effect=fake_read) as read):
            self.controller.dispatch({'op': 'history', 'range': '3h'}, uid=1, web_uid=999)
            self.controller.dispatch({'op': 'alerts'}, uid=1, web_uid=999)
            # Cached answers within the TTL must not re-read the files.
            self.controller.dispatch({'op': 'history', 'range': '3h'}, uid=1, web_uid=999)
            self.controller.dispatch({'op': 'alerts'}, uid=1, web_uid=999)
        self.assertEqual(seen, [True, True])
        self.assertEqual(read.call_count, 2)

    def test_history_ranges_cache_independently(self):
        from wsl_resource_guard.config import Settings
        settings = Settings(state_dir=self.temp.name)
        with (patch('wsl_resource_guard.config.Settings.load', return_value=settings),
              patch('wsl_resource_guard.history.read_history', return_value=[]) as read,
              patch('wsl_resource_guard.history.read_history_downsampled',
                    return_value=[]) as read_down):
            for range_key in ('24h', '7d', '24h', '7d', 'bogus', '3h'):
                self.controller.dispatch({'op': 'history', 'range': range_key}, uid=1, web_uid=999)
        # Alternating clients must not evict each other; an unknown key is
        # served (and cached) as 3h instead of creating its own entry.
        self.assertEqual(read.call_count + read_down.call_count, 3)
        self.assertEqual(set(self.controller._history_cache), {'24h', '7d', '3h'})

    def test_restore_dry_run_plans_without_mutating(self):
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        self.controller.containers.return_value = [container('a', True), container('b', False)]
        result = self.controller.restore(dry_run=True)
        self.run.assert_not_called()
        self.assertIn('restart=unless-stopped', result['message'])
        self.assertIn('start 1개', result['message'])

    def test_restore_dispatch_rules(self):
        # 웹 출처는 항상 거부
        with self.assertRaisesRegex(ControlError, '터미널'):
            self.controller.dispatch({'op': 'restore', 'dry_run': True}, uid=999, web_uid=999)
        # 실제 실행은 --confirm 필요
        with self.assertRaisesRegex(ControlError, 'confirm'):
            self.controller.dispatch({'op': 'restore'}, uid=1, web_uid=999)
        self.run.assert_not_called()
        # dry-run은 확인 없이 허용
        self.controller.unit_state = Mock(return_value={'ActiveState': 'active'})
        result = self.controller.dispatch({'op': 'restore', 'dry_run': True}, uid=1, web_uid=999)
        self.assertIn('계획', result['message'])
        self.run.assert_not_called()

    def test_logs_lines_are_clamped(self):
        entry = dict(self.entry, kind='systemd', target='demo.service')
        self.data['services'] = [entry]
        atomic_json(self.path, self.data)
        self.controller.logs('compose-demo', lines=999999)
        self.assertEqual(self.commands()[-1], ('journalctl', '-u', 'demo.service', '-n', '999999', '--no-pager', '-o', 'short-iso'))
        # dispatch는 1..2000으로 클램프
        self.controller.dispatch({'op': 'logs', 'id': 'compose-demo', 'lines': 50000}, uid=1, web_uid=999)
        self.assertEqual(self.commands()[-1][3:5], ('-n', '2000'))
        self.controller.dispatch({'op': 'logs', 'id': 'compose-demo', 'lines': -5}, uid=1, web_uid=999)
        self.assertEqual(self.commands()[-1][3:5], ('-n', '1'))

    def test_ssh_disable_stops_socket_as_well(self):
        self.data['services'] = [dict(self.entry, id='ssh', target='ssh.service', kind='systemd',
                                     category='foundation', notice='ssh')]
        atomic_json(self.path, self.data)
        self.controller.unit_state = Mock(return_value={'LoadState': 'loaded'})
        self.controller.act('ssh', 'disable', confirmed=True)
        self.assertIn(('systemctl', 'disable', 'ssh.socket'), self.commands())
        self.assertIn(('systemctl', 'stop', 'ssh.socket'), self.commands())

    def test_registry_is_private(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_inspection_never_activates_stopped_docker_socket(self):
        controller = Controller(self.path, self.run)
        controller.unit_state = Mock(return_value={'ActiveState': 'inactive'})
        with self.assertRaises(ControlError):
            controller.containers()
        self.run.assert_not_called()

    def test_docker_disable_also_stops_activation_socket(self):
        self.data['services'] = [dict(self.entry, id='docker', target='docker.service', kind='systemd',
                                     category='foundation', notice='all apps')]
        atomic_json(self.path, self.data)
        self.controller.unit_state = Mock(return_value={'LoadState': 'loaded'})
        self.controller.act('docker', 'disable', confirmed=True)
        self.assertIn(('systemctl', 'disable', 'docker.socket'), self.commands())
        self.assertIn(('systemctl', 'stop', 'docker.socket'), self.commands())

    def test_snapshot_reports_drift(self):
        self.data['services'][0]['autostart'] = False
        atomic_json(self.path, self.data)
        row = self.controller.snapshot()['services'][0]
        self.assertTrue(row['drift'])
        self.assertEqual(row['state'], 'active')
        self.assertFalse(row['autostart'])

    def test_monitor_reuses_cache_and_never_calls_mutating_sample(self):
        from wsl_resource_guard.config import Settings
        from wsl_resource_guard.processes import ProcessSnapshot
        settings = Settings(state_dir=self.temp.name)
        state_file = Path(self.temp.name) / 'state.json'
        state_file.write_text('{"severity":"warning","updated_at":123,"metrics":{"mem_total_kib":100}}')
        before = state_file.read_bytes()
        snapshot = ProcessSnapshot({}, [], [], {}, {})
        with patch('wsl_resource_guard.config.Settings.load', return_value=settings):
            with patch('wsl_resource_guard.processes.build_snapshot', return_value=snapshot) as collect:
                with patch('wsl_resource_guard.daemon.sample', side_effect=AssertionError('must not sample')):
                    with patch('wsl_resource_guard.notifications.Notifier') as notifier:
                        notifier.return_value.channel_status.return_value = {'gmail': 'configured'}
                        first = self.controller.monitor()
                        second = self.controller.monitor(force=True)
        self.assertIs(first, second)
        self.assertEqual(collect.call_count, 1)
        collect.assert_called_once_with(settings.project_roots, uid=os.getuid())
        self.assertEqual(first['severity'], 'warning')
        self.assertEqual(state_file.read_bytes(), before)
        self.assertNotIn('secrets', json.dumps(first))

    def _kill_snapshot(self, cgroup='/user.slice/wrg-demo.scope'):
        from wsl_resource_guard.processes import ProcessInfo, ProcessSnapshot, SessionUsage
        uid = os.getuid()
        fields = dict(uid=uid, state='S', rss_kib=1, swap_kib=0, age_seconds=10, cwd='/x')
        root = ProcessInfo(pid=100, ppid=1, name='codex', cgroup=cgroup, command='codex', **fields)
        child = ProcessInfo(pid=101, ppid=100, name='node', cgroup=cgroup, command='node', **fields)
        session = SessionUsage(root_pid=100, provider='codex', root_name='codex', project='demo',
                               age_seconds=10, rss_kib=1, cgroup=cgroup)
        return ProcessSnapshot({100: root, 101: child}, [session], [], {100: 100, 101: 100}, {100: 'codex'})

    def _kill_settings(self):
        from wsl_resource_guard.config import Settings
        return Settings(state_dir=self.temp.name)

    def test_kill_tree_signals_session_tree_with_sigterm(self):
        with patch('wsl_resource_guard.config.Settings.load', return_value=self._kill_settings()):
            with patch('wsl_resource_guard.processes.build_snapshot', return_value=self._kill_snapshot()):
                with patch('wsl_resource_guard.service_control.os.kill') as kill:
                    result = self.controller.kill_tree(100)
        self.assertEqual(sorted(call.args[0] for call in kill.call_args_list), [100, 101])
        self.assertTrue(all(call.args[1] == signal.SIGTERM for call in kill.call_args_list))
        self.assertIn('SIGTERM', result['message'])

    def test_kill_tree_rejects_unknown_pid_and_systemd_units(self):
        with patch('wsl_resource_guard.config.Settings.load', return_value=self._kill_settings()):
            with patch('wsl_resource_guard.processes.build_snapshot', return_value=self._kill_snapshot()):
                with patch('wsl_resource_guard.service_control.os.kill') as kill:
                    with self.assertRaises(ControlError):
                        self.controller.kill_tree(999)
                    with self.assertRaises(ControlError):
                        self.controller.kill_tree(1)
            with patch('wsl_resource_guard.processes.build_snapshot',
                       return_value=self._kill_snapshot(cgroup='/system.slice/opencode-web.service')):
                with patch('wsl_resource_guard.service_control.os.kill') as kill2:
                    with self.assertRaisesRegex(ControlError, '서비스'):
                        self.controller.kill_tree(100)
        kill.assert_not_called()
        kill2.assert_not_called()

    def test_kill_dispatch_requires_confirmation(self):
        with self.assertRaisesRegex(ControlError, '확인'):
            self.controller.dispatch({'op': 'kill', 'pid': 100}, uid=1, web_uid=999)
        audit = json.loads((Path(self.temp.name) / 'audit.jsonl').read_text().splitlines()[-1])
        self.assertEqual(audit['op'], 'kill')
        self.assertFalse(audit['ok'])
        with patch('wsl_resource_guard.config.Settings.load', return_value=self._kill_settings()):
            with patch('wsl_resource_guard.processes.build_snapshot', return_value=self._kill_snapshot()):
                with patch('wsl_resource_guard.service_control.os.kill'):
                    result = self.controller.dispatch(
                        {'op': 'kill', 'pid': 100, 'confirmed': True}, uid=999, web_uid=999)
        self.assertIn('SIGTERM', result['message'])
        audit = json.loads((Path(self.temp.name) / 'audit.jsonl').read_text().splitlines()[-1])
        self.assertEqual((audit['op'], audit['id'], audit['ok']), ('kill', 100, True))

    def test_service_memory_records_samples_and_rotates(self):
        self.controller.snapshot = Mock(return_value={'services': [
            dict(self.entry, memory_bytes=1024 * 1024 * 512),
            dict(self.entry, id='systemd-x', memory_bytes=None),
        ]})
        self.controller.record_service_memory()
        day = datetime.now().astimezone().strftime('%Y-%m-%d')
        path = Path(self.temp.name) / f'service-memory-{day}.jsonl'
        self.assertTrue(path.exists())
        record = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual(record['services'], {'compose-demo': 1024 * 1024 * 512})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # 오래된 파일 회전
        old = Path(self.temp.name) / 'service-memory-2000-01-01.jsonl'
        old.write_text('{"timestamp":1,"services":{}}\n')
        self.controller.record_service_memory()
        self.assertFalse(old.exists())

    def test_service_memory_skips_when_nothing_measured(self):
        self.controller.snapshot = Mock(return_value={'services': [
            dict(self.entry, memory_bytes=None)]})
        self.controller.record_service_memory()
        self.assertFalse(list(Path(self.temp.name).glob('service-memory-*.jsonl')))

    def test_service_memory_history_is_chronological_and_read_only(self):
        directory = Path(self.temp.name)
        day = datetime.now().astimezone().strftime('%Y-%m-%d')
        (directory / f'service-memory-{day}.jsonl').write_text(
            '{"timestamp":1,"services":{"a":1}}\n'
            '{"timestamp":2,"services":{"a":2}}\n'
            'broken\n'
            '{"timestamp":3,"services":{"a":3}}\n')
        records = self.controller.service_memory_history()
        self.assertEqual([r['timestamp'] for r in records], [1, 2, 3])
        # 읽기 op은 감사 기록을 만들지 않는다
        self.assertFalse((directory / 'audit.jsonl').exists())
        result = self.controller.dispatch({'op': 'service-memory'}, uid=1, web_uid=999)
        # 응답은 5분 구간 최댓값으로 집계된다 — 세 샘플은 한 구간이다.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['services'], {'a': 3})
        self.assertFalse((directory / 'audit.jsonl').exists())

    def _memory_file(self, series_by_id, start=1000.0, step=60.0):
        count = max(len(v) for v in series_by_id.values())
        day = datetime.now().astimezone().strftime('%Y-%m-%d')
        lines = [json.dumps({'timestamp': start + i * step,
                             'services': {sid: vals[i] for sid, vals in series_by_id.items()
                                          if i < len(vals)}})
                 for i in range(count)]
        (Path(self.temp.name) / f'service-memory-{day}.jsonl').write_text('\n'.join(lines) + '\n')

    def test_service_memory_leaks_detects_steady_growth(self):
        growing = [100 * 2**20 + i * 4 * 2**20 for i in range(200)]
        self._memory_file({'compose-demo': growing, 'other': [50 * 2**20] * 200})
        leaks = self.controller.service_memory_leaks()
        self.assertEqual([row['id'] for row in leaks], ['compose-demo'])
        self.assertEqual(leaks[0]['name'], 'Demo')
        self.assertAlmostEqual(leaks[0]['hours'], 199 * 60 / 3600, places=1)
        self.assertEqual(leaks[0]['from_bytes'], 100 * 2**20)

    def test_service_memory_leaks_rejects_insufficient_data(self):
        self._memory_file({'compose-demo': [i * 4 * 2**20 for i in range(100)]})
        self.assertEqual(self.controller.service_memory_leaks(), [])

    def test_service_memory_leaks_rejects_flat_and_short_span(self):
        self._memory_file({'compose-demo': [300 * 2**20 if i % 2 else 350 * 2**20
                                            for i in range(200)]})
        self.assertEqual(self.controller.service_memory_leaks(), [])
        # 충분히 늘어났지만 관측 창이 3시간 미만이면 제외
        self._memory_file({'compose-demo': [100 * 2**20 + i * 4 * 2**20 for i in range(200)]},
                          step=30.0)
        self.assertEqual(self.controller.service_memory_leaks(), [])

    def test_monitor_includes_leaks(self):
        from wsl_resource_guard.config import Settings
        from wsl_resource_guard.processes import ProcessSnapshot
        settings = Settings(state_dir=self.temp.name)
        (Path(self.temp.name) / 'state.json').write_text(
            '{"severity":"normal","metrics":{"mem_total_kib":100}}')
        self._memory_file({'compose-demo': [100 * 2**20 + i * 4 * 2**20 for i in range(200)]})
        snapshot = ProcessSnapshot({}, [], [], {}, {})
        with patch('wsl_resource_guard.config.Settings.load', return_value=settings):
            with patch('wsl_resource_guard.processes.build_snapshot', return_value=snapshot):
                with patch('wsl_resource_guard.notifications.Notifier') as notifier:
                    notifier.return_value.channel_status.return_value = {}
                    result = self.controller.monitor()
        self.assertEqual([row['id'] for row in result['leaks']], ['compose-demo'])

    def test_monitor_annotates_stale_and_killable_per_row(self):
        from wsl_resource_guard.config import Settings
        from wsl_resource_guard.processes import (
            McpUsage, ProcessInfo, ProcessSnapshot, SessionUsage,
        )
        settings = Settings(state_dir=self.temp.name, stale_session_hours=48)
        (Path(self.temp.name) / 'state.json').write_text(
            '{"severity":"normal","metrics":{"mem_total_kib":100}}')

        def proc(pid, cgroup):
            return ProcessInfo(pid=pid, ppid=1, uid=os.getuid(), name='proc', state='S',
                               rss_kib=1, swap_kib=0, age_seconds=10, cwd='/',
                               cgroup=cgroup, command='x')

        sessions = [
            SessionUsage(root_pid=10, provider='claude', root_name='claude', project='demo',
                         age_seconds=100.0, youngest_process_age_seconds=49 * 3600,
                         cgroup='/user.slice/demo.scope'),
            SessionUsage(root_pid=11, provider='opencode', root_name='opencode', project='demo',
                         age_seconds=100.0, youngest_process_age_seconds=49 * 3600,
                         cgroup='/system.slice/opencode-web.service'),
        ]
        groups = [McpUsage(root_pid=20, session_root_pid=11, provider='opencode',
                           project='demo', root_name='mcp', age_seconds=10)]
        snap = ProcessSnapshot(
            {10: proc(10, '/user.slice/demo.scope'),
             11: proc(11, '/system.slice/opencode-web.service'),
             20: proc(20, '/system.slice/opencode-web.service')},
            sessions, [], {}, {}, groups)
        with patch('wsl_resource_guard.config.Settings.load', return_value=settings):
            with patch('wsl_resource_guard.processes.build_snapshot', return_value=snap):
                with patch('wsl_resource_guard.notifications.Notifier') as notifier:
                    notifier.return_value.channel_status.return_value = {}
                    result = self.controller.monitor()
        rows = {row['root_pid']: row for row in result['sessions']}
        self.assertTrue(rows[10]['killable'])
        self.assertTrue(rows[10]['stale'])
        self.assertNotIn('kill_block_reason', rows[10])
        self.assertFalse(rows[11]['killable'])
        # opencode is excluded from the stale rule even when idle for days.
        self.assertFalse(rows[11]['stale'])
        self.assertIn('systemd', rows[11]['kill_block_reason'])
        # An MCP root inside a service cgroup is blocked too, not just sessions.
        self.assertFalse(result['mcp'][0]['killable'])


class RegistrationMetadataTests(unittest.TestCase):
    setUp = ControllerTests.setUp
    def test_explicit_display_name_and_url_are_saved_without_starting(self):
        candidate=dict(id='llm-usage',key='systemd:llm-usage.service',target='llm-usage.service',kind='systemd',name='llm-usage',owned=False,autostart=True,category='app')
        self.controller.discover=Mock(return_value=[candidate])
        self.controller.register(candidate['key'],'LLM Usage','https://main.example.ts.net:9444/')
        saved=self.controller.load()['services'][-1]
        self.assertEqual(saved['name'],'LLM Usage')
        self.assertEqual(saved['url'],'https://main.example.ts.net:9444/')
        self.run.assert_not_called()

    def test_optional_opencode_port_fills_dashboard_url(self):
        candidate = dict(id='opencode-web', key='systemd:opencode-web.service',
                         target='opencode-web.service', kind='systemd', name='OpenCode',
                         owned=False, autostart=True, category='app')
        self.controller.discover = Mock(return_value=[candidate])
        self.controller.register(candidate['key'], 'OpenCode')
        self.assertEqual(self.controller.load()['services'][-1]['url'], 'https://test:4096')
        self.run.assert_not_called()

    def test_portless_origin_still_builds_valid_service_url(self):
        self.assertEqual(service_control._service_url('https://test', 4096), 'https://test:4096')

    def test_generic_application_does_not_receive_private_port_defaults(self):
        candidate = dict(id='example-web', key='systemd:example-web.service', target='example-web.service',
                         kind='systemd', name='example-web', owned=False, autostart=True, category='app')
        self.controller.discover = Mock(return_value=[candidate])
        self.controller.register(candidate['key'], 'Example Web')
        self.assertEqual(self.controller.load()['services'][-1]['url'], '')
        self.run.assert_not_called()

    def test_registration_rejects_unsafe_metadata(self):
        for url in ('javascript:alert(1)','https://user:secret@example.com','https://example.com\n','https://example.com:abc'):
            with self.subTest(url=url),self.assertRaises((ControlError,ValueError)):
                self.controller.register('systemd:llm-usage.service','LLM Usage',url)
        with self.assertRaises(ControlError):self.controller.register('x','name\n','')
