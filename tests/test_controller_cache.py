import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from wsl_resource_guard.service_control import Controller


class ControllerCacheTests(unittest.TestCase):
    def test_forced_refresh_replaces_cached_disk_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(registry=Path(directory) / 'registry.json')
            controller._owner_read = Mock(side_effect=[{'updated_at': 1}, {'updated_at': 2}])
            self.assertEqual(controller.disks()['updated_at'], 1)
            self.assertEqual(controller.disks(force=True)['updated_at'], 2)
            self.assertEqual(controller.disks()['updated_at'], 2)
