import json
import tempfile
import unittest
from pathlib import Path
from scripts.security_gate import check_sarif


class SecurityGateTests(unittest.TestCase):
    def check(self, score='0', level='warning', results=True, known=True):
        rule = {'id': 'example/rule', 'properties': {'security-severity': score}}
        run = {'tool': {'driver': {'name': 'CodeQL', 'rules': [rule] if known else []}}, 'results': [],
               'invocations': [{'executionSuccessful': True}]}
        if results:
            run['results'] = [{'ruleId': 'example/rule', 'level': level}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.sarif'
            path.write_text(json.dumps({'version': '2.1.0', 'runs': [run]}))
            check_sarif(path)

    def test_high_and_critical_findings_block(self):
        for score in ('7.0', '9.8'):
            with self.subTest(score=score), self.assertRaises(ValueError):
                self.check(score)

    def test_error_blocks_without_security_score(self):
        with self.assertRaises(ValueError):
            self.check(level='error')

    def test_empty_and_low_results_allow_publication(self):
        self.check(results=False)
        self.check(score='3.0')

    def test_unknown_rule_cannot_hide_security_score(self):
        with self.assertRaises(ValueError):
            self.check(known=False)

    def test_extension_rule_security_score_is_enforced(self):
        run = {'invocations': [{'executionSuccessful': True}],
               'tool': {'driver': {'name': 'CodeQL', 'rules': []}, 'extensions': [
            {'rules': [{'id': 'example/rule', 'properties': {'security-severity': '7.5'}}]}]},
            'results': [{'ruleId': 'example/rule', 'rule': {
                'id': 'example/rule', 'index': 0, 'toolComponent': {'index': 0}}}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.sarif'
            path.write_text(json.dumps({'version': '2.1.0', 'runs': [run]}))
            with self.assertRaisesRegex(ValueError, 'Blocked SARIF finding'):
                check_sarif(path)
            run['tool']['extensions'][0]['rules'][0]['properties']['security-severity'] = '3.0'
            path.write_text(json.dumps({'version': '2.1.0', 'runs': [run]}))
            check_sarif(path)

    def test_nonfinite_severity_cannot_bypass_gate(self):
        with self.assertRaises(ValueError):
            self.check(score='NaN')

    def test_missing_results_and_failed_analysis_block(self):
        for run in ({'tool': {'driver': {}}},
                    {'tool': {'driver': {}}, 'results': [],
                     'invocations': [{'executionSuccessful': False}]}):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'bad.sarif'
                path.write_text(json.dumps({'version': '2.1.0', 'runs': [run]}))
                with self.assertRaises(ValueError):
                    check_sarif(path)


class CompleteAnalysisTests(unittest.TestCase):
    def good(self):
        return {'tool': {'driver': {'name': 'CodeQL', 'rules': []},
                         'extensions': [{'name': 'codeql/python-queries', 'rules': [{'id': 'py/example'}]}]},
                'invocations': [{'executionSuccessful': True}], 'results': []}

    def check_run(self, run):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'python.sarif'
            path.write_text(json.dumps({'version': '2.1.0', 'runs': [run]}))
            check_sarif(path)

    def test_missing_or_ambiguous_completion_is_blocked(self):
        for invocations in (None, [], [{}], [{'executionSuccessful': False}],
                            [{'executionSuccessful': 1}], [{'executionSuccessful': True}, {}]):
            run = self.good()
            if invocations is None:
                del run['invocations']
            else:
                run['invocations'] = invocations
            with self.subTest(invocations=invocations), self.assertRaises(ValueError):
                self.check_run(run)

    def test_error_notifications_block_even_with_success_flag(self):
        for key in ('toolExecutionNotifications', 'toolConfigurationNotifications'):
            run = self.good()
            run['invocations'][0][key] = [{'level': 'error', 'message': {'text': 'fixture failure'}}]
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check_run(run)
            run['invocations'][0][key][0]['level'] = 'warning'
            self.check_run(run)

    def test_zero_rules_and_wrong_analyzer_block(self):
        for change in ('empty', 'wrong'):
            run = self.good()
            if change == 'empty':
                run['tool']['extensions'][0]['rules'] = []
            else:
                run['tool']['driver']['name'] = 'unrelated tool'
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.check_run(run)
        self.check_run(self.good())

    def test_conflicting_result_indices_or_component_identity_are_blocked(self):
        for component, index in (({'index': 0, 'name': 'wrong'}, 0), ({'index': 0}, 1)):
            run = self.good()
            run['results'] = [{'ruleId': 'py/example', 'ruleIndex': index,
                              'rule': {'index': 0, 'toolComponent': component}}]
            with self.subTest(component=component, index=index), self.assertRaises(ValueError):
                self.check_run(run)

    def test_invalid_rule_metadata_cannot_masquerade_as_clean_analysis(self):
        for score in ('NaN', '-1', '11', True, [], {}):
            run = self.good()
            run['tool']['extensions'][0]['rules'][0]['properties'] = {'security-severity': score}
            with self.subTest(score=score), self.assertRaises(ValueError):
                self.check_run(run)


class LanguageOutputTests(unittest.TestCase):
    def test_both_expected_languages_are_required_and_swaps_are_rejected(self):
        from scripts.security_gate import check_outputs
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for language in ('python', 'javascript'):
                run = CompleteAnalysisTests().good()
                run['tool']['extensions'][0]['name'] = f'codeql/{language}-queries'
                (root / f'{language}.sarif').write_text(json.dumps({'version': '2.1.0', 'runs': [run]}))
            check_outputs(root)
            saved = (root / 'javascript.sarif').read_text()
            (root / 'javascript.sarif').unlink()
            with self.assertRaisesRegex(ValueError, 'language SARIF'):
                check_outputs(root)
            (root / 'javascript.sarif').write_text((root / 'python.sarif').read_text())
            with self.assertRaisesRegex(ValueError, 'language query pack'):
                check_outputs(root)
            (root / 'javascript.sarif').write_text(saved)
            (root / 'unexpected.sarif').write_text(saved)
            with self.assertRaises(ValueError):
                check_outputs(root)

    def test_command_failure_prevents_the_next_publish_command(self):
        import subprocess
        import sys
        from scripts.security_gate import __file__ as gate_file
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'python.sarif'
            run = CompleteAnalysisTests().good()
            for invalid in (True, False):
                run['invocations'] = [] if invalid else [{'executionSuccessful': True}]
                path.write_text(json.dumps({'version': '2.1.0', 'runs': [run]}))
                result = subprocess.run(['/bin/bash', '-e', '-c',
                    '\"$1\" \"$2\" \"$3\" --language python\nprintf published',
                    'gate-fixture', sys.executable, str(gate_file), str(root)],
                    text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode == 0, not invalid)
                self.assertEqual('published' in result.stdout, not invalid)

    def test_release_graph_cannot_publish_when_checks_fail(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        release = yaml.safe_load((root / '.github/workflows/release.yml').read_text())['jobs']
        checks = yaml.safe_load((root / '.github/workflows/checks.yml').read_text())['jobs']
        self.assertEqual(release['checks']['uses'], './.github/workflows/checks.yml')
        self.assertEqual(set(release['package']['needs']), {'public', 'checks'})
        self.assertEqual(release['publish']['needs'], 'package')
        for name in ('checks', 'package', 'publish'):
            self.assertNotIn('if', release[name], 'Keep default success() dependency gating')
            self.assertNotIn('continue-on-error', release[name])
        self.assertEqual(set(checks['codeql']['strategy']['matrix']['language']),
                         {'python', 'javascript-typescript'})
        gates = [step for step in checks['codeql']['steps'] if 'security_gate.py' in step.get('run', '')]
        self.assertEqual(len(gates), 1)
        self.assertIn('--language', gates[0]['run'])
        self.assertNotIn('continue-on-error', gates[0])
        downloads = [step for step in release['package']['steps']
                     if step.get('uses', '').startswith('actions/download-artifact@')]
        self.assertEqual(downloads[0]['with']['name'], 'checked-release-assets')
        self.assertEqual(downloads[0]['with']['digest-mismatch'], 'error')
        self.assertFalse(any('release.py build' in step.get('run', '') for step in release['package']['steps']),
                         'Publish the archive checked by this workflow, without rebuilding it')
        downloads = [step for step in release['publish']['steps']
                     if step.get('uses', '').startswith('actions/download-artifact@')]
        self.assertEqual(downloads[0]['with']['digest-mismatch'], 'error')

    def test_manual_rehearsal_fixture_reaches_real_security_gate(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        for scenario in ('none', 'check-failure', 'analysis-missing', 'security-high'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                for language in ('python', 'javascript'):
                    run = CompleteAnalysisTests().good()
                    run['tool']['extensions'][0]['name'] = f'codeql/{language}-queries'
                    (output/f'{language}.sarif').write_text(json.dumps({'version':'2.1.0','runs':[run]}))
                injected = subprocess.run([sys.executable, str(root/'tests/release_rehearsal.py'), directory, scenario],
                                          text=True, capture_output=True, timeout=10)
                if scenario == 'check-failure':
                    self.assertNotEqual(injected.returncode, 0)
                    self.assertIn('Injected release check failure', injected.stderr)
                    continue
                self.assertEqual(injected.returncode, 0, injected.stderr)
                checked = subprocess.run([sys.executable, str(root/'scripts/security_gate.py'), directory],
                                         text=True, capture_output=True, timeout=10)
                self.assertEqual(checked.returncode == 0, scenario == 'none', checked.stderr)
                if scenario == 'security-high':
                    self.assertIn('Blocked SARIF finding', checked.stderr)

    def test_manual_release_can_only_rehearse_and_still_checks_both_analyses(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root/'.github/workflows/release.yml').read_text())
        events = workflow.get('on', workflow.get(True))
        self.assertIn('workflow_dispatch', events)
        package = workflow['jobs']['package']['steps']
        self.assertTrue(any('security_gate.py' in step.get('run','') for step in package))
        publish = workflow['jobs']['publish']['steps']
        sends = [step for step in publish if 'gh release create' in step.get('run','')]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].get('if'), "github.event_name == 'push'")
        self.assertTrue(any(step.get('if') == "github.event_name == 'workflow_dispatch'" for step in publish))
