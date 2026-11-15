"""Windows host snapshot: fixed script, bounded execution, honest gaps."""
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch

from wsl_resource_guard import hostbridge
from wsl_resource_guard.disks import _PROBES

RAW = {
    'schema': 1,
    'os': {'total_kb': 32 * 1024 * 1024, 'free_kb': 8 * 1024 * 1024, 'build': '26200',
           'boot': '2026-11-14T06:58:05.5000000Z'},
    'memory': {'available': 7 * 2 ** 30, 'committed': 40 * 2 ** 30, 'commit_limit': 100 * 2 ** 30},
    'pagefile': [{'allocated_mb': 70000, 'used_mb': 3000, 'peak_mb': 27000}],
    'vmmem': [{'name': 'vmmemWSL', 'ws': 12 * 2 ** 30, 'private': 14 * 2 ** 30}],
    'top': [{'name': 'vmmemWSL', 'ws': 12 * 2 ** 30}, {'name': 'app\x07', 'ws': 2 ** 30}],
    'volumes': [{'id': 'C:', 'total': 1000, 'free': 200, 'fs': 'NTFS'}, {'id': 'G:', 'total': 500, 'free': 100, 'fs': 'FAT32'},
                {'id': '..', 'total': 1, 'free': 1, 'fs': 'x'}],
    'wsl_list': '  NAME              STATE           VERSION\n* Ubuntu            Running         2\n'
                '  docker-desktop    Stopped         2',
    'lxss': [{'name': 'Ubuntu', 'base': 'E:\\WSL\\Ubuntu'},
             {'name': 'docker-desktop', 'base': '\\\\?\\C:\\Users\\USER\\AppData\\Local\\Docker\\wsl\\main'}],
    'wslconfig': '[wsl2]\n# comment with secret=value\nmemory=20GB\nswap=8GB\nkernelCommandLine = anything goes\n'
                 '[experimental]\nautoMemoryReclaim=dropCache\n',
}


def completed(raw=RAW, code=0):
    return subprocess.CompletedProcess([], code, stdout=json.dumps(raw).encode(), stderr=b'')


