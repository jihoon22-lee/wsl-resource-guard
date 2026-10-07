"""Heartbeat slots follow elapsed scheduled time, including DST and restarts."""
from datetime import datetime
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from wsl_resource_guard import daemon
from wsl_resource_guard.config import Settings
from wsl_resource_guard.notifications import NotificationResult
from test_daemon import metrics,snapshot


class HeartbeatSlotTests(unittest.TestCase):
    def test_cli_distinguishes_failed_attempt_from_success(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from wsl_resource_guard import cli
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=[])
            daemon.save_state(settings.state_path/'state.json',{'last_email_status':100})
            out=io.StringIO()
            with (patch.object(cli,'_load_settings',return_value=settings),
                  patch.object(cli,'sample',return_value=(metrics(12),snapshot(),daemon.Evaluation('normal',[],[]))),
                  patch.object(cli,'Notifier') as notifier,redirect_stdout(out)):
                notifier.return_value.channel_status.return_value={}
                cli.cmd_status(SimpleNamespace(config=None))
            self.assertIn('last email attempt',out.getvalue())
            self.assertIn('last email sent    확인된 성공 기록 없음',out.getvalue())

    def test_future_slot_warns_and_is_replaced_without_claiming_skipped_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=[],email_heartbeat_enabled=True)
            now=datetime(2026,10,6,1,1).timestamp()
            daemon.save_state(settings.state_path/'state.json',{'last_email_hour_slot':f'epoch:{int(now+86400)}'})
            with (patch.object(daemon,'read_system_metrics',return_value=metrics(12,timestamp=now)),
                  patch.object(daemon,'read_disks',return_value=[]),
                  patch.object(daemon,'read_host_memory',return_value=(None,0,0)),
                  patch.object(daemon,'build_snapshot',return_value=snapshot()),
                  patch.object(daemon,'_event_reports',return_value=('fixture','fixture')),
                  patch.object(daemon,'Notifier') as notifier):
                notifier.return_value.send.return_value=[NotificationResult('gmail',False,'not configured',skipped=True)]
                _,_,evaluation=daemon.sample(settings,notify=True)
                daemon.sample(settings,notify=True)
            self.assertTrue(any('미래 heartbeat' in text for text in evaluation.observations))
            self.assertEqual(notifier.return_value.send.call_count,1)
            state=daemon.load_state(settings.state_path/'state.json')
            self.assertEqual(state['last_email_status'],0)
            self.assertEqual(state['last_email_success'],0)

    def test_all_supported_sampling_cadences_catch_each_due_hour_once(self):
        settings=Settings(email_heartbeat_enabled=True,email_heartbeat_minute=0)
        base=datetime(2026,10,6).timestamp()+60
        for step in (5,15,60,120,600):
            last=''; sent=[]
            for now in range(int(base),int(base)+86400-60,step):
                if daemon.email_heartbeat_due(now,last,settings):
                    sent.append(now)
                    last=daemon.email_heartbeat_slot(now,settings)
            with self.subTest(step=step): self.assertEqual(len(sent),24)

    def test_legacy_future_and_latest_processed_slots(self):
        settings=Settings(email_heartbeat_enabled=True,email_heartbeat_minute=20)
        now=datetime(2026,10,6,12,23).timestamp()
        self.assertTrue(daemon.email_heartbeat_due(now,'2026-10-06T10',settings))
        self.assertFalse(daemon.email_heartbeat_due(now,'2026-10-06T12',settings))
        latest=daemon.email_heartbeat_slot(now,settings)
        self.assertFalse(daemon.email_heartbeat_due(now+15,latest,settings))
        self.assertTrue(daemon.email_heartbeat_due(now,'epoch:'+str(int(now+86400)),settings))
        self.assertTrue(daemon.email_heartbeat_due(now,'broken',settings))
        self.assertLessEqual(daemon.next_email_heartbeat_at(now,'',settings).timestamp(),now)
        self.assertGreater(daemon.next_email_heartbeat_at(now,latest,settings).timestamp(),now)

    def test_dst_fold_and_non_hour_offset_use_distinct_epochs(self):
        original=os.environ.get('TZ')
        try:
            for zone,month,day,minute,difference in (
                    ('America/New_York',11,1,10,3600),('Australia/Lord_Howe',4,5,45,1800)):
                os.environ['TZ']=zone; time.tzset()
                settings=Settings(email_heartbeat_enabled=True,email_heartbeat_minute=minute)
                first=datetime(2026,month,day,1,minute,tzinfo=ZoneInfo(zone),fold=0).timestamp()
                second=datetime(2026,month,day,1,minute,tzinfo=ZoneInfo(zone),fold=1).timestamp()
                with self.subTest(zone=zone):
                    self.assertEqual(second-first,difference)
                    old=daemon.email_heartbeat_slot(first,settings)
                    self.assertTrue(daemon.email_heartbeat_due(second,old,settings))
                    self.assertNotEqual(old,daemon.email_heartbeat_slot(second,settings))
                    self.assertEqual(daemon.next_email_heartbeat_at(first,old,settings).timestamp(),second)
            os.environ['TZ']='America/New_York'; time.tzset()
            settings=Settings(email_heartbeat_enabled=True,email_heartbeat_minute=30)
            first=datetime(2026,3,8,1,30,tzinfo=ZoneInfo('America/New_York')).timestamp()
            following=daemon.next_email_heartbeat_at(first,daemon.email_heartbeat_slot(first,settings),settings)
            self.assertEqual(following.hour,3)
            self.assertEqual(following.timestamp()-first,3600)
        finally:
            if original is None: os.environ.pop('TZ',None)
            else: os.environ['TZ']=original
            time.tzset()

    def test_sample_persists_attempt_separately_from_success_without_retry_burst(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=[],email_heartbeat_enabled=True)
            base=datetime(2026,10,6,1,1).timestamp()
            readings=[metrics(12,timestamp=t) for t in (base,base+15,base+3600)]
            states=[]
            with (patch.object(daemon,'read_system_metrics',side_effect=readings),
                  patch.object(daemon,'read_disks',return_value=[]),
                  patch.object(daemon,'read_host_memory',return_value=(None,0,0)),
                  patch.object(daemon,'build_snapshot',return_value=snapshot()),
                  patch.object(daemon,'_event_reports',return_value=('fixture','<body>fixture</body>')),
                  patch.object(daemon,'Notifier') as notifier):
                notifier.return_value.send.side_effect=[
                    [NotificationResult('gmail',False,'fixture failure')],
                    [NotificationResult('gmail',True,'sent')]]
                for _ in readings:
                    daemon.sample(settings,notify=True)
                    states.append(daemon.load_state(settings.state_path/'state.json'))
            self.assertEqual(notifier.return_value.send.call_count,2)
            self.assertEqual(states[0]['last_email_status'],base)
            self.assertEqual(states[0]['last_email_success'],0)
            self.assertEqual(states[1]['last_email_hour_slot'],states[0]['last_email_hour_slot'])
            self.assertEqual(states[2]['last_email_success'],base+3600)

    def test_overdue_heartbeat_coalesces_with_gmail_alert_in_same_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=[],email_heartbeat_enabled=True)
            now=datetime(2026,10,6,1,1).timestamp()
            with (patch.object(daemon,'read_system_metrics',return_value=metrics(1,timestamp=now)),
                  patch.object(daemon,'read_disks',return_value=[]),
                  patch.object(daemon,'read_host_memory',return_value=(None,0,0)),
                  patch.object(daemon,'build_snapshot',return_value=snapshot()),
                  patch.object(daemon,'_event_reports',return_value=('fixture','fixture')),
                  patch.object(daemon,'Notifier') as notifier):
                notifier.return_value.send.return_value=[NotificationResult('gmail',True,'sent')]
                daemon.sample(settings,notify=True)
            self.assertEqual(notifier.return_value.send.call_count,1)
            state=daemon.load_state(settings.state_path/'state.json')
            self.assertEqual(state['last_email_hour_slot'],daemon.email_heartbeat_slot(now,settings))
