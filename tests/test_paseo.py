import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from wsl_resource_guard import paseo, owner_worker


class PaseoProbeTests(unittest.TestCase):
    def test_partial_health_and_disabled_relay_are_distinct(self):
        good = {k: {'state': 'ok'} for k in ('http', 'local', 'relay')}
        self.assertTrue(paseo.normalize({'checks': good})['ok'])
        for state, expected in [('failed', False), ('unknown', None), ('disabled', True)]:
            value = paseo.normalize({'checks': {**good, 'relay': {'state': state}}})
            self.assertIs(value['ok'], expected)
            self.assertEqual(value['checks']['relay']['state'], state)
        self.assertFalse(paseo.normalize({'checks': {**good, 'local': {'state': 'failed'}}})['ok'])

    def test_untrusted_output_never_exposes_credentials_or_fakes_health(self):
        value = paseo.normalize({'ok': True, 'url': 'private', 'error': 'private', 'checks': {
            'http': {'state': 'ok', 'latency_ms': True, 'token': 'private'},
            'local': {'state': 'disabled'}, 'relay': {'state': ['ok'], 'latency_ms': 999999}}})
        self.assertNotIn('private', json.dumps(value))
        self.assertIsNone(value['ok'])
        self.assertEqual(value['checks']['local'], {'state': 'unknown'})
        for invalid in (None, [], {'checks': []}, {'checks': {'local': 'ok'}}):
            self.assertIsNone(paseo.normalize(invalid)['ok'])

    def test_stale_health_does_not_remain_green_or_change_original(self):
        value = paseo.normalize({'checks': {k: {'state': 'ok'} for k in ('http', 'local', 'relay')}}, 1)
        with patch.object(paseo.time, 'time', return_value=200):
            stale = paseo.fresh_health(value)
        self.assertTrue(stale['stale'])
        self.assertIsNone(stale['ok'])
        self.assertEqual(stale['checked_at'], 1)
        self.assertTrue(value['ok'])

    def test_worker_rejects_arbitrary_commands_paths_and_hosts(self):
        for args in ({'host': 'http://other/'}, {'path': '/etc/shadow'}, {'command': 'anything'}):
            with self.assertRaises(ValueError):
                owner_worker.dispatch('paseo-health', args)

    def test_probe_cannot_run_as_root(self):
        with patch.object(paseo.os, 'geteuid', return_value=0), self.assertRaises(PermissionError):
            paseo.probe(Path('/home/demo'))

    def test_missing_binary_timeout_and_bad_output_are_unknown(self):
        with patch.object(paseo.os, 'geteuid', return_value=1000):
            with patch.object(paseo.shutil, 'which', return_value=None):
                self.assertIsNone(paseo.probe(Path('/tmp'))['ok'])
            with patch.object(paseo.shutil, 'which', return_value='/usr/bin/node'):
                for result in (subprocess.TimeoutExpired('node', 17),
                               Mock(returncode=1, stdout='private'),
                               Mock(returncode=0, stdout='not-json'),
                               Mock(returncode=0, stdout='x' * 9000)):
                    with patch.object(paseo.subprocess, 'run') as run:
                        if isinstance(result, Exception):
                            run.side_effect = result
                        else:
                            run.return_value = result
                        value = paseo.probe(Path('/tmp'))
                        self.assertIsNone(value['ok'])
                        self.assertNotIn('private', json.dumps(value))
                        self.assertEqual(run.call_args.kwargs['timeout'], 17)
                        self.assertNotIn('NODE_OPTIONS', run.call_args.kwargs['env'])


if __name__ == '__main__':
    unittest.main()
