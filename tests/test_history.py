import json
from datetime import datetime
from pathlib import Path
import tempfile
import time
import unittest

from wsl_resource_guard import history
from wsl_resource_guard.history import (alert_episodes, downsample,
                                        downsample_service_memory,
                                        read_history, reason_label,
                                        reason_summary_7d)


def record(ts, severity='normal', reasons=(), mem_free=1000,
           psi=0.0, swap_used=0, vmmem=None, observed=None):
    metrics = {'mem_available_kib': mem_free, 'swap_total_kib': swap_used + 100,
               'swap_free_kib': 100, 'psi_some_avg60': psi, 'psi_full_avg60': 0.0}
    if vmmem is not None:
        metrics['vmmem_bytes'] = vmmem
    if observed is not None:
        metrics['vmmem_observed_at'] = observed
    return {'timestamp': ts, 'severity': severity, 'reasons': list(reasons),
            'metrics': metrics}


def write_history(state: Path, day: str, records: list) -> None:
    with (state / f'history-{day}.jsonl').open('w') as handle:
        for rec in records:
            handle.write(json.dumps(rec) + '\n')


class ReadHistoryTests(unittest.TestCase):
    def test_reads_records_since_cutoff_across_day_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            write_history(state, '2026-09-26', [record(1000), record(2000)])
            write_history(state, '2026-09-27', [record(5000), record(9000)])
            rows = read_history(state, since=4500)
            self.assertEqual([r['timestamp'] for r in rows], [5000, 9000])

    def test_skips_files_older_than_since_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            write_history(state, '2026-09-20', [record(1)])
            write_history(state, '2026-09-27', [record(9000)])
            rows = read_history(state, since=8000)
            self.assertEqual([r['timestamp'] for r in rows], [9000])

    def test_corrupt_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with (state / 'history-2026-09-27.jsonl').open('w') as handle:
                handle.write('not json\n' + json.dumps(record(9000)) + '\n')
            self.assertEqual(len(read_history(state, 0)), 1)


    def test_raw_and_downsampled_rows_share_one_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            extra = dict(record(9000), observations=['x'], pending_reasons=['p'],
                         mcp_rss_kib=1, mcp_count=1, mcp_process_count=1)
            write_history(state, '2026-09-27', [extra, record(9100)])
            raw = read_history(state, since=0)
            bucketed = downsample(raw, 300)
            self.assertEqual(set(raw[0]), set(bucketed[0]))
            self.assertNotIn('mcp_rss_kib', raw[0])


class DownsampleTests(unittest.TestCase):
    def test_keeps_worst_case_per_bucket(self):
        # One 300 s bucket: lowest free RAM, highest PSI, worst severity win.
        rows = [record(0, mem_free=900, psi=1.0),
                record(60, severity='critical', reasons=['ram'], mem_free=100, psi=9.0),
                record(120, mem_free=500, psi=3.0)]
        merged = downsample(rows, 300)
        self.assertEqual(len(merged), 1)
        out = merged[0]
        self.assertEqual(out['metrics']['mem_available_kib'], 100)
        self.assertEqual(out['metrics']['psi_some_avg60'], 9.0)
        self.assertEqual(out['severity'], 'critical')
        self.assertEqual(out['reasons'], ['ram'])
        self.assertEqual(out['timestamp'], 120)

    def test_vmmem_keeps_latest_not_max(self):
        rows = [record(0, vmmem=10, observed=0), record(60, vmmem=5, observed=60)]
        merged = downsample(rows, 300)
        self.assertEqual(merged[0]['metrics']['vmmem_bytes'], 5)
        self.assertEqual(merged[0]['metrics']['vmmem_observed_at'], 60)

    def test_buckets_stay_separate_and_raw_passthrough(self):
        rows = [record(0), record(299), record(300)]
        self.assertEqual(len(downsample(rows, 300)), 2)
        self.assertEqual(downsample(rows, 0), rows)


class ServiceMemoryDownsampleTests(unittest.TestCase):
    def test_per_service_max_and_missing_stays_missing(self):
        rows = [{'timestamp': 0, 'services': {'a': 100, 'b': 50}},
                {'timestamp': 60, 'services': {'a': 40}},
                {'timestamp': 300, 'services': {'a': 200}}]
        merged = downsample_service_memory(rows, 300)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]['services'], {'a': 100, 'b': 50})
        # 'b' absent in both raw samples of the bucket must not appear.
        self.assertEqual(merged[1]['services'], {'a': 200})


