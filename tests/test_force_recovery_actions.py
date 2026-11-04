from pathlib import Path
import re
import sys
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

FORCE = Path(__file__).resolve().parent.parent / 'rust' / 'node-agent' / 'src' / 'intents' / 'force.rs'


class ForceRecoveryActionsTests(TestCase):
    def test_every_backend_node_job_action_can_be_force_retired(self):
        from backend.node_operations import ACTIONS
        listed = re.search(r'NODE_ACTIONS: \[&str; \d+\] = \[(.*?)\];', FORCE.read_text(), re.S)
        self.assertIsNotNone(listed)
        self.assertLessEqual(set(ACTIONS), set(re.findall(r'"([a-z_]+)"', listed.group(1))))