class NormalizeTests(unittest.TestCase):
    def test_script_output_becomes_typed_bounded_fields(self):
        data = hostbridge.normalize(RAW)
        self.assertEqual(data['memory'], {
            'total_bytes': 32 * 2 ** 30, 'available_bytes': 7 * 2 ** 30,
            'commit_used_bytes': 40 * 2 ** 30, 'commit_limit_bytes': 100 * 2 ** 30,
            'pagefile_allocated_bytes': 70000 * 2 ** 20, 'pagefile_used_bytes': 3000 * 2 ** 20,
            'pagefile_peak_bytes': 27000 * 2 ** 20})
        self.assertEqual(data['vmmem'], {'working_set_bytes': 12 * 2 ** 30, 'private_bytes': 14 * 2 ** 30})
        self.assertEqual([v['id'] for v in data['volumes']], ['C:', 'G:'])
        self.assertEqual(data['distros'], [
            {'name': 'Ubuntu', 'state': 'Running', 'version': 2, 'default': True, 'vhd_dir': 'E:\\WSL\\Ubuntu'},
            {'name': 'docker-desktop', 'state': 'Stopped', 'version': 2, 'default': False,
             'vhd_dir': 'C:\\Users\\USER\\AppData\\Local\\Docker\\wsl\\main'}])
        self.assertEqual(data['wslconfig'], {'present': True, 'memory': '20GB', 'swap': '8GB', 'processors': None,
                                             'auto_memory_reclaim': 'dropCache', 'sparse_vhd': None})
        self.assertEqual(data['processes'][1]['name'], 'app')  # control characters removed
        self.assertEqual(data['windows']['build'], '26200')
        self.assertAlmostEqual(data['windows']['boot_at'], 1794639485.0, delta=1)

    def test_missing_parts_stay_unknown_instead_of_zero(self):
        data = hostbridge.normalize({'schema': 1, 'os': None, 'memory': None, 'pagefile': [], 'vmmem': [],
                                     'top': None, 'volumes': None, 'wsl_list': None, 'lxss': None, 'wslconfig': None})
        self.assertTrue(all(value is None for value in data['memory'].values()))
        self.assertIsNone(data['vmmem'])
        self.assertEqual((data['volumes'], data['distros'], data['processes']), ([], [], []))
        self.assertIsNone(data['wslconfig']['present'])            # unreadable: unknown, not absent
        self.assertIs(hostbridge.parse_wslconfig('')['present'], False)
        self.assertIs(hostbridge.parse_wslconfig('[wsl2]\n')['present'], True)
        # A part that failed on Windows arrives as null or missing, never as 0.
        partial = hostbridge.normalize(dict(RAW, pagefile=[{'allocated_mb': None, 'used_mb': None, 'peak_mb': None}],
                                            os={'total_kb': None, 'free_kb': None}, memory={'available': None}))
        self.assertEqual({v for v in partial['memory'].values()}, {None})
        with self.assertRaises(ValueError):
            hostbridge.normalize({'schema': 2})
        with self.assertRaises(ValueError):
            hostbridge.normalize(['not', 'a', 'dict'])

    def test_negative_or_non_numeric_counters_are_rejected(self):
        raw = dict(RAW, memory={'available': -1, 'committed': 'many', 'commit_limit': True},
                   os=dict(RAW['os'], free_kb=float('nan')))
        memory = hostbridge.normalize(raw)['memory']
        self.assertEqual((memory['available_bytes'], memory['commit_used_bytes'], memory['commit_limit_bytes']),
                         (None, None, None))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: _PROBES.pop('host-snapshot', None))

    def patched(self, **kwargs):
        # windows_mounts is stubbed too: collection must not stat the real Windows drives.
        mounts = patch.object(hostbridge, 'windows_mounts', return_value={'C': '/mnt/c'})
        mounts.start()
        self.addCleanup(mounts.stop)
        return (patch.object(hostbridge, 'powershell_path', return_value=Path('/fixture/powershell.exe')),
                patch.object(hostbridge.subprocess, 'run', **kwargs),
                patch.object(hostbridge, 'current_distro_name', return_value='Ubuntu'))

    def test_successful_collection_is_marked_observed(self):
        a, b, c = self.patched(return_value=completed())
        with a, b as run, c:
            snapshot = hostbridge.read_host_snapshot(now=1000.0)
        self.assertEqual((snapshot['schema'], snapshot['status'], snapshot['observed_at'], snapshot['attempted_at']),
                         (1, 'ok', 1000.0, 1000.0))
        self.assertEqual(snapshot['distro'], 'Ubuntu')
        self.assertEqual([(v['id'], v['mounted']) for v in snapshot['volumes']], [('C:', True), ('G:', False)])
        self.assertEqual((snapshot['vhd_path'], snapshot['failures']), ('', 0))  # E: is not mounted here
        command = run.call_args.args[0]
        self.assertEqual(command[1:4], ['-NoProfile', '-NonInteractive', '-EncodedCommand'])
        self.assertEqual(len(command), 5)  # the fixed script only, no caller-supplied argument

    def test_any_attempt_waits_a_full_interval(self):
        a, b, c = self.patched(return_value=completed())
        with a, b as run, c:
            first = hostbridge.read_host_snapshot(now=1000.0)
            self.assertIs(hostbridge.read_host_snapshot(first, interval=60, now=1030.0), first)
            self.assertEqual(run.call_count, 1)
            second = hostbridge.read_host_snapshot(first, interval=60, now=1061.0)
            self.assertEqual((run.call_count, second['observed_at']), (2, 1061.0))
            forced = hostbridge.read_host_snapshot(second, interval=60, force=True, now=1062.0)
            self.assertEqual((run.call_count, forced['observed_at']), (3, 1062.0))
        with patch.object(hostbridge, 'powershell_path', return_value=None):
            failed = hostbridge.read_host_snapshot(second, interval=60, now=1200.0)
            self.assertIs(hostbridge.read_host_snapshot(failed, interval=60, now=1230.0), failed)

    def test_consecutive_failures_back_off_and_success_resets(self):
        attempts, snapshot, now = [], {}, 1000.0
        with patch.object(hostbridge, 'powershell_path', return_value=None):
            for _ in range(200):                       # one sample every 15 s for 50 minutes
                before = snapshot.get('attempted_at')
                snapshot = hostbridge.read_host_snapshot(snapshot, interval=60, now=now)
                if snapshot['attempted_at'] != before:
                    attempts.append(now - 1000.0)
                now += 15
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        self.assertEqual(gaps[:4], [120.0, 240.0, 480.0, 900.0])   # doubling, capped at 15 minutes
        self.assertTrue(all(gap == 900.0 for gap in gaps[4:]))
        a, b, c = self.patched(return_value=completed())
        with a, b, c:
            good = hostbridge.read_host_snapshot(snapshot, interval=60, force=True, now=now)
        self.assertEqual((good['status'], good['failures'], hostbridge.retry_interval(good, 60)), ('ok', 0, 60))

    def test_interop_start_failure_is_retried_once_and_named(self):
        broken = subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'/mnt/c/x/powershell.exe: Invalid argument\n')
        a, b, c = self.patched(side_effect=[broken, completed()])
        with a, b as run, c, patch.object(hostbridge, 'INTEROP_RETRY_SECONDS', 0):
            self.assertEqual(hostbridge.read_host_snapshot(now=1000.0)['status'], 'ok')
            self.assertEqual(run.call_count, 2)
        a, b, c = self.patched(return_value=broken)
        with a, b as run, c, patch.object(hostbridge, 'INTEROP_RETRY_SECONDS', 0):
            failed = hostbridge.read_host_snapshot(now=1000.0)
            self.assertEqual((failed['status'], run.call_count), ('error', 2))
            self.assertIn('interop could not start PowerShell', failed['error'])
            self.assertNotIn('/mnt/c', failed['error'])   # stderr itself is never stored

    def test_failure_keeps_the_last_observation_and_says_so(self):
        a, b, c = self.patched(return_value=completed())
        with a, b, c:
            good = hostbridge.read_host_snapshot(now=1000.0)
        cases = (('unavailable', dict(return_value=completed()), None),
                 ('error', dict(return_value=completed(code=1)), Path('/fixture/powershell.exe')),
                 ('error', dict(return_value=subprocess.CompletedProcess([], 0, stdout=b'not json', stderr=b'')),
                  Path('/fixture/powershell.exe')),
                 ('timeout', dict(side_effect=subprocess.TimeoutExpired('powershell', 15)),
                  Path('/fixture/powershell.exe')))
        for status, run, executable in cases:
            with self.subTest(status=status), \
                    patch.object(hostbridge, 'powershell_path', return_value=executable), \
                    patch.object(hostbridge.subprocess, 'run', **run), \
                    patch.object(hostbridge, 'current_distro_name', return_value='Ubuntu'):
                snapshot = hostbridge.read_host_snapshot(good, interval=60, now=2000.0)
                self.assertEqual((snapshot['status'], snapshot['observed_at'], snapshot['attempted_at']),
                                 (status, 1000.0, 2000.0))
                self.assertTrue(snapshot['error'])
                if run.get('return_value') is not None and run['return_value'].returncode:
                    self.assertIn('PowerShell exit 1', snapshot['error'])
                self.assertEqual(snapshot['memory'], good['memory'])
        with patch.object(hostbridge, 'powershell_path', return_value=None):
            first = hostbridge.read_host_snapshot(now=3000.0)
        self.assertEqual((first['status'], first['observed_at']), ('unavailable', None))
        self.assertNotIn('memory', first)

    def test_stalled_host_does_not_hold_the_sampler_or_pile_up(self):
        entered, release = threading.Event(), threading.Event()

        def stalled():
            entered.set()
            release.wait(5)
            return None
        with patch.object(hostbridge, 'powershell_path', side_effect=stalled) as path, \
                patch.object(hostbridge, 'HOST_TIMEOUT_SECONDS', .05):
            try:
                first = hostbridge.read_host_snapshot(now=1000.0)
                second = hostbridge.read_host_snapshot(first, interval=60, force=True, now=1001.0)
                self.assertTrue(entered.is_set())
                self.assertEqual((first['status'], second['status'], path.call_count), ('timeout', 'busy', 1))
            finally:
                release.set()
                _PROBES['host-snapshot'].join(3)

    def test_oversized_output_is_refused(self):
        big = subprocess.CompletedProcess([], 0, stdout=b' ' * (hostbridge.MAX_OUTPUT_BYTES + 1), stderr=b'')
        a, b, c = self.patched(return_value=big)
        with a, b, c:
            self.assertEqual(hostbridge.read_host_snapshot(now=1000.0)['status'], 'error')


