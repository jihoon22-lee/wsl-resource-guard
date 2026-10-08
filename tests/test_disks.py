from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wsl_resource_guard import disks
from wsl_resource_guard.config import Settings
from wsl_resource_guard.daemon import evaluate, sample, append_disk_history
from wsl_resource_guard.notifications import NotificationResult
from test_daemon import metrics, snapshot


def setUpModule() -> None:
    # sample() must never start the real powershell.exe from a unit test.
    patcher = patch("wsl_resource_guard.metrics.POWERSHELL", Path("/nonexistent/powershell.exe"))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def disk(free=25, identifier='C:', kind='windows', status='ok'):
    total = 100 * 2**30
    return {'id':identifier,'name':identifier,'kind':kind,'mount':'/mnt/c','status':status,
            'error':'read failed' if status!='ok' else '', 'observed_at':100,'checked_at':100,
            'total_bytes':total,'available_bytes':free*2**30,'used_bytes':total-free*2**30,
            'reserved_bytes':0,'available_percent':free,'used_percent':100-free}


class DiskCollectionTests(unittest.TestCase):
    def setUp(self):
        disks._CACHE.clear()

    def test_unmounted_drive_does_not_report_root_capacity(self):
        with patch.object(disks,'mount_table',return_value={'/':('ext4','/dev/sdd')}):
            with patch.object(disks.os,'statvfs') as stat:
                stat.return_value=SimpleNamespace(f_blocks=100,f_bfree=40,f_bavail=35,f_frsize=4096)
                rows=disks.collect_disks(['C'])
        self.assertEqual(rows[0]['status'],'unmounted')
        self.assertIsNone(rows[0]['total_bytes'])
        stat.assert_called_once_with('/')

    def test_reserved_space_separate_from_user_available(self):
        with patch.object(disks,'mount_table',return_value={'/':('ext4','/dev/sdd')}):
            with patch.object(disks.os,'statvfs',return_value=SimpleNamespace(f_blocks=100,f_bfree=40,f_bavail=35,f_frsize=4096)):
                row=disks.collect_disks([])[0]
        self.assertEqual(row['used_bytes'],60*4096)
        self.assertEqual(row['reserved_bytes'],5*4096)
        self.assertEqual(row['available_percent'],35)

    def test_stale_measurement_preserved_across_restart(self):
        previous=[disk(9)]
        with patch.object(disks,'mount_table',return_value={}),patch.object(disks.os,'statvfs',side_effect=OSError('unavailable')):
            row=disks.read_disks(['C'],previous=previous)[0]
        self.assertEqual(row['status'],'unmounted')
        self.assertEqual(row['available_percent'],9)
        self.assertEqual(row['observed_at'],100)
        self.assertEqual(disks.disk_severity(row),'critical')

    def test_cache_and_manual_refresh_rate_limit(self):
        with patch.object(disks,'collect_disks',return_value=[disk()]) as collect:
            with patch.object(disks.time,'monotonic',side_effect=[100,101,106,160]):
                first=disks.read_disks(['C'])
                first[0]['available_bytes']=0
                second=disks.read_disks(['C'],force=True)
                self.assertNotEqual(second[0]['available_bytes'],0)
                disks.read_disks(['C'],force=True)
                disks.read_disks(['C'])
        self.assertEqual(collect.call_count,2)

    def test_mount_source_must_match_drive(self):
        with patch.object(disks,'mount_table',return_value={'/mnt/c':('9p','D:\\')}):
            with patch.object(disks.os,'statvfs',return_value=SimpleNamespace(f_blocks=100,f_bfree=40,f_bavail=40,f_frsize=4096)):
                row=disks.collect_disks(['C'])[0]
        self.assertEqual(row['status'],'unmounted')

    def test_mountinfo_decodes_escapes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'mountinfo'
            path.write_text('1 2 0:3 / /mnt/c rw - 9p C:\\134 rw\n')
            self.assertEqual(disks.mount_table(path)['/mnt/c'],('9p','C:\\'))


