import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from wsl_resource_guard.build_info import compare_stamps, source_revision


class ReleaseMetadataTests(unittest.TestCase):
    def fixture(self, root):
        content = '[project]\nname="wsl-resource-guard"\nversion="0.1.0"\n'
        (root / 'pyproject.toml').write_text(content)
        manifest = {'schema': 1, 'version': '0.1.0', 'commit': 'a' * 40,
                    'files': {'pyproject.toml': hashlib.sha256(content.encode()).hexdigest()}}
        (root / 'SOURCE.json').write_text(json.dumps(manifest))

    def test_archive_revision_survives_without_git(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            self.assertEqual(source_revision(root), {'commit': 'a' * 40, 'dirty': False,
                                                    'version': '0.1.0'})

    def test_corrupt_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            (root / 'pyproject.toml').write_text('changed')
            with self.assertRaises(ValueError):
                source_revision(root)

    def test_manifest_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            data = json.loads((root / 'SOURCE.json').read_text())
            data['files']['../escape'] = 'b' * 64
            (root / 'SOURCE.json').write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                source_revision(root)

    def test_unlisted_installable_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            (root / 'wsl_resource_guard').mkdir()
            (root / 'wsl_resource_guard/extra.py').write_text('print("extra")')
            with self.assertRaises(ValueError):
                source_revision(root)

    def test_two_unknown_copies_are_not_confirmed_equal(self):
        unknown = {'commit': 'unknown', 'dirty': False}
        self.assertEqual(compare_stamps(unknown, unknown, True)[0], 'WARN')
        self.assertEqual(compare_stamps(unknown, None, False)[0], 'WARN')


if __name__ == '__main__':
    unittest.main()
