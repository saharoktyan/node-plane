"""Run every unittest module, with real infrastructure isolated from DB doubles.

Run each suite in a fresh process. Unexpected skips fail CI instead of turning
missing dependencies or disabled integration coverage into a green check.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCKER_MODULES = {'test_runtime_docker', 'test_agent_container_faults'}


def suite_for(name: str) -> str:
    if name.endswith('_postgres'):
        return 'postgres'
    if name in DOCKER_MODULES or name.endswith('_docker'):
        return 'docker'
    return 'unit'


def run(suite: str) -> int:
    if suite == 'postgres':
        if not os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'):
            raise SystemExit('PostgreSQL suite requires a disposable NODE_PLANE_TEST_POSTGRES_DSN')
        import psycopg
        if getattr(psycopg, '__fake__', False):
            raise SystemExit('PostgreSQL suite must run in a fresh process with real psycopg')
    elif suite == 'docker':
        if os.environ.get('NODE_PLANE_TEST_DOCKER') != '1':
            raise SystemExit('Docker suite requires NODE_PLANE_TEST_DOCKER=1')
    else:
        if os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN') or os.environ.get('NODE_PLANE_TEST_DOCKER') == '1':
            raise SystemExit('Run infrastructure suites separately from unit database doubles')

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'app'))
    # Fault fixtures launch Python workers/RPC servers in child processes.
    # sys.path alone does not propagate their application imports.
    os.environ['PYTHONPATH'] = os.pathsep.join((str(ROOT / 'app'), str(ROOT),
                                               os.environ.get('PYTHONPATH', '')))
    names = [f'tests.{path.stem}' for path in sorted((ROOT / 'tests').glob('test_*.py'))
             if suite_for(path.stem) == suite]
    if not names:
        raise SystemExit('No test modules selected')
    tests = unittest.defaultTestLoader.loadTestsFromNames(names)
    if not tests.countTestCases():
        raise SystemExit('No test cases selected')
    if suite == 'postgres' and getattr(sys.modules.get('psycopg'), '__fake__', False):
        raise SystemExit('A PostgreSQL test module imported a fake psycopg')
    result = unittest.TextTestRunner(verbosity=2).run(tests)
    if result.skipped:
        print(f'CI requires full coverage: {len(result.skipped)} unexpected skipped tests', file=sys.stderr)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite', choices=('unit', 'postgres', 'docker'))
    raise SystemExit(run(parser.parse_args().suite))
