"""CPU/IO pressure and GPU memory: observed values or an explicit gap, never zeros."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard import gpu, metrics
from wsl_resource_guard.disks import _PROBES


class PressureTests(unittest.TestCase):
    def test_cpu_and_io_pressure_are_read_with_their_own_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'cpu').write_text('some avg10=0.51 avg60=0.39 avg300=0.38 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
            (root / 'io').write_text('some avg10=0.00 avg60=1.25 avg300=0.38 total=1\nfull avg10=0.00 avg60=0.75 avg300=0.37 total=2\n')
            self.assertEqual(metrics.read_pressure(root), {
                'cpu': {'some_avg60': 0.39, 'full_avg60': 0.0, 'status': 'normal'},
                'io': {'some_avg60': 1.25, 'full_avg60': 0.75, 'status': 'normal'}})
            (root / 'cpu').write_text('some avg10=0.51 avg60=0.39 avg300=0.38 total=1\n')   # older kernels: no full line
            (root / 'io').unlink()
            self.assertEqual(metrics.read_pressure(root), {
                'cpu': {'some_avg60': 0.39, 'full_avg60': None, 'status': 'normal'},
                'io': {'some_avg60': None, 'full_avg60': None, 'status': 'missing'}})
            (root / 'cpu').write_text('some avg60=nan\n')
            self.assertEqual(metrics.read_pressure(root)['cpu'], {'some_avg60': None, 'full_avg60': None, 'status': 'error'})


class GpuTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: _PROBES.pop('gpu', None))
        tool = patch.object(gpu, 'nvidia_smi_path', return_value='/fixture/nvidia-smi')
        tool.start()
        self.addCleanup(tool.stop)

    def run_result(self, stdout, code=0):
        return patch.object(gpu.subprocess, 'run',
                            return_value=subprocess.CompletedProcess([], code, stdout=stdout, stderr=''))

    def test_devices_are_parsed_and_gated_by_the_interval(self):
        with self.run_result('NVIDIA GeForce RTX 3070, 2397, 8192, 7\nSecond, [N/A], 4096, [N/A]\n') as run:
            first = gpu.read_gpu(now=1000.0)
            self.assertEqual((first['status'], first['observed_at'], first['attempted_at']), ('ok', 1000.0, 1000.0))
            self.assertEqual(first['devices'], [
                {'name': 'NVIDIA GeForce RTX 3070', 'memory_used_bytes': 2397 * 2**20,
                 'memory_total_bytes': 8192 * 2**20, 'utilization_percent': 7.0},
                {'name': 'Second', 'memory_used_bytes': None, 'memory_total_bytes': 4096 * 2**20,
                 'utilization_percent': None}])
            self.assertIs(gpu.read_gpu(first, interval=60, now=1030.0), first)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], '/fixture/nvidia-smi')

    def test_no_tool_or_failure_is_a_gap_not_zero(self):
        with patch.object(gpu, 'nvidia_smi_path', return_value=None):
            missing = gpu.read_gpu(now=1000.0)
        self.assertEqual((missing['status'], missing['observed_at'], missing['devices']), ('missing', None, []))
        with self.run_result('NVIDIA, 100, 8192, 1\n'):
            good = gpu.read_gpu(now=1000.0)
        for kwargs in (dict(return_value=subprocess.CompletedProcess([], 9, stdout='', stderr='')),
                       dict(side_effect=subprocess.TimeoutExpired('nvidia-smi', 5)),
                       dict(return_value=subprocess.CompletedProcess([], 0, stdout='garbage line\n', stderr=''))):
            with self.subTest(kwargs=kwargs), patch.object(gpu.subprocess, 'run', **kwargs):
                failed = gpu.read_gpu(good, interval=60, now=2000.0)
                self.assertEqual((failed['status'], failed['observed_at'], failed['attempted_at']), ('error', 1000.0, 2000.0))
                self.assertEqual(failed['devices'], good['devices'])    # last observation, marked not current
        for bad in ('text', {'devices': 5}, {'attempted_at': 'soon'}):
            with self.run_result('NVIDIA, 100, 8192, 1\n'):
                self.assertEqual(gpu.read_gpu(bad, now=3000.0)['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
