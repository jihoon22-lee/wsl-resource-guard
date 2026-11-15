from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard.config import (CONFIG_GROUPS, CONFIG_RULES, Settings,
                                       load_daemon_settings)

TEMPLATE = Path(__file__).resolve().parents[1] / "config/config.toml"


class SettingsTests(unittest.TestCase):
    def test_defaults_to_home_projects_root(self) -> None:
        settings = Settings()
        self.assertEqual(settings.project_roots, ["~/projects"])

    def test_loads_known_flat_toml_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'interval_seconds = 30\nproject_roots = ["/work"]\nwindows_toast_enabled = false\n',
                encoding="utf-8",
            )
            settings = Settings.load(path)
        self.assertEqual(settings.interval_seconds, 30)
        self.assertEqual(settings.project_roots, ["/work"])
        self.assertFalse(settings.windows_toast_enabled)


    def load_text(self, text: str) -> Settings:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return Settings.load(path)

    def test_drive_thresholds_load_and_reject_bad_tables(self) -> None:
        good = self.load_text('disk_drives = ["C", "E"]\nwarning_disk_free_gib = 50\n\n'
                              '[disk_thresholds.e]\nwarning_free_percent = 5\ncritical_free_percent = 3\n'
                              'warning_free_gib = 40\n')
        self.assertEqual(good.load_warnings, [])
        self.assertEqual(good.disk_thresholds, {'E': {'warning_free_percent': 5, 'critical_free_percent': 3,
                                                       'warning_free_gib': 40}})
        self.assertEqual(good.warning_disk_free_gib, 50)
        for text in ('[disk_thresholds.EE]\nwarning_free_percent = 5\n',           # not a drive letter
                     '[disk_thresholds.E]\nwarning_free_percent = 500\n',         # out of range
                     '[disk_thresholds.E]\nunknown_key = 1\n',
                     '[disk_thresholds.E]\nwarning_free_percent = true\n',
                     'disk_thresholds = "E"\n'):
            with self.subTest(text=text):
                bad = self.load_text(text)
                self.assertEqual(bad.disk_thresholds, {})
                self.assertEqual(len(bad.load_warnings), 1)

    def test_inconsistent_drive_limits_fall_back_for_that_drive_only(self) -> None:
        settings = self.load_text('[disk_thresholds.C]\nwarning_free_percent = 5\n\n'
                                  '[disk_thresholds.E]\nwarning_free_percent = 3\ncritical_free_percent = 5\n\n'
                                  '[disk_thresholds.D]\nwarning_free_gib = 10\ncritical_free_gib = 30\n')
        # C: warning 5% is not above the global critical 10%.
        self.assertEqual(settings.disk_thresholds, {})
        self.assertEqual(len(settings.load_warnings), 3)

    def test_absolute_floors_are_optional_but_ordered_when_both_set(self) -> None:
        self.assertEqual(self.load_text('warning_disk_free_gib = 50\n').load_warnings, [])
        self.assertEqual(self.load_text('critical_disk_free_gib = 20\n').load_warnings, [])
        bad = self.load_text('warning_disk_free_gib = 20\ncritical_disk_free_gib = 50\n')
        self.assertEqual((bad.warning_disk_free_gib, bad.critical_disk_free_gib), (0, 0))
        self.assertEqual(len(bad.load_warnings), 1)
        from wsl_resource_guard.config import check_relations
        with self.assertRaises(ValueError):
            check_relations(Settings(critical_disk_free_gib=50), 'warning_disk_free_gib', 20)
        check_relations(Settings(critical_disk_free_gib=50), 'warning_disk_free_gib', 0)   # 0 turns the floor off

    def test_shipped_template_loads_without_warnings(self) -> None:
        self.assertEqual(Settings.load(TEMPLATE).load_warnings, [])

    def test_shipped_template_matches_code_defaults(self) -> None:
        import tomllib
        defaults = Settings()
        for key, value in tomllib.loads(TEMPLATE.read_text(encoding="utf-8")).items():
            with self.subTest(key=key):
                self.assertEqual(value, getattr(defaults, key))

    def test_wrong_types_and_ranges_keep_defaults_and_warn(self) -> None:
        settings = self.load_text(
            'interval_seconds = "15"\nretention_days = 0\ngmail_enabled = "yes"\n'
            'project_roots = "/work"\ndisk_drives = ["C", "CD"]\nstate_dir = ""\n'
            'alert_quiet_hours = "25-99"\nwarning_available_gib = true\n'
        )
        defaults = Settings()
        for key in ("interval_seconds", "retention_days", "gmail_enabled", "project_roots",
                    "disk_drives", "state_dir", "alert_quiet_hours", "warning_available_gib"):
            with self.subTest(key=key):
                self.assertEqual(getattr(settings, key), getattr(defaults, key))
                self.assertTrue(any(w.startswith(key + ":") for w in settings.load_warnings))

    def test_zero_interval_cannot_create_a_busy_loop(self) -> None:
        settings = self.load_text("interval_seconds = 0\n")
        self.assertEqual(settings.interval_seconds, 15)

    def test_load_warnings_do_not_expose_exception_details(self) -> None:
        marker = 'private-exception-detail'
        with patch('wsl_resource_guard.config.coerce_config_value', side_effect=ValueError(marker)):
            settings = self.load_text('interval_seconds = 30\n')
        self.assertEqual(settings.interval_seconds, Settings().interval_seconds)
        self.assertTrue(settings.load_warnings)
        self.assertNotIn(marker, str(settings.load_warnings))
        with patch.object(Settings, 'load', side_effect=PermissionError(marker)):
            settings = load_daemon_settings()
        self.assertIn('PermissionError', str(settings.load_warnings))
        self.assertNotIn(marker, str(settings.load_warnings))

    def test_integral_float_is_accepted_for_integer_keys(self) -> None:
        settings = self.load_text("interval_seconds = 30.0\ndisk_drives = [\"c\"]\n")
        self.assertEqual(settings.interval_seconds, 30)
        self.assertIsInstance(settings.interval_seconds, int)
        self.assertEqual(settings.disk_drives, ["C"])
        self.assertEqual(settings.load_warnings, [])

    def test_relation_violation_restores_both_defaults(self) -> None:
        settings = self.load_text("warning_available_gib = 1.0\ncritical_available_gib = 2.0\n")
        self.assertEqual((settings.warning_available_gib, settings.critical_available_gib), (4.0, 2.0))
        self.assertTrue(any("warning_available_gib" in w for w in settings.load_warnings))

    def test_unknown_key_is_reported(self) -> None:
        settings = self.load_text("interval_second = 30\n")
        self.assertEqual(settings.interval_seconds, 15)
        self.assertTrue(any(w.startswith("interval_second:") for w in settings.load_warnings))

    def test_daemon_settings_survive_invalid_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_bytes(b"interval_seconds = 30\n# \xff\n")
            settings = load_daemon_settings(path)
        self.assertEqual(settings.interval_seconds, 15)
        self.assertEqual(settings.config_path, path)
        self.assertTrue(any("UnicodeDecodeError" in warning for warning in settings.load_warnings))

    def test_oversized_numeric_value_keeps_default_and_other_settings(self) -> None:
        settings = self.load_text("interval_seconds = 30\nwarning_available_gib = " + "9" * 310 + "\n")
        self.assertEqual(settings.warning_available_gib, 4.0)
        self.assertEqual(settings.interval_seconds, 30)
        self.assertTrue(any(w.startswith("warning_available_gib:") for w in settings.load_warnings))

    def test_daemon_settings_survive_parser_integer_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("interval_seconds = " + "9" * 5000 + "\n", encoding="utf-8")
            settings = load_daemon_settings(path)
        self.assertEqual(settings.interval_seconds, 15)
        self.assertTrue(settings.load_warnings)

    def test_config_groups_cover_rules_exactly_once(self) -> None:
        grouped = [key for _, keys in CONFIG_GROUPS for key in keys]
        self.assertEqual(sorted(grouped), sorted(CONFIG_RULES))
        self.assertEqual(len(grouped), len(set(grouped)))
        titles = [title for title, _ in CONFIG_GROUPS]
        self.assertEqual(len(titles), len(set(titles)))

    def test_daemon_settings_survive_broken_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("interval_seconds = [\n", encoding="utf-8")
            settings = load_daemon_settings(path)
        self.assertEqual(settings.interval_seconds, 15)
        self.assertEqual(settings.config_path, path)
        self.assertTrue(any("TOMLDecodeError" in w for w in settings.load_warnings))

if __name__ == "__main__":
    unittest.main()