class AlertEpisodeTests(unittest.TestCase):
    def test_normal_sample_closes_episode(self):
        rows = [record(0, 'warning', ['psi']), record(60, 'warning', ['psi']),
                record(120), record(180, 'critical', ['ram'])]
        episodes = alert_episodes(rows, now=180 + 600)
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0]['duration_seconds'], 60)
        self.assertFalse(episodes[0]['ongoing'])
        self.assertTrue(episodes[1]['ongoing'])
        self.assertEqual(episodes[1]['worst_severity'], 'critical')

    def test_gap_closes_episode(self):
        rows = [record(0, 'warning', ['psi']), record(6000)]
        episodes = alert_episodes(rows, gap_seconds=1800)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]['end'], 0)
        self.assertFalse(episodes[0]['ongoing'])

    def test_reasons_ranked_by_frequency(self):
        rows = [record(0, 'warning', ['a', 'b']), record(60, 'warning', ['a']),
                record(120, 'warning', ['a'])]
        episodes = alert_episodes(rows)
        self.assertEqual(episodes[0]['reasons_top5'][0], 'a')
        self.assertEqual(episodes[0]['reasons_top5'], ['a', 'b'])

    def test_reason_summary_counts_and_keeps_last_raw(self):
        rows = [record(0, 'warning', ['a', 'b']), record(60, 'warning', ['a']),
                record(120, 'warning', ['a'])]
        summary = alert_episodes(rows)[0]['reason_summary']
        self.assertEqual(summary, [
            {'label': 'a', 'samples': 3, 'last': 'a'},
            {'label': 'b', 'samples': 1, 'last': 'b'}])

    def test_stale_trailing_episode_is_not_ongoing(self):
        rows = [record(1000, 'warning', ['psi']), record(1060, 'warning', ['psi'])]
        # A daemon stop must not leave the last episode 'in progress': its end
        # is older than now - gap, so it closes instead of stretching forever.
        episodes = alert_episodes(rows, gap_seconds=1800, now=1060 + 3600)
        self.assertFalse(episodes[0]['ongoing'])
        self.assertEqual(episodes[0]['end'], 1060)
        # Inside the gap the episode still counts as ongoing.
        episodes = alert_episodes(rows, gap_seconds=1800, now=1060 + 600)
        self.assertTrue(episodes[0]['ongoing'])

    def test_default_now_uses_the_current_clock(self):
        fresh = alert_episodes([record(time.time(), 'warning', ['psi'])])
        self.assertTrue(fresh[0]['ongoing'])
        stale = alert_episodes(
            [record(time.time() - 4000, 'warning', ['psi'])])
        self.assertFalse(stale[0]['ongoing'])

    def test_reason_labels_dedupe_numeric_variants(self):
        rows = [
            record(0, 'warning', ['E: 디스크 여유 18.7% (174 GiB / 931 GiB)']),
            record(60, 'warning', ['E: 디스크 여유 18.8% (173 GiB / 931 GiB)',
                                   '가용 RAM 1.5 GiB']),
        ]
        episode = alert_episodes(rows)[0]
        self.assertEqual(episode['reasons_top5'], ['E: 디스크 여유', '가용 RAM'])
        summary = {s['label']: s for s in episode['reason_summary']}
        self.assertEqual(summary['E: 디스크 여유']['samples'], 2)
        self.assertEqual(summary['E: 디스크 여유']['last'],
                         'E: 디스크 여유 18.8% (173 GiB / 931 GiB)')
        self.assertEqual(summary['가용 RAM']['samples'], 1)


class ReasonSummary7dTests(unittest.TestCase):
    def test_counts_each_label_once_per_episode_and_sums_duration(self):
        episodes = alert_episodes([
            record(1000, 'warning', ['E: 디스크 여유 18.7% (174 GiB / 931 GiB)']),
            record(1060, 'warning', ['E: 디스크 여유 18.8% (173 GiB / 931 GiB)',
                                     '가용 RAM 1.5 GiB']),
            record(5000, 'critical', ['가용 RAM 0.9 GiB']),
        ])
        self.assertEqual(len(episodes), 2)
        rollup = {r['label']: r for r in reason_summary_7d(episodes, now=6000)}
        # 디스크 recurred inside one episode: one count, the full duration.
        self.assertEqual(rollup['E: 디스크 여유'],
                         {'label': 'E: 디스크 여유', 'episodes': 1,
                          'total_seconds': 60})
        # RAM appeared in both episodes: two counts, both durations.
        self.assertEqual(rollup['가용 RAM'],
                         {'label': '가용 RAM', 'episodes': 2,
                          'total_seconds': 60})

    def test_excludes_episodes_started_before_the_7_day_cutoff(self):
        episodes = alert_episodes(
            [record(100, 'warning', ['old cause']), record(160, 'warning', ['old cause'])])
        self.assertEqual(reason_summary_7d(episodes, now=100 + 8 * 86400), [])

    def test_orders_by_episode_count_then_total_seconds(self):
        episodes = [
            {'start': 5000, 'duration_seconds': 0,
             'reason_summary': [{'label': 'b', 'samples': 1, 'last': 'b'},
                                {'label': 'a', 'samples': 1, 'last': 'a'}]},
            {'start': 5100, 'duration_seconds': 10,
             'reason_summary': [{'label': 'a', 'samples': 1, 'last': 'a'}]},
        ]
        rollup = reason_summary_7d(episodes, now=6000)
        self.assertEqual([r['label'] for r in rollup], ['a', 'b'])


