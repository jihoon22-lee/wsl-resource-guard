"""Fail closed on missing/invalid SARIF, analyzer errors, or high security results."""
import argparse
import json
import math
from pathlib import Path


def result_rule(run, result):
    descriptor = result.get('rule', {})
    if not isinstance(descriptor, dict):
        raise ValueError('Invalid SARIF rule descriptor')
    component = descriptor.get('toolComponent')
    tool = run['tool']
    if component is None:
        rules = tool['driver'].get('rules', [])
    else:
        if not isinstance(component, dict):
            raise ValueError('Invalid SARIF tool component')
        index = component.get('index')
        extensions = tool.get('extensions', [])
        if type(index) is not int or not 0 <= index < len(extensions):
            raise ValueError('Unknown SARIF tool component')
        for field in ('name', 'guid'):
            if field in component and component[field] != extensions[index].get(field):
                raise ValueError('Inconsistent SARIF tool component identity')
        rules = extensions[index].get('rules', [])
    if result.get('ruleId') is not None and descriptor.get('id') is not None and result['ruleId'] != descriptor['id']:
        raise ValueError('Inconsistent SARIF rule IDs')
    rule_id = result.get('ruleId', descriptor.get('id'))
    if 'index' in descriptor and 'ruleIndex' in result and descriptor['index'] != result['ruleIndex']:
        raise ValueError('Inconsistent SARIF rule indices')
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


def security_score(rule):
    properties = rule.get('properties', {})
    if not isinstance(properties, dict):
        raise ValueError('Invalid SARIF rule properties')
    raw = properties.get('security-severity', 0)
    if type(raw) not in (str, int, float):
        raise ValueError('Invalid SARIF security score')
    try:
        score = float(raw)
    except ValueError as exc:
        raise ValueError('Invalid SARIF security score') from exc
    if not math.isfinite(score) or not 0 <= score <= 10:
        raise ValueError('Invalid SARIF security score')
    return score


def check_sarif(path, *, language=None):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError('Invalid SARIF document')
    runs = data.get('runs')
    if data.get('version') != '2.1.0' or not isinstance(runs, list) or not runs:
        raise ValueError('Missing SARIF runs')
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get('tool'), dict):
            raise ValueError('Missing SARIF tool')
        tool = run['tool']
        if not isinstance(tool.get('driver'), dict) or tool['driver'].get('name') != 'CodeQL':
            raise ValueError('Missing SARIF driver')
        extensions = tool.get('extensions', [])
        if not isinstance(extensions, list):
            raise ValueError('Invalid SARIF extensions')
        components = [tool['driver'], *extensions]
        rule_count = 0
        for component in components:
            if not isinstance(component, dict) or not isinstance(component.get('rules', []), list):
                raise ValueError('Invalid SARIF rule component')
            ids = set()
            for rule in component.get('rules', []):
                if not isinstance(rule, dict) or not isinstance(rule.get('id'), str) or not rule['id']:
                    raise ValueError('Invalid SARIF rule')
                if rule['id'] in ids:
                    raise ValueError('Duplicate SARIF rule ID')
                ids.add(rule['id'])
                security_score(rule)
                rule_count += 1
        if not rule_count:
            raise ValueError('Analysis contains no rules')
        if language is not None and not any(
                component.get('name') == f'codeql/{language}-queries' and component.get('rules')
                for component in components):
            raise ValueError('Missing expected CodeQL language query pack')
        if 'results' not in run or not isinstance(run['results'], list):
            raise ValueError('Missing SARIF results')
        invocations = run.get('invocations')
        if not isinstance(invocations, list) or not invocations:
            raise ValueError('Missing successful analyzer invocation')
        for invocation in invocations:
            if not isinstance(invocation, dict) or invocation.get('executionSuccessful') is not True:
                raise ValueError('Analyzer did not complete')
            for name in ('toolExecutionNotifications', 'toolConfigurationNotifications'):
                notices = invocation.get(name, [])
                if not isinstance(notices, list):
                    raise ValueError('Invalid analyzer notifications')
                for notice in notices:
                    if (not isinstance(notice, dict) or notice.get('level', 'warning') not in ('none', 'note', 'warning')
                            or 'exception' in notice):
                        raise ValueError('Analyzer reported an execution or configuration error')
        for result in run['results']:
            if not isinstance(result, dict):
                raise ValueError('Invalid SARIF result')
            rule = result_rule(run, result)
            configuration = rule.get('defaultConfiguration', {})
            if not isinstance(configuration, dict):
                raise ValueError('Invalid SARIF rule configuration')
            level = result.get('level', configuration.get('level', 'warning'))
            if level not in ('none', 'note', 'warning', 'error'):
                raise ValueError('Invalid SARIF finding level')
            score = security_score(rule)
            if level == 'error' or score >= 7:
                raise ValueError('Blocked SARIF finding: ' + result.get('ruleId', 'unknown'))


def check_outputs(directory, languages=('python', 'javascript')):
    root = Path(directory)
    expected = {root / f'{language}.sarif' for language in languages}
    if not expected or set(root.glob('*.sarif')) != expected:
        raise ValueError('Missing or unexpected language SARIF output; release blocked')
    for language in languages:
        check_sarif(root / f'{language}.sarif', language=language)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--language', choices=('python', 'javascript', 'javascript-typescript'))
    args = parser.parse_args()
    languages = (args.language.replace('-typescript', ''),) if args.language else ('python', 'javascript')
    check_outputs(args.directory, languages)
