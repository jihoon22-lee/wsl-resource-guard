"""The full CI profile must exercise optional web and cryptographic behavior."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flask  # noqa: F401
import cryptography  # noqa: F401

def unprivileged(suite):
    result = unittest.TestSuite()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            result.addTests(unprivileged(item))
        elif item.__class__.__name__ != 'RootIsolationTests':
            result.addTest(item)
    return result

result = unittest.TextTestRunner(verbosity=1).run(unprivileged(unittest.defaultTestLoader.discover('tests')))
if result.skipped:
    print(f'Full CI profile unexpectedly skipped {len(result.skipped)} tests.', file=sys.stderr)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
