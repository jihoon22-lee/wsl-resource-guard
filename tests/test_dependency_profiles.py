import unittest
from scripts import dependency_profiles as profiles


class DependencyProfileTests(unittest.TestCase):
    def test_export_markers_and_hash_lines_preserve_active_versions(self):
        exported = '# uv export\nFlask==3.1.3 \\\n    --hash=sha256:aaaa\nMarkupSafe==3.0.3 ; python_version >= "3.11" \\\n    --hash=sha256:bbbb\nignored==1 ; python_version < "3.0"\n'
        self.assertEqual(profiles.export_versions(exported), {'flask': '3.1.3', 'markupsafe': '3.0.3'})

    def test_non_exact_or_conflicting_exports_fail(self):
        for text in ('Flask>=3', 'Flask @ https://example.invalid/x.whl',
                     'Flask==3.1.3\nflask==3.1.4', 'Flask==3.*', ''):
            with self.subTest(text=text), self.assertRaises(ValueError):
                profiles.export_versions(text)

    def test_runtime_packages_must_match_development_and_installed_versions(self):
        web = {'flask': '3.1.3', 'markupsafe': '3.0.3'}
        dev = {**web, 'playwright': '1.63.0'}
        profiles.check_versions(web, dev, dev)
        for candidate, installed in (({**dev, 'markupsafe':'3.0.4'}, dev),
                                     ({'flask':'3.1.3'}, dev),
                                     (dev, {**dev, 'flask':'3.1.2'}), (dev, web)):
            with self.subTest(candidate=candidate, installed=installed), self.assertRaises(ValueError):
                profiles.check_versions(web, candidate, installed)