class PathTests(unittest.TestCase):
    MOUNTS = {'/mnt/c': ('9p', 'C:\\'), '/mnt/e': ('9p', 'E:\\'), '/': ('ext4', '/dev/sdd'),
              '/usr/lib/wsl/drivers': ('9p', 'drivers')}

    def test_powershell_is_found_only_on_the_mounted_system_drive(self):
        with patch.object(hostbridge, 'mount_table', return_value={'/win/c': ('9p', 'C:\\'), '/mnt/e': ('9p', 'E:\\')}), \
                patch.object(hostbridge.Path, 'exists', autospec=True, return_value=True):
            self.assertEqual(hostbridge.powershell_path(), Path('/win/c') / hostbridge.POWERSHELL_RELATIVE)
        # Another drive carrying the same path is never used.
        with patch.object(hostbridge, 'mount_table', return_value={'/mnt/e': ('9p', 'E:\\')}), \
                patch.object(hostbridge.Path, 'exists', autospec=True, return_value=True) as exists:
            self.assertIsNone(hostbridge.powershell_path())
            self.assertEqual(exists.call_count, 0)
        with patch.object(hostbridge, 'mount_table', return_value=self.MOUNTS), \
                patch.object(hostbridge.Path, 'exists', autospec=True, return_value=False):
            self.assertIsNone(hostbridge.powershell_path())
        with patch.object(hostbridge, 'mount_table', return_value={'/': ('ext4', '/dev/sdd')}):
            self.assertIsNone(hostbridge.powershell_path())
        with patch.object(hostbridge, 'mount_table', side_effect=OSError):
            self.assertIsNone(hostbridge.powershell_path())

    def test_vhd_path_is_kept_only_when_the_file_is_really_there(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'WSL/Ubuntu').mkdir(parents=True)
            data = {'distro': 'Ubuntu', 'distros': hostbridge.normalize(RAW)['distros']}
            mounts = {'E': tmp}
            self.assertEqual(hostbridge._existing_vhd_path(data, mounts), '')          # no ext4.vhdx there
            (Path(tmp) / 'WSL/Ubuntu/ext4.vhdx').write_bytes(b'')
            self.assertEqual(hostbridge._existing_vhd_path(data, mounts), str(Path(tmp) / 'WSL/Ubuntu/ext4.vhdx'))
            self.assertEqual(hostbridge._existing_vhd_path(dict(data, distro='Debian'), mounts), '')
            self.assertEqual(hostbridge._existing_vhd_path(dict(data, distro=''), mounts), '')
            self.assertEqual(hostbridge._existing_vhd_path(data, {'C': tmp}), '')        # its drive is not mounted
            for hostile in ('E:\\WSL\\..\\..\\etc', 'E:\\WSL\\Ubuntu\x00', '\\\\server\\share', 'WSL'):
                odd = dict(data, distros=[dict(data['distros'][0], vhd_dir=hostile)])
                self.assertEqual(hostbridge._existing_vhd_path(odd, mounts), '', hostile)

    def test_detected_path_is_read_defensively_from_stored_state(self):
        self.assertEqual(hostbridge.detected_vhd_path({'vhd_path': '/mnt/e/WSL/Ubuntu/ext4.vhdx'}),
                         '/mnt/e/WSL/Ubuntu/ext4.vhdx')
        for bad in (None, {}, {'vhd_path': None}, {'vhd_path': 5}, {'vhd_path': 'relative'},
                    {'vhd_path': '/mnt/e/x\x00'}, {'vhd_path': '/' + 'a' * 2000}, {'distros': 7}, 'text'):
            self.assertEqual(hostbridge.detected_vhd_path(bad), '', bad)

    def test_distro_name_comes_from_the_unc_root(self):
        for output, expected in (('\\\\wsl.localhost\\Ubuntu\\\n', 'Ubuntu'), ('\\\\wsl$\\Debian\n', 'Debian'),
                                 ('C:\\Windows\n', ''), ('', '')):
            with self.subTest(output=output), patch.object(
                    hostbridge.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr='')):
                self.assertEqual(hostbridge.current_distro_name(), expected)
        with patch.object(hostbridge.subprocess, 'run', side_effect=OSError):
            self.assertEqual(hostbridge.current_distro_name(), '')


if __name__ == '__main__':
    unittest.main()
