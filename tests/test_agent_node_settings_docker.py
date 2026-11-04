"""Node settings through the real apply-node-settings.sh and the built
agent, with fake deploy scripts. In the Docker suite, where CI builds the
agent."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_agent_node_settings_intent as base
from tests.test_runtime_docker import AGENT_BINARY


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires the Docker suite')
class AgentNodeSettingsDockerTests(unittest.TestCase):
    setUp = base.AgentNodeSettingsIntentTests.setUp
    intent = base.AgentNodeSettingsIntentTests.intent

    def test_real_settings_script_preserves_identities_and_updates_protocol_parameters(self):
        with patch.dict(os.environ, {'NODE_PLANE_AGENT_BIN': str(AGENT_BINARY),
                                     'NODE_PLANE_RUNTIME_ROOT': str(Path(self.temp.name))}):
            base.AgentNodeSettingsIntentTests.real_settings_script_preserves_identities_and_updates_protocol_parameters(self)


if __name__ == '__main__':
    unittest.main()
