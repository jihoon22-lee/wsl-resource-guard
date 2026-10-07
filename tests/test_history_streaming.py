"""Bounded history reads must not erase an otherwise valid large day."""
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from wsl_resource_guard import history, safe_read


def large_day(folder, base, count=8640):
    path = folder / ('history-' + datetime.fromtimestamp(base).strftime('%Y-%m-%d') + '.jsonl')
    with path.open('w') as out:
        for i in range(count):
            sessions = [dict(root_pid=n+100, provider='claude', root_name='claude',
                project=f'project-{n}', age_seconds=3600+i*10, rss_kib=1048576+n,
                swap_kib=1024, mcp_count=2, mcp_rss_kib=2048, process_count=5,
                youngest_process_age_seconds=3600+i*10, persistent=False,
                projects=[dict(project=f'project-{n}', rss_kib=1048576+n, swap_kib=1024,
                               process_count=5, cpu_percent=1.2)]) for n in range(5)]
            out.write(json.dumps(dict(timestamp=base+i*10, severity='normal', reasons=[],
                observations=[], metrics=dict(mem_available_kib=1024, swap_free_kib=100,
                swap_total_kib=1000, psi_some_avg60=0.0, psi_full_avg60=None,
                psi_status='partial'), sessions=sessions))+'\n')
    return path


class StreamingReadTests(unittest.TestCase):
    def test_streaming_buckets_preserve_newest_observation_when_clock_reverses(self):
        rows = iter([{'timestamp':120,'metrics':{'vmmem_bytes':5,'vmmem_observed_at':120}},
                     {'timestamp':60,'metrics':{'vmmem_bytes':10,'vmmem_observed_at':60}}])
        out=history.downsample(rows,300)[0]
        self.assertEqual(out['timestamp'],120)
        self.assertEqual(out['metrics']['vmmem_bytes'],5)

    def test_owner_worker_reports_large_output_failure_instead_of_empty_result(self):
        import pwd
        from types import SimpleNamespace
        from wsl_resource_guard.owner_worker import call_owner
        account=pwd.getpwuid(os.getuid())
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'.local/state/wsl-resource-guard'; state.mkdir(parents=True)
            now=time.time()
            path=state/('history-'+datetime.fromtimestamp(now).strftime('%Y-%m-%d')+'.jsonl')
            with path.open('w') as out:
                for i in range(20):
                    out.write(json.dumps(dict(timestamp=now+i,metrics={},reasons=['x'*500000]))+'\n')
            owner=SimpleNamespace(pw_uid=account.pw_uid,pw_gid=account.pw_gid,
                                  pw_name=account.pw_name,pw_dir=directory)
            with self.assertRaises(OSError): call_owner(owner,'history',{'range':'3h'})

    def test_old_file_size_boundary_does_not_remove_valid_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); path = folder/'history-2026-10-06.jsonl'
            row = b'{"timestamp": 1, "metrics": {}}\n'
            for size in (16*1024*1024-1, 16*1024*1024, 16*1024*1024+1):
                # Legal JSON whitespace, split into bounded lines, isolates the
                # former whole-file byte boundary from the per-line boundary.
                remaining = size-len(row)
                with path.open('wb') as out:
                    while remaining:
                        n = min(65536,remaining)
                        out.write(b' '*(n-1)+b'\n'); remaining -= n
                    out.write(row)
                with self.subTest(size=size):
                    self.assertEqual(len(history.read_history(folder,0)),1)

    def test_real_owner_worker_and_weekly_report_keep_large_day(self):
        import pwd
        from types import SimpleNamespace
        from wsl_resource_guard.owner_worker import call_owner
        from wsl_resource_guard.reporting import build_weekly_report
        account = pwd.getpwuid(os.getuid())
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory); state=home/'.local/state/wsl-resource-guard'
            state.mkdir(parents=True)
            now=time.time(); base=datetime.fromtimestamp(now).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()-86400
            large_day(state,base)
            owner=SimpleNamespace(pw_uid=account.pw_uid,pw_gid=account.pw_gid,
                                  pw_name=account.pw_name,pw_dir=directory)
            rows=call_owner(owner,'history',{'range':'7d'})
            self.assertTrue(rows)
            self.assertEqual(rows[-1]['timestamp'],base+86390)
            self.assertEqual(len(call_owner(owner,'session-history',{})),5)
            self.assertTrue(call_owner(owner,'attribution',{'range':'7d'})['rows'])
            self.assertEqual(call_owner(owner,'alerts',{})['episodes'],[])
            _, text, _ = build_weekly_report(state,now)
            self.assertIn('project-4',text)

    def test_large_day_preserved_in_raw_buckets_sessions_and_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            base = datetime(2026, 10, 6).timestamp()
            path = large_day(folder, base)
            self.assertGreater(path.stat().st_size, 16*1024*1024)
            self.assertEqual(len(history.read_history(folder, base)),8640)
            buckets = history.read_history_downsampled(folder, base, 300)
            self.assertEqual(len(buckets),288)
            self.assertEqual(buckets[-1]['timestamp'],base+86390)
            self.assertEqual(len(history.session_history(folder,base)),5)
            self.assertEqual(len(history.memory_attribution(folder,base,300)['rows']),288)

    def test_read_error_is_not_reported_as_empty_day(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder/'history-2026-10-06.jsonl'
            os.mkfifo(path)
            for reader in (history.read_history, history.session_history,
                           lambda p,s:history.read_history_downsampled(p,s,300),
                           lambda p,s:history.memory_attribution(p,s,300)):
                with self.subTest(reader=reader), self.assertRaises(OSError):
                    reader(folder,0)

    def test_latest_limit_filters_and_raw_budget_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            records = [dict(timestamp=t, severity='warning' if t%2 else 'normal', metrics={})
                       for t in (3,1,2,6,4,5)]
            (folder/'history-2026-10-06.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
            rows = history.read_history(folder,0,limit=2,severity='warning')
            self.assertEqual([r['timestamp'] for r in rows],[3,5])
            with self.assertRaises(OSError):
                history.read_history(folder,0,max_records=3)
            with self.assertRaises(OSError):
                history.read_history(folder,0,max_bytes=10)

    def test_replacement_with_same_size_and_mtime_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); path=folder/'history-2026-10-06.jsonl'
            path.write_text('{"timestamp": 10, "metrics": {"mem_available_kib": 1}}\n')
            original=path.stat()
            self.assertEqual(history.read_history_downsampled(folder,0,300)[0]['metrics']['mem_available_kib'],1)
            other=folder/'replacement'
            other.write_text('{"timestamp": 10, "metrics": {"mem_available_kib": 2}}\n')
            os.utime(other,ns=(original.st_atime_ns,original.st_mtime_ns)); other.replace(path)
            self.assertEqual(history.read_history_downsampled(folder,0,300)[0]['metrics']['mem_available_kib'],2)

    def test_partial_line_nonobjects_nonfinite_and_appended_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory); path=folder/'history-2026-10-06.jsonl'
            path.write_text('[]\nnull\n{"timestamp": NaN}\n{"timestamp": Infinity}\n{"timestamp": 1}\n{"timestamp":')
            self.assertEqual([r['timestamp'] for r in history.read_history(folder,0)],[1])
            with path.open('a') as handle: handle.write('2}\n')
            self.assertEqual([r['timestamp'] for r in history.read_history(folder,0)],[1,2])


