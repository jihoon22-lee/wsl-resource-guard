"""Only consecutive observations may mature sustain or recovery timers."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard import daemon
from wsl_resource_guard.config import Settings
from test_daemon import metrics, snapshot


class ObservationGapTests(unittest.TestCase):
    def run_samples(self, settings, readings, snapshots=None, disks=None):
        results=[]; states=[]
        with ExitStack() as stack:
            stack.enter_context(patch.object(daemon,'read_system_metrics',side_effect=readings))
            stack.enter_context(patch.object(daemon,'build_snapshot',side_effect=snapshots or [snapshot()]*len(readings)))
            stack.enter_context(patch.object(daemon,'read_disks',side_effect=disks or [[]]*len(readings)))
            stack.enter_context(patch.object(daemon,'read_host_memory',return_value=(None,0,0)))
            for _ in readings:
                results.append(daemon.sample(settings)[2])
                states.append(daemon.load_state(settings.state_path/'state.json'))
        return results,states

    def test_gap_does_not_mature_ram_swap_session_or_mcp(self):
        for kind in ('ram','swap','session','mcp'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                settings=Settings(state_dir=directory,disk_drives=[],interval_seconds=15,
                    warning_available_sustain_seconds=30,warning_swap_out_sustain_seconds=30,
                    process_warning_sustain_seconds=30)
                times=(100,1000,1015,1030)
                readings=[metrics(3 if kind in ('ram','swap') else 12,
                                  swap_out=1000 if kind=='swap' else 0,timestamp=t) for t in times]
                snaps=[snapshot(rss_gib=12 if kind=='session' else 0,
                                mcp_rss_gib=8 if kind=='mcp' else 0)]*len(times)
                results,states=self.run_samples(settings,readings,snaps)
                self.assertEqual([r.severity for r in results],['normal','normal','normal','warning'])
                self.assertTrue(all(t==1000 for t in states[1]['condition_since'].values()))

    def test_normal_gap_cannot_recover_an_active_ram_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=[],recovery_sustain_seconds=30)
            readings=[metrics(1,timestamp=100),metrics(12,timestamp=115),
                      metrics(12,timestamp=1000),metrics(12,timestamp=1015),metrics(12,timestamp=1030)]
            results,states=self.run_samples(settings,readings)
            self.assertEqual([r.severity for r in results],['critical']*4+['normal'])
            self.assertEqual(states[2]['normal_since'],1000)

    def test_clock_reversal_restarts_pending_and_normal_streaks(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=[],warning_available_sustain_seconds=30)
            _,states=self.run_samples(settings,[metrics(3,timestamp=t) for t in (100,115,110)])
            self.assertEqual(states[-1]['condition_since']['warning.available_ram'],110)

    def test_disk_recovery_timer_resets_across_sample_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=['C:'],recovery_sustain_seconds=30)
            old=metrics(12,timestamp=100)
            old.disks=[dict(id='C:',kind='windows',status='ok',available_percent=5,
                            available_bytes=5*2**30,total_bytes=100*2**30)]
            daemon.save_state(settings.state_path/'state.json',dict(metrics=old.to_dict(),
                severity='critical',notified_severity='critical',reasons=['C: 디스크 여유'],
                notified_disk_conditions=['critical.disk.C:'],disk_normal_since={'C:':100}))
            good=dict(old.disks[0],available_percent=50,available_bytes=50*2**30)
            _,states=self.run_samples(settings,[metrics(12,timestamp=1000)],disks=[[good]])
            self.assertEqual(states[0]['disk_normal_since'],{'C:':1000})
            self.assertIn('critical.disk.C:',states[0]['notified_disk_conditions'])

    def test_changed_interval_uses_new_gap_tolerance(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=[],interval_seconds=60,
                              warning_available_sustain_seconds=180)
            self.run_samples(settings,[metrics(3,timestamp=100),metrics(3,timestamp=160)])
            settings.interval_seconds=15
            _,states=self.run_samples(settings,[metrics(3,timestamp=220)])
            self.assertEqual(states[0]['condition_since']['warning.available_ram'],220)

    def test_disk_read_failure_does_not_prove_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=['C'],recovery_sustain_seconds=30)
            bad=dict(id='C:',kind='windows',status='ok',available_percent=5,
                     available_bytes=5*2**30,total_bytes=100*2**30)
            unavailable=dict(id='C:',kind='windows',status='error',error='fixture')
            good=dict(bad,available_percent=50,available_bytes=50*2**30)
            readings=[metrics(12,timestamp=t) for t in (100,115,130,145,160,175,190)]
            results,_=self.run_samples(settings,readings,
                disks=[[bad],[unavailable],[unavailable],[unavailable],[good],[good],[good]])
            self.assertEqual([r.severity for r in results],['critical']*6+['normal'])

    def test_failed_sample_does_not_advance_persisted_streak(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(state_dir=directory,disk_drives=[])
            self.run_samples(settings,[metrics(3,timestamp=100)])
            before=(settings.state_path/'state.json').read_bytes()
            with patch.object(daemon,'read_system_metrics',side_effect=OSError('fixture')):
                with self.assertRaises(OSError): daemon.sample(settings)
            self.assertEqual((settings.state_path/'state.json').read_bytes(),before)
            results,_=self.run_samples(settings,[metrics(3,timestamp=1000)])
            self.assertEqual(results[0].severity,'normal')
