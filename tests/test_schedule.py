import unittest
from datetime import datetime, timedelta

from wsl_resource_guard.config import Settings
from wsl_resource_guard.daemon import (
    alert_reminder_due,
    email_heartbeat_due,
    email_hour_slot,
    next_email_heartbeat_at,
)


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            reminder_cooldown_seconds=21600,
            critical_reminder_seconds=900,
            email_heartbeat_enabled=True,
            email_heartbeat_minute=0,
        )

    @staticmethod
    def local_timestamp(hour: int, minute: int, second: int = 0) -> float:
        local_tz = datetime.now().astimezone().tzinfo
        return datetime(2026, 8, 25, hour, minute, second, tzinfo=local_tz).timestamp()

    def test_warning_reminds_every_six_hours(self) -> None:
        self.assertFalse(alert_reminder_due("warning", "warning", 21599, 0, self.settings))
        self.assertTrue(alert_reminder_due("warning", "warning", 21600, 0, self.settings))

    def test_critical_reminds_every_fifteen_minutes(self) -> None:
        self.assertFalse(alert_reminder_due("critical", "critical", 899, 0, self.settings))
        self.assertTrue(alert_reminder_due("critical", "critical", 900, 0, self.settings))

    def test_state_transition_alerts_immediately(self) -> None:
        self.assertTrue(alert_reminder_due("warning", "normal", 1, 0, self.settings))

    def test_email_heartbeat_is_due_at_top_of_hour(self) -> None:
        now = self.local_timestamp(1, 0, 5)
        self.assertTrue(email_heartbeat_due(now, "", self.settings))

    def test_email_heartbeat_is_not_due_before_top_of_hour(self) -> None:
        now = self.local_timestamp(0, 59, 59)
        self.assertFalse(email_heartbeat_due(now, email_hour_slot(now), self.settings))

    def test_email_heartbeat_is_sent_only_once_per_hour_slot(self) -> None:
        now = self.local_timestamp(1, 0, 20)
        self.assertFalse(email_heartbeat_due(now, email_hour_slot(now), self.settings))

    def test_email_heartbeat_is_due_in_the_next_hour_slot(self) -> None:
        previous = self.local_timestamp(0, 0, 10)
        now = self.local_timestamp(1, 0, 10)
        self.assertTrue(email_heartbeat_due(now, email_hour_slot(previous), self.settings))

    def test_next_heartbeat_is_next_top_of_hour(self) -> None:
        now = self.local_timestamp(1, 23, 45)
        expected = datetime.fromtimestamp(now).astimezone().replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=1)
        self.assertEqual(next_email_heartbeat_at(now, email_hour_slot(now), self.settings), expected)


if __name__ == "__main__":
    unittest.main()
