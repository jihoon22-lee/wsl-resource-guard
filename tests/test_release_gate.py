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
