"""Scheduled and incident transport outcomes share diagnostics, not reminder clocks."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard.config import Settings
from wsl_resource_guard.delivery import Delivery, merge_delivery_summary
from wsl_resource_guard.incidents import atomic_write
from wsl_resource_guard.notifications import NotificationResult


class ScheduledDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shared = self.root / 'shared'
        self.shared.mkdir()
        self.settings = Settings(state_dir=str(self.root), secrets_path=self.root/'secrets.json',
                                 email_heartbeat_enabled=True, weekly_report_enabled=False)
        self.settings.secrets_path.write_text('{}')
        self.now = 5000
        self.engine = Delivery(self.settings, self.shared, clock=lambda: self.now)

    def report(self, results):
        atomic_write(self.root/'state.json', {'updated_at': self.now, 'severity': 'normal'})
        with patch('wsl_resource_guard.delivery.Notifier.send', return_value=results) as send:
            self.engine.reports()
            self.engine.reports()
        self.assertEqual(send.call_count, 1)
        return json.loads((self.root/'scheduled-delivery.json').read_text())

    def test_outcomes_are_visible_without_an_incident_ledger(self):
        for result, status, attempt, success, error in (
                (NotificationResult('gmail', True, 'fixture'), 'accepted', 5000, 5000, False),
                (NotificationResult('gmail', False, 'private credential failure'), 'failed', 5000, 0, True),
                (NotificationResult('gmail', False, 'TimeoutError private'), 'unknown', 5000, 0, True),
                (NotificationResult('gmail', False, 'not configured', skipped=True), 'skipped', 0, 0, False),
                (None, 'skipped', 0, 0, False)):
            with self.subTest(status=status, result=result):
                (self.root/'scheduled-delivery.json').unlink(missing_ok=True)
                record = self.report([result] if result else [])
                source = {'last_alert': 100, 'last_email_status': 0, 'last_email_success': 0}
                before = copy.deepcopy(source)
                summary = merge_delivery_summary(source, self.root)
                self.assertEqual(record['status'], status)
                self.assertEqual(summary['last_email_status'], attempt)
                self.assertEqual(summary['last_email_success'], success)
                self.assertEqual('gmail' in summary['last_channel_errors'], error)
                self.assertEqual(summary['last_email_hour_slot'], record['slot'])
                self.assertEqual(summary['last_alert'], 100)
                self.assertEqual(source, before)
                self.assertNotIn('private', json.dumps((record, summary)))

    def test_success_survives_later_failure_and_skipped_slots(self):
        for now, result, expected_attempt in (
                (5000, NotificationResult('gmail', True, ''), 5000),
                (8600, NotificationResult('gmail', False, 'SMTPAuthenticationError'), 8600),
                (12200, NotificationResult('gmail', False, 'missing', skipped=True), 8600)):
            self.now = now
            self.report([result])
            summary = merge_delivery_summary({}, self.root)
            self.assertEqual(summary['last_email_status'], expected_attempt)
            self.assertEqual(summary['last_email_success'], 5000)

    def test_independent_delivery_failures_do_not_hide_each_other(self):
        for incident_status, scheduled_sent in (('accepted', False), ('failed', True), ('failed', False)):
            with self.subTest(incident_status=incident_status, scheduled_sent=scheduled_sent):
                (self.root/'scheduled-delivery.json').unlink(missing_ok=True)
                atomic_write(self.root/'delivery.json', {'events': {'event': {
                    'next_reminder_at': 9000, 'destinations': {'gmail:one': {
                        'channel': 'gmail', 'status': incident_status, 'attempted_at': 4900,
                        'finished_at': 4901}}}}})
                ledger_before = (self.root/'delivery.json').read_bytes()
                self.report([NotificationResult('gmail', scheduled_sent, 'fixture')])
                summary = merge_delivery_summary({}, self.root)
                self.assertIn('gmail', summary['last_channel_errors'])
                self.assertEqual(summary['last_alert'], 4900)
                self.assertEqual(summary['last_email_status'], 5000)
                self.assertEqual(summary['last_email_success'], 5000 if scheduled_sent else
                                 4901 if incident_status == 'accepted' else 0)
                self.assertEqual((self.root/'delivery.json').read_bytes(), ledger_before)

    def test_interrupted_transport_is_unknown_and_not_resent_in_same_slot(self):
        atomic_write(self.root/'state.json', {'updated_at': self.now})
        with patch('wsl_resource_guard.delivery.Notifier.send', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.reports()
        with patch('wsl_resource_guard.delivery.Notifier.send') as send:
            Delivery(self.settings, self.shared, clock=lambda: self.now).reports()
        send.assert_not_called()
        summary = merge_delivery_summary({}, self.root)
        self.assertIn('gmail', summary['last_channel_errors'])
        self.assertEqual(summary['last_email_status'], 5000)
        self.assertEqual(summary['last_email_success'], 0)

    def test_latest_destination_failure_keeps_an_older_success_timestamp(self):
        atomic_write(self.root/'delivery.json', {'events': {
            'old': {'destinations': {'gmail:one': {'channel': 'gmail', 'status': 'accepted',
                                                  'attempted_at': 4800, 'finished_at': 4801}}},
            'new': {'destinations': {'gmail:one': {'channel': 'gmail', 'status': 'failed',
                                                  'attempted_at': 4900, 'finished_at': 4901}}}}})
        summary = merge_delivery_summary({}, self.root)
        self.assertEqual(summary['last_email_success'], 4801)
        self.assertIn('gmail', summary['last_channel_errors'])

    def test_legacy_accepted_report_preserves_success_after_next_slot(self):
        atomic_write(self.root/'scheduled-delivery.json', {
            'slot': 'epoch:0', 'status': 'accepted', 'attempted_at': 100, 'finished_at': 101})
        self.report([NotificationResult('gmail', False, 'failure')])
        self.assertEqual(merge_delivery_summary({}, self.root)['last_email_success'], 101)


if __name__ == '__main__':
    unittest.main()