class LineIteratorTests(unittest.TestCase):
    def test_large_line_and_midread_truncation_are_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data'
            path.write_bytes(b'x'*(1024*1024)+b'\n')
            with self.assertRaises(OSError):
                list(safe_read.iter_text_lines(path,deadline=time.monotonic()+2))
            path.write_bytes(b'first\n'+b'x'*100000+b'\n')
            rows=safe_read.iter_text_lines(path,deadline=time.monotonic()+2)
            self.assertEqual(next(rows),'first')
            path.write_bytes(b'')
            with self.assertRaises(OSError): list(rows)

    def test_snapshot_end_bounded_line_and_readable_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data'; path.write_text('abc\ndef\n')
            link=path.with_name('link'); link.symlink_to(path)
            rows=safe_read.iter_text_lines(link,max_line_bytes=4,deadline=time.monotonic()+2)
            self.assertEqual(next(rows),'abc')
            with path.open('a') as out: out.write('later\n')
            self.assertEqual(list(rows),['def'])
            with self.assertRaises(OSError):
                list(safe_read.iter_text_lines(path,max_line_bytes=3,deadline=time.monotonic()+2))

    def test_fifo_device_directory_and_expired_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo=Path(directory)/'fifo'; os.mkfifo(fifo)
            path=Path(directory)/'file'; path.write_text('hello\n')
            for bad in (fifo,Path('/dev/null'),Path(directory)):
                with self.subTest(path=bad), self.assertRaises(OSError):
                    list(safe_read.iter_text_lines(bad,max_line_bytes=100,deadline=time.monotonic()+1))
            with self.assertRaises(TimeoutError):
                list(safe_read.iter_text_lines(path,max_line_bytes=100,deadline=time.monotonic()-1))
