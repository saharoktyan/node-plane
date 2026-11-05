import unittest

from backend.service_instances import changed_engines


class ChangedEnginesTests(unittest.TestCase):
    def setUp(self):
        self.applied = {'protocols': ['awg', 'xray'],
                        'settings': {'public_host': 'node.example', 'awg_port': 51820, 'xray_tcp_port': 443}}

    def test_an_engine_setting_touches_only_that_engine(self):
        settings = {**self.applied['settings'], 'awg_port': 51821}
        self.assertEqual(changed_engines(self.applied, ['awg', 'xray'], settings), {'awg'})
        settings = {**self.applied['settings'], 'xray_tcp_port': 8443}
        self.assertEqual(changed_engines(self.applied, ['awg', 'xray'], settings), {'xray'})

    def test_a_shared_setting_touches_every_engine(self):
        settings = {**self.applied['settings'], 'public_host': 'other.example'}
        self.assertEqual(changed_engines(self.applied, ['awg', 'xray'], settings), {'awg', 'xray'})

    def test_added_and_removed_engines_are_touched(self):
        self.assertEqual(changed_engines(self.applied, ['xray'], self.applied['settings']), {'awg'})
        applied = {'protocols': ['xray'], 'settings': self.applied['settings']}
        self.assertEqual(changed_engines(applied, ['awg', 'xray'], self.applied['settings']), {'awg'})

    def test_unknown_applied_state_touches_every_configured_engine(self):
        self.assertEqual(changed_engines(None, ['xray'], {}), {'xray'})

    def test_compared_settings_can_be_limited(self):
        settings = {**self.applied['settings'], 'policy_value': 'x'}
        self.assertEqual(changed_engines(self.applied, ['awg', 'xray'], settings, keys={'awg_port'}), set())
