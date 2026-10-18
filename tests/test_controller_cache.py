import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from wsl_resource_guard.service_control import Controller


class ControllerCacheTests(unittest.TestCase):
    def test_forced_refresh_replaces_cached_disk_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(registry=Path(directory) / 'registry.json')
            controller._owner_read = Mock(side_effect=[{'updated_at': 1}, {'updated_at': 2}])
            self.assertEqual(controller.disks()['updated_at'], 1)
            self.assertEqual(controller.disks(force=True)['updated_at'], 2)
            self.assertEqual(controller.disks()['updated_at'], 2)

    def test_long_history_ranges_are_reused_for_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(registry=Path(directory) / 'registry.json')
            controller._owner_read = Mock(side_effect=lambda operation, args=None: [operation, args['range']])
            clock = [1000.0]
            with patch('wsl_resource_guard.service_control.time.monotonic', side_effect=lambda: clock[0]):
                for key in ('3h', '24h', '7d', '14d'):
                    controller.history(key)
                    controller.attribution(key)
                clock[0] += 120
                for key in ('3h', '24h', '7d', '14d'):
                    controller.history(key)
                    controller.attribution(key)
                reread = [call.args[1]['range'] for call in controller._owner_read.call_args_list[8:]]
                self.assertEqual(sorted(reread), ['24h', '24h', '3h', '3h'])
                clock[0] += 200
                controller.history('14d')
                self.assertEqual(controller._owner_read.call_args_list[-1].args, ('history', {'range': '14d'}))

