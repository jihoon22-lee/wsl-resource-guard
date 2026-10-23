import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from wsl_resource_guard.build_info import compare_stamps, source_revision
from scripts import release


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


class ReleaseDocumentationTests(unittest.TestCase):
    def archive(self, folder, files):
        files = {'pyproject.toml': '[project]\nname="wsl-resource-guard"\nversion="0.1.0"\n',
                 'bin/wrg': 'print("fixture CLI")\n', **files}
        manifest = {'schema': 1, 'version': '0.1.0', 'commit': 'a' * 40,
                    'files': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}}
        files['SOURCE.json'] = json.dumps(manifest)
        path = Path(folder)/'release.tar.gz'
        with tarfile.open(path, 'w:gz') as tar:
            for name, text in files.items():
                payload = text.encode()
                member = tarfile.TarInfo('wsl-resource-guard-0.1.0/' + name)
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))
        return path

    def test_allowlist_includes_current_readme_and_review_documents(self):
        for name in ('docs/paseo.md', 'workthrough/2026-10-22-release-0.2.3.md',
                     'workthrough/2026-10-23-five-stage-code-review.md',
                     'workthrough/2026-10-23-review-remediation.md'):
            with self.subTest(name=name):
                self.assertIn(name, release.ROOT_FILES | release.DOCS)

    def test_archive_links_resolve_inside_the_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = self.archive(folder, {
                'README.md': '[Paseo](docs/paseo.md#setup) [spaced](<docs/a b.md>)\n'
                             '[external](https://example.com/docs) [anchor](#here)\n'
                             '[ref]: docs/paseo.md "title"\n',
                'docs/paseo.md': '[home](../README.md) [encoded](a%20b.md?download=1)\n',
                'docs/a b.md': '# Here\n'})
            release.verify(archive)

    def test_manifest_valid_archive_with_missing_relative_target_is_rejected(self):
        for link in ('[Paseo](docs/paseo.md)', '[Paseo][guide]\n[guide]: docs/paseo.md',
                     '[escape](../outside.md)'):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as folder:
                archive = self.archive(folder, {'README.md': link})
                with self.assertRaisesRegex(ValueError, 'documentation target'):
                    release.verify(archive)


if __name__ == '__main__':
    unittest.main()
