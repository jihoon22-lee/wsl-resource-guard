import os
import pwd
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wsl_resource_guard.build_info import compare_stamps, read_stamp, source_revision, write_stamp
from wsl_resource_guard.service_install import disable_legacy_unit, sync_user_library


class ServiceInstallTests(unittest.TestCase):
    def test_missing_legacy_unit_is_not_disabled(self) -> None:
        run = Mock()
        with patch.object(Path, "exists", return_value=False):
            disable_legacy_unit(run)
        run.assert_not_called()

    def test_existing_legacy_unit_is_disabled(self) -> None:
        run = Mock()
        with patch.object(Path, "exists", return_value=True):
            disable_legacy_unit(run)
        run.assert_called_once_with(
            "systemctl", "disable", "devbox-wsl-service-recovery.service"
        )

    def test_user_library_receives_every_module_and_a_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            (source / "wsl_resource_guard").mkdir(parents=True)
            for name in ("cli.py", "services.py", "daemon.py", "disks.py"):
                (source / "wsl_resource_guard" / name).write_text(f"# {name}\n")
            user_lib = Path(directory) / "lib"
            user_lib.mkdir()
            (user_lib / "daemon.py").write_text("# stale\n")
            with patch("wsl_resource_guard.service_install.os.chown") as chown, \
                 patch("wsl_resource_guard.guard_install.snapshot_state"):
                owner = pwd.getpwuid(os.getuid())
                copied = sync_user_library(source, user_lib, owner.pw_uid, owner.pw_gid, Path(directory) / 'backup')
            self.assertEqual(copied, ["cli.py", "daemon.py", "disks.py", "services.py"])
            self.assertEqual((user_lib / "daemon.py").read_text(), "# daemon.py\n")
            chown.assert_not_called()  # user-writable paths are never chowned by root
            self.assertEqual(read_stamp(user_lib)["commit"], "unknown")  # not a git checkout


class BuildStampTests(unittest.TestCase):
    def test_stamp_round_trip_and_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            with patch("wsl_resource_guard.build_info.source_revision",
                       return_value={"commit": "a" * 40, "dirty": False}):
                write_stamp(package, package)
            self.assertEqual(read_stamp(package)["commit"], "a" * 40)
            (package / "BUILD.json").write_text("{broken")
            self.assertIsNone(read_stamp(package))

    def test_source_revision_outside_git_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(source_revision(Path(directory)), {"commit": "unknown", "dirty": False})

    def test_compare_stamps(self) -> None:
        same = {"commit": "a" * 40, "dirty": False}
        other = {"commit": "b" * 40, "dirty": False}
        self.assertEqual(compare_stamps(None, same, True)[0], "WARN")
        self.assertEqual(compare_stamps(same, None, False), ("OK", "a" * 12 + " · 웹·서비스 관리 미설치"))
        self.assertIn("install-services.sh", compare_stamps(same, None, True)[1])
        self.assertIn("≠", compare_stamps(same, other, True)[1])
        self.assertEqual(compare_stamps(same, dict(same, dirty=True), True)[0], "WARN")
        self.assertEqual(compare_stamps(same, dict(same), True), ("OK", "a" * 12))


if __name__ == "__main__":
    unittest.main()
