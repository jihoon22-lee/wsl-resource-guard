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
        # One bad table costs only that drive; duplicates and unmonitored drives are reported.
        mixed = self.load_text('disk_drives = ["C", "E"]\n\n[disk_thresholds.C]\nwarning_free_percent = 30\n\n'
                               '[disk_thresholds.E]\nwarning_free_percent = 500\n\n'
                               '[disk_thresholds.F]\nwarning_free_percent = 15\n')
        self.assertEqual(mixed.disk_thresholds, {'C': {'warning_free_percent': 30}, 'F': {'warning_free_percent': 15}})
        self.assertEqual(len(mixed.load_warnings), 2)
        self.assertTrue(any('disk_drives에 없는' in warning for warning in mixed.load_warnings))
        twice = self.load_text('[disk_thresholds.e]\nwarning_free_percent = 30\n\n'
                               '[disk_thresholds.E]\nwarning_free_percent = 25\n')
        self.assertEqual(twice.disk_thresholds, {'E': {'warning_free_percent': 25}})
        self.assertTrue(any('둘입니다' in warning for warning in twice.load_warnings))

    def test_a_table_placed_before_root_keys_is_reported_not_silently_applied(self) -> None:
        # In TOML every key after [table] belongs to it; the swallowed keys make the table invalid.
        swallowed = self.load_text('gmail_enabled = false\n[disk_thresholds.E]\nwarning_free_percent = 5\n'
                                   'windows_toast_enabled = false\n')
        self.assertEqual(swallowed.disk_thresholds, {})
        self.assertFalse(swallowed.gmail_enabled)
        self.assertTrue(swallowed.windows_toast_enabled)      # it was read as part of the table
        self.assertEqual(len(swallowed.load_warnings), 1)
        template = TEMPLATE.read_text().splitlines()
        first_table = next(i for i, line in enumerate(template) if 'disk_thresholds.' in line)
        self.assertFalse([line for line in template[first_table:] if line and not line.startswith('#')])

    def test_inconsistent_drive_limits_fall_back_for_that_drive_only(self) -> None:
        settings = self.load_text('[disk_thresholds.C]\nwarning_free_percent = 5\n\n'
                                  '[disk_thresholds.E]\nwarning_free_percent = 3\ncritical_free_percent = 5\n\n'
                                  '[disk_thresholds.D]\nwarning_free_gib = 10\ncritical_free_gib = 30\n')
        # C: warning 5% is not above the global critical 10%.
        self.assertEqual(settings.disk_thresholds, {})
        self.assertEqual(len(settings.load_warnings), 3)
        kept = self.load_text('[disk_thresholds.C]\nwarning_free_percent = 30\n\n'
                              '[disk_thresholds.E]\nwarning_free_percent = 3\ncritical_free_percent = 5\n')
        self.assertEqual(kept.disk_thresholds, {'C': {'warning_free_percent': 30}})   # only E is dropped
        self.assertEqual(len(kept.load_warnings), 1)

    def test_a_common_value_that_would_cancel_a_drive_table_is_refused(self) -> None:
        from wsl_resource_guard.config import check_relations
        settings = Settings(warning_disk_free_gib=100, disk_thresholds={'E': {'warning_free_gib': 20}})
        with self.assertRaisesRegex(ValueError, 'disk_thresholds.E'):
            check_relations(settings, 'critical_disk_free_gib', 50)
        check_relations(settings, 'critical_disk_free_gib', 10)
        with self.assertRaisesRegex(ValueError, 'disk_thresholds.E'):
            check_relations(Settings(disk_thresholds={'E': {'warning_free_percent': 12}}),
                            'critical_disk_free_percent', 15)

    def test_settings_with_drive_tables_cross_the_owner_worker_boundary(self) -> None:
        import json
        from dataclasses import asdict
        settings = Settings(warning_disk_free_gib=50, disk_thresholds={'E': {'warning_free_percent': 5.5}})
        values = json.loads(json.dumps(dict(asdict(settings), config_path='x', secrets_path='y')))
        values['config_path'], values['secrets_path'] = Path(values['config_path']), Path(values['secrets_path'])
        rebuilt = Settings(**values)
        self.assertEqual(rebuilt.disk_thresholds, settings.disk_thresholds)
        self.assertEqual(rebuilt.warning_disk_free_gib, 50)

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

    def test_host_limits_are_optional_and_ordered_when_both_set(self) -> None:
        defaults = Settings()
        self.assertGreater(defaults.warning_host_available_gib, defaults.critical_host_available_gib)
        self.assertLess(defaults.warning_host_commit_percent, defaults.critical_host_commit_percent)
        self.assertEqual(self.load_text('warning_host_available_gib = 0\n').load_warnings, [])
        self.assertEqual(self.load_text('critical_host_commit_percent = 0\n').load_warnings, [])
        for text in ('warning_host_available_gib = 1\ncritical_host_available_gib = 2\n',
                     'warning_host_commit_percent = 97\ncritical_host_commit_percent = 95\n'):
            with self.subTest(text=text):
                self.assertEqual(len(self.load_text(text).load_warnings), 1)

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
