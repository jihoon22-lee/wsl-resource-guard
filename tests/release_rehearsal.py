"""Inject failures only into disposable copies of a manual release rehearsal's SARIF."""
import argparse
import json
from pathlib import Path


def inject_failure(directory: Path, scenario: str) -> None:
    if scenario == 'none':
        return
    if scenario == 'check-failure':
        raise RuntimeError('Injected release check failure')
    if scenario == 'analysis-missing':
        (directory/'javascript.sarif').unlink()
        return
    if scenario != 'security-high':
        raise ValueError('Unknown rehearsal scenario')
    path = directory/'python.sarif'
    document = json.loads(path.read_text())
    run = document['runs'][0]
    # Create a valid driver rule/result independently of query-pack rule indices.
    rule = {'id': 'rehearsal/blocked-finding', 'properties': {'security-severity': '9.8'}}
    run['tool']['driver'].setdefault('rules', []).append(rule)
    run['results'].append({'ruleId': rule['id'], 'level': 'warning'})
    path.write_text(json.dumps(document))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('scenario', choices=['none', 'check-failure', 'analysis-missing', 'security-high'])
    args = parser.parse_args()
    inject_failure(args.directory, args.scenario)
