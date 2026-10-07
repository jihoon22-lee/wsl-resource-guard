"""Fail closed on missing/invalid SARIF, analyzer errors, or high security results."""
import json
import math
import sys
from pathlib import Path


def result_rule(run, result):
    descriptor = result.get('rule', {})
    component = descriptor.get('toolComponent')
    tool = run['tool']
    if component is None:
        rules = tool['driver'].get('rules', [])
    else:
        index = component.get('index')
        extensions = tool.get('extensions', [])
        if type(index) is not int or not 0 <= index < len(extensions):
            raise ValueError('Unknown SARIF tool component')
        rules = extensions[index].get('rules', [])
    if result.get('ruleId') is not None and descriptor.get('id') is not None and result['ruleId'] != descriptor['id']:
        raise ValueError('Inconsistent SARIF rule IDs')
    rule_id = result.get('ruleId', descriptor.get('id'))
    index = descriptor.get('index', result.get('ruleIndex'))
    if index is not None:
        if type(index) is not int or not 0 <= index < len(rules):
            raise ValueError('Unknown SARIF rule index')
        rule = rules[index]
        if rule_id is not None and rule.get('id') != rule_id:
            raise ValueError('Inconsistent SARIF rule reference')
        return rule
    matches = [rule for rule in rules if rule_id is not None and rule.get('id') == rule_id]
    if len(matches) != 1:
        raise ValueError('Result references unknown SARIF rule')
    return matches[0]


def check_sarif(path):
    data = json.loads(Path(path).read_text())
    runs = data.get('runs')
    if data.get('version') != '2.1.0' or not isinstance(runs, list) or not runs:
        raise ValueError('Missing SARIF runs')
    for run in runs:
        if not isinstance(run.get('tool', {}).get('driver'), dict):
            raise ValueError('Missing SARIF driver')
        if 'results' not in run or not isinstance(run['results'], list):
            raise ValueError('Missing SARIF results')
        for invocation in run.get('invocations', []):
            if invocation.get('executionSuccessful') is False:
                raise ValueError('Analyzer did not complete')
        for result in run['results']:
            rule = result_rule(run, result)
            level = result.get('level', rule.get('defaultConfiguration', {}).get('level', 'warning'))
            score = float(rule.get('properties', {}).get('security-severity', 0))
            if not math.isfinite(score) or not 0 <= score <= 10:
                raise ValueError('Invalid SARIF security score')
            if level == 'error' or score >= 7:
                raise ValueError('Blocked SARIF finding: ' + result.get('ruleId', 'unknown'))


if __name__ == '__main__':
    paths = list(Path(sys.argv[1]).glob('*.sarif')) if len(sys.argv) == 2 else []
    if not paths:
        raise SystemExit('No SARIF output; release blocked')
    for path in paths:
        check_sarif(path)