class DiskInsightTests(unittest.TestCase):
    def test_daily_records_take_last_line_of_each_day(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder/'disk-history-2026-09-13.jsonl').write_text(
                json.dumps({'timestamp':1,'disks':[{'id':'C:','available_bytes':90*2**30}]})+'\n'
                +json.dumps({'timestamp':2,'disks':[{'id':'C:','available_bytes':89*2**30}]})+'\n')
            (folder/'disk-history-2026-09-14.jsonl').write_text('broken\n'
                +json.dumps({'timestamp':3,'disks':[{'id':'C:','available_bytes':85*2**30}]})+'\n')
            records = disks.daily_disk_records(folder, 14)
        self.assertEqual([r['date'] for r in records], ['2026-09-13','2026-09-14'])
        # 날짜별 마지막 유효 레코드만 사용
        self.assertEqual(records[0]['disks'][0]['available_bytes'], 89*2**30)
        self.assertEqual(records[1]['disks'][0]['available_bytes'], 85*2**30)

    def test_trend_and_warning_projection(self):
        history = [
            {'date':'2026-09-10','disks':[{'id':'C:','available_bytes':50*2**30}]},
            {'date':'2026-09-13','disks':[{'id':'C:','available_bytes':41*2**30}]},
        ]
        info = disks.disk_insights([disk(41)], history, 20, 10)['C:']
        self.assertAlmostEqual(info['gib_per_day'], 3.0)
        self.assertAlmostEqual(info['daily_delta_gib'], -9.0, places=1)
        # 여유 41 GiB → 20 GiB(20%)까지 21 GiB / 3 GiB·일 = 7일
        self.assertAlmostEqual(info['days_to_warning'], 7.0)
        self.assertAlmostEqual(info['days_to_critical'], 31/3, places=3)

    def test_one_time_copy_is_not_a_lasting_burn_rate(self):
        # 2026-10-02 E: one 200 GiB copy on day 3, flat before and after. The
        # old first-to-last slope claimed ~16 GiB/day and "위험까지 2일".
        values = [320, 320, 120, 119.8, 119.8, 119.9, 119.8, 119.8]
        history = [{'date': f'2026-09-{20 + i:02d}',
                    'disks': [{'id': 'C:', 'available_bytes': v * 2**30}]}
                   for i, v in enumerate(values)]
        info = disks.disk_insights([disk(12.8)], history, 20, 10)['C:']
        self.assertLess(abs(info['gib_per_day']), 0.1)
        self.assertNotIn('days_to_critical', info)

    def test_negligible_burn_or_distant_threshold_has_no_forecast(self):
        # 2026-10-03 D: a ~0.0001 GiB/day median produced "경고까지 약 454931일".
        values = [315.30, 315.30, 315.29, 315.29, 315.29, 315.28]
        history = [{'date': f'2026-09-{20 + i:02d}',
                    'disks': [{'id': 'C:', 'available_bytes': v * 2**30}]}
                   for i, v in enumerate(values)]
        big = disk(33.8)
        big['total_bytes'] = 931.5 * 2**30
        big['available_bytes'] = 315.28 * 2**30
        info = disks.disk_insights([big], history, 10, 5).get('C:', {})
        self.assertNotIn('days_to_warning', info)
        # 0.1 GiB/day with 200 GiB headroom: real, but over a year away.
        history = [{'date': f'2026-09-{20 + i:02d}',
                    'disks': [{'id': 'C:', 'available_bytes': (300 - 0.1 * i) * 2**30}]}
                   for i in range(6)]
        big['available_bytes'] = 299.5 * 2**30
        info = disks.disk_insights([big], history, 10, 5)['C:']
        self.assertAlmostEqual(info['gib_per_day'], 0.1)
        self.assertNotIn('days_to_warning', info)

    def test_steady_burn_keeps_its_rate_in_the_median(self):
        history = [{'date': f'2026-09-{20 + i:02d}',
                    'disks': [{'id': 'C:', 'available_bytes': (60 - 2 * i) * 2**30}]}
                   for i in range(8)]
        info = disks.disk_insights([disk(46)], history, 20, 10)['C:']
        self.assertAlmostEqual(info['gib_per_day'], 2.0)

    def test_hourly_burst_that_stopped_has_no_forecast(self):
        now = __import__('time').time()
        row = disk(41)
        values = [100, 60, 60, 60, 60, 60]
        recent = [{'timestamp': now - (5 - i) * 3600,
                   'disks': [{'id': 'C:', 'available_bytes': v * 2**30}]}
                  for i, v in enumerate(values)]
        info = disks.disk_insights([row], [], 20, 10, recent=recent).get('C:', {})
        self.assertNotIn('recent_gib_per_hour', info)

    def test_shrinking_rate_required_and_below_threshold_has_no_eta(self):
        # 여유가 늘고 있으면 예측 없음
        history = [
            {'date':'2026-09-10','disks':[{'id':'C:','available_bytes':20*2**30}]},
            {'date':'2026-09-12','disks':[{'id':'C:','available_bytes':30*2**30}]},
        ]
        info = disks.disk_insights([disk(30)], history, 20, 10)['C:']
        self.assertLess(info['gib_per_day'], 0)
        self.assertNotIn('days_to_warning', info)
        # 이미 warning 이하면 warning 예측 없이 critical 예측만 남는다
        history[1]['disks'][0]['available_bytes'] = 15*2**30
        info = disks.disk_insights([disk(15)], history, 20, 10)['C:']
        self.assertNotIn('days_to_warning', info)
        self.assertAlmostEqual(info['days_to_critical'], 2.0)

    def test_vhd_reclaim_suggestion_thresholds(self):
        row = disk(50, 'wsl', 'wsl')
        row['vhd'] = {'path':'/mnt/d/x.vhdx','file_bytes':80*2**30,'host_drive':'D:','status':'ok'}
        info = disks.disk_insights([row], [], 20, 10)['wsl']
        self.assertAlmostEqual(info['vhd_reclaim_gib'], 30.0)
        # 차이가 작거나 비율이 낮으면 제안 없음
        row['vhd']['file_bytes'] = 55*2**30
        self.assertNotIn('wsl', disks.disk_insights([row], [], 20, 10))
        row['vhd']['file_bytes'] = 52*2**30  # 2 GiB — 5 GiB 미만
        self.assertNotIn('wsl', disks.disk_insights([row], [], 20, 10))

    def test_recent_records_filter_by_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            now = __import__('time').time()
            today = datetime.fromtimestamp(now).strftime('%Y-%m-%d')
            lines = ''.join(
                json.dumps({'timestamp': now - h * 3600,
                            'disks': [{'id': 'E:', 'available_bytes': 100 * 2**30}]}) + '\n'
                for h in (8, 4, 1))
            (folder / f'disk-history-{today}.jsonl').write_text(lines)
            records = disks.recent_disk_records(folder, hours=6)
        self.assertEqual([len(r['disks']) for r in records], [1, 1])
        self.assertTrue(all(r['timestamp'] >= now - 6 * 3600 for r in records))

    def test_hourly_burn_forecast(self):
        # E: today — 5.2 GiB/h consumption, ~40 GiB headroom above critical.
        now = __import__('time').time()
        row = disk(15.4, 'E:')
        row['total_bytes'] = 932 * 2**30
        row['available_bytes'] = 132.9 * 2**30
        row['available_percent'] = 132.9 / 932 * 100
        recent = [
            {'timestamp': now - 7 * 3600, 'disks': [{'id': 'E:', 'available_bytes': 174.6 * 2**30}]},
            {'timestamp': now, 'disks': [{'id': 'E:', 'available_bytes': 132.9 * 2**30}]},
        ]
        info = disks.disk_insights([row], [], 20, 10, recent=recent)['E:']
        self.assertAlmostEqual(info['recent_gib_per_hour'], 41.7 / 7, places=1)
        # 위험(10% = 93.2 GiB)까지 남은 39.7 GiB / 5.96 GiB·시간
        self.assertAlmostEqual(info['hours_to_critical'], 39.7 / (41.7 / 7), places=1)
        self.assertNotIn('hours_to_warning', info)  # 이미 warning 이하

    def test_hourly_forecast_needs_rate_and_coverage(self):
        now = __import__('time').time()
        row = disk(41)
        # 느린 소모(0.1 GiB/h)는 예측하지 않는다
        slow = [{'timestamp': now - 3 * 3600, 'disks': [{'id': 'C:', 'available_bytes': 41.3 * 2**30}]},
                {'timestamp': now, 'disks': [{'id': 'C:', 'available_bytes': 41 * 2**30}]}]
        info = disks.disk_insights([row], [], 20, 10, recent=slow).get('C:', {})
        self.assertNotIn('recent_gib_per_hour', info)
        # 관측 구간이 2시간 미만이면 예측하지 않는다
        short = [{'timestamp': now - 3600, 'disks': [{'id': 'C:', 'available_bytes': 42 * 2**30}]},
                 {'timestamp': now, 'disks': [{'id': 'C:', 'available_bytes': 41 * 2**30}]}]
        info = disks.disk_insights([row], [], 20, 10, recent=short).get('C:', {})
        self.assertNotIn('recent_gib_per_hour', info)
        # WSL 내부 행은 경보 대상이 아니라 예측하지 않는다
        wsl_row = disk(41, 'wsl', 'wsl')
        wsl_recent = [{'timestamp': now - 3 * 3600, 'disks': [{'id': 'wsl', 'available_bytes': 80 * 2**30}]},
                      {'timestamp': now, 'disks': [{'id': 'wsl', 'available_bytes': 50 * 2**30}]}]
        info = disks.disk_insights([wsl_row], [], 20, 10, recent=wsl_recent).get('wsl', {})
        self.assertNotIn('recent_gib_per_hour', info)

    def test_wsl_row_keeps_trend_but_no_threshold_forecast(self):
        # F10: the WSL internal volume is not an alert target, so no days_to_*.
        wsl_row = disk(41, 'wsl', 'wsl')
        history = [
            {'date': '2026-09-25', 'disks': [{'id': 'wsl', 'available_bytes': 80 * 2**30}]},
            {'date': '2026-09-27', 'disks': [{'id': 'wsl', 'available_bytes': 60 * 2**30}]},
        ]
        info = disks.disk_insights([wsl_row], history, 20, 10)['wsl']
        self.assertIn('gib_per_day', info)
        self.assertNotIn('days_to_warning', info)
        self.assertNotIn('days_to_critical', info)
        # Windows drives still get the daily forecast.
        win_row = disk(41)
        history[0]['disks'].append({'id': 'C:', 'available_bytes': 60 * 2**30})
        history[1]['disks'].append({'id': 'C:', 'available_bytes': 50 * 2**30})
        info = disks.disk_insights([win_row], history, 20, 10)['C:']
        self.assertIn('days_to_critical', info)


