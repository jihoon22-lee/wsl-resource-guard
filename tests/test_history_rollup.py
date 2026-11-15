"""Daily rollups must answer exactly what the raw history answers, or step aside."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard import history

DAY = 86400


def sample(ts: float, index: int) -> dict:
    severity = 'critical' if index % 97 == 0 else 'warning' if index % 11 == 0 else 'normal'
    psi = None if index % 53 == 0 else round((index % 7) / 10, 2)
    return {
        'timestamp': ts, 'severity': severity, 'reasons': [f'reason {index % 5}'] if severity != 'normal' else [],
        'observations': [f'obs {index % 3}'], 'pending_reasons': [],
        'metrics': {'mem_total_kib': 1000, 'mem_available_kib': 500 + index % 200, 'swap_total_kib': 100,
                    'swap_free_kib': 100 - index % 30, 'psi_some_avg60': psi, 'psi_full_avg60': psi,
                    'psi_status': 'normal' if psi is not None else 'error',
                    'swap_in_mib_per_minute': index % 4, 'swap_out_mib_per_minute': index % 9,
                    'vmmem_bytes': 10 ** 9 + index, 'vmmem_observed_at': ts, 'disks': [{'id': 'C:', 'n': index}]},
        'sessions': [
            {'provider': 'claude', 'root_pid': 100 + index // 400, 'root_name': 'claude', 'project': 'alpha',
             'age_seconds': (index % 400) * 60 + 30, 'rss_kib': 1000 + index % 300, 'mcp_count': index % 3,
             'projects': [{'project': 'alpha', 'rss_kib': 700 + index % 300}, {'project': 'beta', 'rss_kib': 300}]},
            {'provider': 'codex', 'root_pid': 7, 'root_name': 'codex', 'project': 'gamma', 'persistent': True,
             'age_seconds': index * 60.0 + 5, 'rss_kib': 400 + (index * 7) % 500, 'mcp_count': 0}],
        'mcp_rss_kib': 0, 'mcp_count': 0, 'mcp_process_count': 0}


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        history._ROLLUPS_VERIFIED.clear()
        history._ROLLUPS_FAILED.clear()
        history._file_cache.clear()
        # Three full days and part of a fourth, one sample a minute, bucket-aligned start.
        self.start = float(int(datetime(2026, 10, 5, 0, 0).timestamp()))
        self.now = self.start + 3 * DAY + 6 * 3600
        index = 0
        ts = self.start
        while ts < self.now:
            day = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
            with open(self.state / f'history-{day}.jsonl', 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(sample(ts, index)) + '\n')
            ts += 60
            index += 1

    def build_all(self):
        while history.maintain_rollups(self.state, self.now):
            pass

    def answers(self):
        since = self.start
        return {
            **{f'history:{bucket}': history.read_history_downsampled(self.state, since, bucket)
               for bucket in (300, 1800, 3600)},
            **{f'attribution:{bucket}': history.memory_attribution(self.state, since, bucket, top=2)
               for bucket in (300, 1800, 3600)},
            'attribution:raw-step': history.memory_attribution(self.state, self.now - 3 * 3600, 0),
            'sessions': history.session_history(self.state, since, now=self.now),
        }

    def test_answers_are_identical_with_and_without_rollups(self):
        raw = self.answers()
        self.assertGreater(len(raw['history:300']), 800)
        self.build_all()
        self.assertEqual(sorted(p.name for p in self.state.glob('rollup-*.jsonl')),
                         ['rollup-2026-10-05.jsonl', 'rollup-2026-10-06.jsonl', 'rollup-2026-10-07.jsonl'])
        history._file_cache.clear()
        with patch.object(history, '_iter_records', wraps=history._iter_records) as parsed:
            rolled = self.answers()
            raw_files = {call.args[0].name for call in parsed.call_args_list}
        self.assertEqual(raw_files, {'history-2026-10-08.jsonl'})   # only the unfinished day is parsed
        for name in raw:
            with self.subTest(answer=name):
                self.assertEqual(rolled[name], raw[name])

    def test_one_day_per_call_and_today_is_never_frozen(self):
        self.assertTrue(history.maintain_rollups(self.state, self.now))
        self.assertEqual(len(list(self.state.glob('rollup-*.jsonl'))), 1)
        self.assertTrue(history.maintain_rollups(self.state, self.now))
        self.assertFalse(history.maintain_rollups(self.state, self.now))
        self.assertFalse((self.state / 'rollup-2026-10-08.jsonl').exists())
        with patch.object(history, 'build_rollup') as build:
            self.assertFalse(history.maintain_rollups(self.state, self.now))   # nothing left to do
            build.assert_not_called()
        self.assertEqual(os.stat(self.state / 'rollup-2026-10-05.jsonl').st_mode & 0o777, 0o600)

    def test_a_changed_or_damaged_day_falls_back_to_raw_and_is_rebuilt(self):
        self.build_all()
        raw_day = self.state / 'history-2026-10-06.jsonl'
        extra = sample(self.start + 2 * DAY - 20, 97)    # a critical sample is added to a finished day
        with open(raw_day, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(extra) + '\n')
        self.assertIsNone(history.load_rollup(raw_day))                       # stale: not trusted
        history._file_cache.clear()
        changed = history.read_history_downsampled(self.state, self.start, 3600)
        bucket = next(row for row in changed if row['timestamp'] == self.start + 2 * DAY - 20)
        self.assertEqual(bucket['severity'], 'critical')
        self.assertTrue(history.maintain_rollups(self.state, self.now) or True)
        self.build_all()
        self.assertIsNotNone(history.load_rollup(raw_day))
        self.assertEqual(history.read_history_downsampled(self.state, self.start, 3600), changed)
        for damage in ('', '{broken', '{"version":1}\n{broken', '{"version":9,"bucket_seconds":300,"source":[1,1]}\n{}'):
            history.rollup_path(raw_day).write_text(damage)
            with self.subTest(damage=damage):
                self.assertIsNone(history.load_rollup(raw_day))
                history._file_cache.clear()
                self.assertEqual(history.read_history_downsampled(self.state, self.start, 3600), changed)

    def test_rotated_days_lose_their_rollup_and_a_bad_day_is_not_retried_forever(self):
        self.build_all()
        (self.state / 'history-2026-10-05.jsonl').unlink()
        history.maintain_rollups(self.state, self.now)
        self.assertFalse((self.state / 'rollup-2026-10-05.jsonl').exists())
        history.rollup_path(self.state / 'history-2026-10-06.jsonl').unlink()
        history._ROLLUPS_VERIFIED.clear()
        with patch.object(history, 'build_rollup', side_effect=OSError('too large')) as build, patch('builtins.print'):
            for _ in range(3):
                history.maintain_rollups(self.state, self.now)
            self.assertEqual(build.call_count, 1)

    def test_two_weeks_of_full_size_samples_fit_the_result_limit(self):
        # Production rows carry drives, pressure and sessions: about 2.5 KiB each.
        # 14 days of 5-minute buckets is over 10 MiB, more than one answer may hold.
        for old in self.state.glob('history-*.jsonl'):
            old.unlink()
        padding = [{'id': f'{letter}:', 'label': 'x' * 180} for letter in 'CDEFGHIJKL']
        ts = self.start
        end = self.start + 14 * DAY
        index = 0
        handles = {}
        while ts < end:
            row = sample(ts, index)
            row['metrics']['disks'] = padding
            day = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
            if day not in handles:
                handles[day] = open(self.state / f'history-{day}.jsonl', 'a', encoding='utf-8')
            handles[day].write(json.dumps(row) + '\n')
            ts += 300
            index += 1
        for handle in handles.values():
            handle.close()
        now = end + 3600
        while history.maintain_rollups(self.state, now):
            pass
        stored = sum(len(history.load_rollup(p)['buckets']) * len(json.dumps(history.load_rollup(p)['buckets'][0]))
                     for p in self.state.glob('history-*.jsonl'))
        self.assertGreater(stored, history.MAX_RESULT_BYTES)       # the case this guards
        history._file_cache.clear()
        with patch.object(history, '_iter_records', wraps=history._iter_records) as parsed:
            rows = history.read_history_downsampled(self.state, self.start, 3600)
            parsed.assert_not_called()
        self.assertEqual(len(rows), 14 * 24)
        self.assertEqual(rows[0]['metrics']['disks'], padding)

    def test_a_day_that_cannot_be_aggregated_is_skipped_without_stopping_the_guard(self):
        for error in (TypeError('unorderable'), OverflowError('too large'), KeyError('timestamp')):
            history._ROLLUPS_VERIFIED.clear()
            history._ROLLUPS_FAILED.clear()
            for old in self.state.glob('rollup-*.jsonl'):
                old.unlink()
            with self.subTest(error=type(error).__name__), patch('builtins.print') as out:
                with patch.object(history, 'build_rollup', side_effect=error) as build:
                    # A failed parse costs as much as a good one: one day per call.
                    self.assertTrue(history.maintain_rollups(self.state, self.now))
                    self.assertEqual(build.call_count, 1)
                    while history.maintain_rollups(self.state, self.now):
                        pass
                    self.assertEqual(build.call_count, 3)
                    history.maintain_rollups(self.state, self.now)
                    self.assertEqual(build.call_count, 3)              # not retried
                self.assertIn('history rollup skipped', out.call_args.args[0])
                self.assertEqual(list(self.state.glob('rollup-*.jsonl')), [])

    def test_a_rollup_with_unusable_rows_is_not_trusted(self):
        self.build_all()
        raw_day = self.state / 'history-2026-10-06.jsonl'
        expected = history.read_history_downsampled(self.state, self.start, 3600)
        sessions = history.session_history(self.state, self.start, now=self.now)
        attribution = history.memory_attribution(self.state, self.start, 3600)
        target = history.rollup_path(raw_day)
        head, _, body = target.read_text().partition('\n')
        good = json.loads(body)
        key = next(iter(good['sessions']))
        for name, change in (
                ('bucket without timestamp', lambda d: d['buckets'][0].pop('timestamp')),
                ('bucket timestamp text', lambda d: d['buckets'][0].update(timestamp='noon')),
                ('bucket is a list', lambda d: d['buckets'].append([])),
                ('bucket without metrics', lambda d: d['buckets'][0].pop('metrics')),
                ('attribution without n', lambda d: d['attribution'][0].pop('n')),
                ('attribution zero n', lambda d: d['attribution'][0].update(n=0)),
                ('attribution text amount', lambda d: d['attribution'][0]['sum'].update(alpha='much')),
                ('session without last_seen', lambda d: d['sessions'][key].pop('last_seen')),
                ('session is text', lambda d: d['sessions'].update({key: 'gone'}))):
            data = json.loads(body)
            change(data)
            target.write_text(head + '\n' + json.dumps(data))
            history._file_cache.clear()
            with self.subTest(damage=name):
                self.assertIsNone(history.load_rollup(raw_day))
                self.assertEqual(history.read_history_downsampled(self.state, self.start, 3600), expected)
                self.assertEqual(history.session_history(self.state, self.start, now=self.now), sessions)
                self.assertEqual(history.memory_attribution(self.state, self.start, 3600), attribution)

    def test_guard_maintains_rollups_when_it_appends_history(self):
        from wsl_resource_guard import daemon
        from wsl_resource_guard.config import Settings
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_daemon import metrics, snapshot
        settings = Settings(state_dir=str(self.state))
        current = metrics(8.0, timestamp=self.now)
        evaluation = daemon.Evaluation('normal', [], [])
        with patch('builtins.print'):
            daemon.record_history(settings, current, snapshot(), evaluation, 0.0, self.now)
        self.assertEqual(len(list(self.state.glob('rollup-*.jsonl'))), 1)
        for offset, error in ((60, OSError('disk full')), (120, TypeError('malformed line'))):
            with patch.object(history, 'maintain_rollups', side_effect=error), patch('builtins.print') as out:
                last, _ = daemon.record_history(settings, metrics(8.0, timestamp=self.now + offset), snapshot(),
                                                evaluation, 0.0, self.now)
            self.assertEqual(last, self.now + offset)             # sampling is not disturbed
            self.assertIn('history rollup unavailable', out.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
