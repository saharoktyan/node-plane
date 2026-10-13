"""Guard CI against silently disabled infrastructure coverage and skipped tests."""
import importlib.util
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ci_runner',
    Path(__file__).resolve().parents[1] / 'scripts/run_ci_tests.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CIRunnerTests(unittest.TestCase):
    def test_infrastructure_flags_are_required_and_cannot_leak_into_unit_suite(self):
        with patch.dict(os.environ, {}, clear=True):
            for suite in ('postgres', 'docker'):
                with self.subTest(suite=suite), self.assertRaises(SystemExit):
                    runner.run(suite)
        for flag, value in [('NODE_PLANE_TEST_POSTGRES_DSN', 'fixture'), ('NODE_PLANE_TEST_DOCKER', '1')]:
            with patch.dict(os.environ, {flag: value}, clear=True), self.assertRaises(SystemExit):
                runner.run('unit')

    def test_missing_modules_and_skipped_cases_are_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests' / 'test_fixture.py').touch()
            with patch.object(runner, 'ROOT', root), patch.dict(os.environ, {}, clear=True):
                for body in ('pass', 'skip', 'error'):
                    with self.subTest(body=body):
                        class Fixture(unittest.TestCase):
                            def runTest(self):
                                if body == 'skip':
                                    self.skipTest('dependency missing')
                                if body == 'error':
                                    raise RuntimeError('broken import or fixture')
                        suite = unittest.TestSuite([Fixture()])
                        with patch.object(runner.unittest.defaultTestLoader, 'loadTestsFromNames', return_value=suite), redirect_stderr(io.StringIO()):
                            self.assertEqual(runner.run('unit'), 0 if body == 'pass' else 1)

    def test_every_module_is_selected_by_exactly_one_suite(self):
        for suite in ('unit', 'postgres', 'docker'):
            names = [p.stem for p in (runner.ROOT / 'tests').glob('test_*.py')
                     if runner.suite_for(p.stem) == suite]
            self.assertTrue(names, suite)
        self.assertEqual(runner.suite_for('test_new_feature'), 'unit')
        self.assertEqual(runner.suite_for('test_new_feature_postgres'), 'postgres')
        self.assertEqual(runner.suite_for('test_new_feature_docker'), 'docker')
        self.assertEqual(runner.suite_for('test_agent_container_faults'), 'docker')
