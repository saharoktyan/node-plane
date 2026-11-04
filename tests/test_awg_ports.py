import json
from pathlib import Path
import re
import unittest

from backend.awg_ports import allowed_port
from backend.nodes import protocol_defaults
from backend.node_settings import _digest, _valid_result

AGENT_PORTS = Path(__file__).resolve().parents[1] / 'rust/node-agent/src/protocols/awg/ports.rs'


class AwgPortTests(unittest.TestCase):
    def test_preset_defaults_and_manual_override(self):
        for preset, expected in [('quic', 443), ('dns', 53)]:
            settings = protocol_defaults({'awg_i1_preset': preset}, ['awg'])
            self.assertEqual(settings['awg_port'], expected)
            self.assertEqual(settings['awg_port_mode'], 'auto')
        chaos = protocol_defaults({'awg_i1_preset': 'chaos'}, ['awg'])
        self.assertTrue(1024 <= chaos['awg_port'] <= 9999)
        self.assertEqual(protocol_defaults(chaos, ['awg']), chaos)
        custom = protocol_defaults({'awg_port': 51820}, ['awg'])
        self.assertEqual((custom['awg_port'], custom['awg_port_mode']), (51820, 'manual'))

    def test_controller_accepts_every_port_the_agent_may_select(self):
        source = AGENT_PORTS.read_text()
        for preset, preferred in [('quic', 443), ('dns', 53)]:
            listed = re.search(preset.upper() + r': \[u16; \d+\] = \[(.*?)\];', source)
            self.assertIsNotNone(listed)
            for selected in [preferred, *map(int, listed.group(1).split(','))]:
                self.assertTrue(allowed_port(preset, preferred, selected))
        # Chaos: the 128 ports from the preferred one, wrapping within 1024-9999.
        self.assertIn('(0..128)', source)
        self.assertIn('1024 + (preferred - 1024 + offset) % 8976', source)
        for offset in range(128):
            self.assertTrue(allowed_port('chaos', 9990, 1024 + (9990 - 1024 + offset) % 8976))
        self.assertFalse(allowed_port('chaos', 9990, 8000))

    def test_selected_port_is_bound_to_effective_settings_digest(self):
        intent = {'node_key': 'node', 'revision': 1, 'protocols': ['awg'],
            'settings': {'awg_port': 443, 'awg_i1_preset': 'quic', 'awg_port_mode': 'auto'}}
        effective = dict(intent, settings=dict(intent['settings'], awg_port=8443))
        result = {'node_key': 'node', 'revision': 1, 'awg_port': 8443, 'settings_sha256': _digest(effective)}
        self.assertTrue(_valid_result(intent, json.dumps(result)))
        result['settings_sha256'] = _digest(intent)
        self.assertFalse(_valid_result(intent, json.dumps(result)))
        result['awg_port'] = 65000
        self.assertFalse(_valid_result(intent, json.dumps(result)))
        intent['settings']['awg_port_mode'] = 'manual'
        self.assertFalse(_valid_result(intent, json.dumps(result)))