class ReasonLabelTests(unittest.TestCase):
    def test_normalizes_daemon_reason_strings(self):
        cases = {
            'E: 디스크 여유 18.7% (174 GiB / 931 GiB)': 'E: 디스크 여유',
            'E: 디스크 여유 8.0% (74 GiB / 931 GiB) (마지막 확인값 · 현재 조회 불가)':
                'E: 디스크 여유',
            '가용 RAM 1.5 GiB': '가용 RAM',
            '메모리 완전 정체 PSI 25.00%': '메모리 완전 정체 PSI',
            '메모리 압력 PSI 3.20%': '메모리 압력 PSI',
            '가용 RAM 1.5 GiB에서 완전 정체 PSI 3%': '가용 RAM에서 완전 정체 PSI',
            '가용 RAM 1.5 GiB, swap-out 300 MiB/min': '가용 RAM, swap-out',
            'swap-out 512 MiB/min': 'swap-out',
            'OOM kill 2건 발생 (희생 프로세스: node, python)': 'OOM kill 발생',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(reason_label(raw), expected)

    def test_plain_labels_pass_through(self):
        self.assertEqual(reason_label('a'), 'a')
        self.assertEqual(reason_label(''), '')


if __name__ == '__main__':
    unittest.main()


class SessionScanTests(unittest.TestCase):
    def write_day(self, folder, day, records):
        (folder / f'history-{day}.jsonl').write_text(
            ''.join(json.dumps(r) + '\n' for r in records))

    def session(self, pid, project, rss, age, provider='claude'):
        return {'root_pid': pid, 'provider': provider, 'root_name': provider, 'project': project,
                'age_seconds': age, 'rss_kib': rss, 'mcp_count': 1,
                'projects': [{'project': project, 'rss_kib': rss}]}

    def test_attribution_stacks_top_projects_and_folds_the_rest(self):
        base = datetime(2026, 10, 3, 12, 0).timestamp()
        records = [{'timestamp': base + i * 60, 'sessions': [
            self.session(1, 'big', 4 * 2**20, 3600 + i * 60),
            self.session(2, 'small', 2**20, 600 + i * 60),
            self.session(3, 'tiny', 1024, 60 + i * 60)]} for i in range(10)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.write_day(folder, '2026-10-03', records)
            result = history.memory_attribution(folder, base, 300, top=2)
        self.assertEqual(result['keys'], ['big', 'small', '기타'])
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual(result['rows'][0]['projects']['big'], 4 * 2**20)
        self.assertEqual(result['rows'][0]['projects']['기타'], 1024)

    def test_session_history_tracks_peaks_and_pid_reuse(self):
        base = datetime(2026, 10, 3, 12, 0).timestamp()
        records = [
            {'timestamp': base, 'sessions': [self.session(7, 'a', 100, 1000)]},
            {'timestamp': base + 60, 'sessions': [self.session(7, 'a', 900, 1060)]},
            # Same PID, new start time: a different session.
            {'timestamp': base + 7200, 'sessions': [self.session(7, 'b', 50, 30)]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.write_day(folder, '2026-10-03', records)
            rows = history.session_history(folder, base - 10, now=base + 7210)
        self.assertEqual([r['project'] for r in rows], ['b', 'a'])
        self.assertFalse(rows[0]['ended'])
        self.assertTrue(rows[1]['ended'])
        self.assertEqual(rows[1]['peak_rss_kib'], 900)
        self.assertAlmostEqual(rows[1]['duration_seconds'], 1060)

    def test_cached_downsample_matches_direct_and_sees_new_lines(self):
        base = datetime(2026, 10, 3, 12, 0).timestamp()
        metrics = {'mem_available_kib': 5, 'swap_free_kib': 1, 'swap_total_kib': 2}
        records = [{'timestamp': base + i * 60, 'severity': 'normal', 'metrics': metrics}
                   for i in range(30)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.write_day(folder, '2026-10-03', records)
            first = history.read_history_downsampled(folder, base, 300)
            self.assertEqual(first, history.downsample(history.read_history(folder, base), 300))
            records.append({'timestamp': base + 3600, 'severity': 'warning', 'metrics': metrics})
            self.write_day(folder, '2026-10-03', records)
            self.assertEqual(history.read_history_downsampled(folder, base, 300)[-1]['severity'],
                             'warning')
