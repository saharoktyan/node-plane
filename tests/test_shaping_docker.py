"""Speed limits on a real kernel and Xray (tests/e2e/shaping).

Part of the Docker suite. Builds a bench image (needs network once) and runs
the agent's shaping pass in a privileged container without network, so the
host is untouched. Needs cargo and jq on the host.
"""
import os
from pathlib import Path
import subprocess
import unittest

BENCH = Path(__file__).resolve().parent / 'e2e/shaping/run.sh'


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires a privileged Docker bench')
class ShapingBenchTests(unittest.TestCase):
    def test_limits_apply_follow_evictions_rebuild_and_lift(self):
        result = subprocess.run([str(BENCH)], capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('FAIL', result.stdout)
