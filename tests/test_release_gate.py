import json
import tempfile
import unittest
from pathlib import Path
from scripts.security_gate import check_sarif


class SecurityGateTests(unittest.TestCase):
    def check(self, score='0', level='warning', results=True, known=True):
        rule = {'id': 'example/rule', 'properties': {'security-severity': score}}
        run = {'tool': {'driver': {'rules': [rule] if known else []}}, 'results': []}
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
        run = {'tool': {'driver': {'rules': []}, 'extensions': [
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
