"""Fail closed on missing/invalid SARIF, analyzer errors, or high security results."""
import json
import sys
from pathlib import Path


def check_sarif(path):
    data = json.loads(Path(path).read_text())
    runs = data.get('runs')
    if data.get('version') != '2.1.0' or not isinstance(runs, list) or not runs:
        raise ValueError('Missing SARIF runs')
    for run in runs:
        rules = {rule['id']: rule for rule in run['tool']['driver'].get('rules', [])}
        if 'results' not in run or not isinstance(run['results'], list):
            raise ValueError('Missing SARIF results')
        for invocation in run.get('invocations', []):
            if invocation.get('executionSuccessful') is False:
                raise ValueError('Analyzer did not complete')
        for result in run['results']:
            if result.get('ruleId') not in rules:
                raise ValueError('Result references unknown SARIF rule')
            rule = rules.get(result.get('ruleId'), {})
            level = result.get('level', rule.get('defaultConfiguration', {}).get('level', 'warning'))
            score = rule.get('properties', {}).get('security-severity', 0)
            if level == 'error' or float(score) >= 7:
                raise ValueError('Blocked SARIF finding: ' + result.get('ruleId', 'unknown'))


if __name__ == '__main__':
    paths = list(Path(sys.argv[1]).glob('*.sarif')) if len(sys.argv) == 2 else []
    if not paths:
        raise SystemExit('No SARIF output; release blocked')
    for path in paths:
        check_sarif(path)