class DiskAlertTests(unittest.TestCase):
    def test_strict_thresholds_and_all_drives(self):
        for drive in ('C:','D:','E:'):
            for free,expected in [(20,'normal'),(19.999,'warning'),(10,'warning'),(9.999,'critical'),(0,'critical')]:
                with self.subTest(drive=drive,free=free):
                    m=metrics(8);m.disks=[disk(free,drive)]
                    result=evaluate(m,snapshot(),Settings())
                    self.assertEqual(result.severity,expected)
                    if expected!='normal':self.assertIn(drive,result.reasons[0])

    def test_wsl_internal_usage_is_display_only(self):
        m=metrics(8);m.disks=[disk(1,'wsl','wsl')]
        self.assertEqual(evaluate(m,snapshot(),Settings()).severity,'normal')

    def test_worst_severity_wins_and_preserves_other_reasons(self):
        m=metrics(1);m.disks=[disk(15)]
        result=evaluate(m,snapshot(),Settings())
        self.assertEqual(result.severity,'critical')
        self.assertTrue(any('RAM' in reason for reason in result.reasons))
        self.assertTrue(any('C:' in reason for reason in result.reasons))

    def test_unknown_capacity_is_not_zero_free(self):
        m=metrics(8);d=disk();d.update(status='error',total_bytes=None,available_bytes=None)
        m.disks=[d]
        result=evaluate(m,snapshot(),Settings())
        self.assertEqual(result.severity,'normal')
        self.assertTrue(result.observations)

    def test_stale_critical_does_not_report_false_recovery(self):
        m=metrics(8);m.disks=[disk(9,status='error')]
        result=evaluate(m,snapshot(),Settings())
        self.assertEqual(result.severity,'critical')
        self.assertIn('마지막 확인값',result.reasons[0])

    def test_real_alert_pipeline_reminders_escalation_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory)
            with patch('wsl_resource_guard.daemon.read_system_metrics') as read_metrics,patch('wsl_resource_guard.daemon.read_disks') as read_disk:
                with patch('wsl_resource_guard.daemon.build_snapshot',return_value=snapshot()),patch('wsl_resource_guard.daemon.Notifier') as notifier:
                    notifier.return_value.send.return_value=[NotificationResult('gmail',True,'test transport')]
                    def run(t,free,expected,count):
                        read_metrics.return_value=metrics(8,timestamp=t)
                        read_disk.return_value=[disk(free)]
                        self.assertEqual(sample(settings,notify=True)[2].severity,expected)
                        self.assertEqual(notifier.return_value.send.call_count,count)
                    run(100,15,'warning',1)
                    run(115,14,'warning',1)
                    run(120,9,'critical',2)
                    run(130,25,'critical',2)
                    for timestamp in range(145,249,15):
                        run(timestamp,25,'critical',2)
                    run(249,25,'critical',2)
                    run(250,25,'normal',3)
                    self.assertEqual(notifier.return_value.send.call_args.args[2],'recovery')
                    text,html=notifier.return_value.send.call_args.args[1],notifier.return_value.send.call_args.kwargs['html_message']
                    self.assertIn('디스크',text)
                    self.assertIn('디스크',html)

    def test_new_drive_alerts_during_existing_critical(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory)
            with patch('wsl_resource_guard.daemon.read_system_metrics') as read_metrics,patch('wsl_resource_guard.daemon.read_disks') as read_disk:
                with patch('wsl_resource_guard.daemon.build_snapshot',return_value=snapshot()),patch('wsl_resource_guard.daemon.Notifier') as notifier:
                    notifier.return_value.send.return_value=[]
                    read_metrics.return_value=metrics(8,timestamp=100);read_disk.return_value=[disk(9)]
                    sample(settings,notify=True)
                    read_metrics.return_value=metrics(8,timestamp=105);read_disk.return_value=[disk(8),disk(9,'D:')]
                    sample(settings,notify=True)
                    self.assertEqual(notifier.return_value.send.call_count,2)
                    read_metrics.return_value=metrics(8,timestamp=110)
                    sample(settings,notify=True)
                    self.assertEqual(notifier.return_value.send.call_count,2)

    def test_history_is_private_and_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            (path/'disk-history-2000-01-01.jsonl').write_text('old')
            (path/'history-2000-01-01.jsonl').write_text('memory history')
            m=metrics(8,timestamp=datetime(2026,9,8,10).astimezone().timestamp());m.disks=[disk(15)]
            append_disk_history(path,m,14)
            file=path/'disk-history-2026-09-08.jsonl'
            self.assertEqual(json.loads(file.read_text())['disks'][0]['available_percent'],15)
            self.assertEqual(file.stat().st_mode & 0o777,0o600)
            self.assertFalse((path/'disk-history-2000-01-01.jsonl').exists())
            self.assertTrue((path/'history-2000-01-01.jsonl').exists())

    def test_threshold_oscillation_does_not_spam_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory)
            with patch('wsl_resource_guard.daemon.read_system_metrics') as read_metrics,patch('wsl_resource_guard.daemon.read_disks') as read_disk:
                with patch('wsl_resource_guard.daemon.build_snapshot',return_value=snapshot()),patch('wsl_resource_guard.daemon.Notifier') as notifier:
                    notifier.return_value.send.return_value=[]
                    observations = [(100,19,1),(110,21,1),(120,19,1),(140,21,1)]
                    observations += [(t,21,1) for t in range(155,260,15)]
                    observations += [(260,21,2),(270,19,3)]
                    for timestamp,free,count in observations:
                        read_metrics.return_value=metrics(8,timestamp=timestamp)
                        read_disk.return_value=[disk(free)]
                        sample(settings,notify=True)
                        self.assertEqual(notifier.return_value.send.call_count,count)
