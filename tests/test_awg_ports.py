import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch

from backend.awg_ports import allowed_port
from backend.nodes import protocol_defaults
from backend.node_settings import _digest, _valid_result

spec = importlib.util.spec_from_file_location('test_awg_ports_runtime',
    Path(__file__).resolve().parents[1] / 'runtime_assets' / 'awg_ports.py')
ports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ports)


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

    def test_candidates_are_bounded_and_controller_accepts_the_same_policy(self):
        for preset, preferred in [('quic', 443), ('dns', 53), ('chaos', 9990)]:
            candidates = ports.candidates(preset, preferred)
            self.assertEqual(candidates[0], preferred)
            self.assertEqual(len(candidates), len(set(candidates)))
            self.assertLessEqual(len(candidates), 128)
            for selected in candidates:
                self.assertTrue(allowed_port(preset, preferred, selected))
        self.assertFalse(allowed_port('chaos', 9990, 8000))

    def test_foreign_docker_mapping_is_skipped_even_without_listener(self):
        with patch.object(ports, 'docker_bindings', return_value=({443: [False]}, set())), \
             patch.object(ports, 'can_bind', return_value=True) as bind:
            self.assertEqual(ports.select_port('quic', 443, '/runtime/data/wg0.conf', 'awg'), 8443)
        bind.assert_called_once_with(8443)

    def test_owned_running_container_preserves_port_on_reapply(self):
        with patch.object(ports, 'docker_bindings', return_value=({8443: [True]}, {8443})), \
             patch.object(ports, 'can_bind', return_value=False) as bind:
            self.assertEqual(ports.select_port('quic', 8443, '/runtime/data/wg0.conf', 'awg'), 8443)
        bind.assert_not_called()

    def test_docker_inventory_requires_mount_ownership_and_reads_dynamic_publications(self):
        records = [{'Name': '/awg', 'State': {'Running': True}, 'Mounts': [
            {'Type': 'bind', 'Source': '/foreign/data', 'Destination': '/opt/amnezia/awg'}],
            'HostConfig': {'PortBindings': {'443/udp': [{'HostPort': ''}]}},
            'NetworkSettings': {'Ports': {'443/udp': [{'HostPort': '8443'}]}}}]
        results = [subprocess.CompletedProcess([], 0, 'container-id'),
            subprocess.CompletedProcess([], 0, json.dumps(records))]
        with patch.object(ports.subprocess, 'run', side_effect=results):
            reserved, owned = ports.docker_bindings('/managed/data/wg0.conf', 'awg')
        self.assertEqual(reserved, {8443: [False]})
        self.assertFalse(owned)

    def test_occupied_ports_fall_back_and_exhaustion_fails(self):
        with patch.object(ports, 'docker_bindings', return_value=({}, set())), \
             patch.object(ports, 'can_bind', side_effect=lambda port: port == 5300):
            self.assertEqual(ports.select_port('dns', 53, '/runtime/data/wg0.conf', 'awg'), 5300)
        with patch.object(ports, 'docker_bindings', return_value=({}, set())), \
             patch.object(ports, 'can_bind', return_value=False):
            with self.assertRaisesRegex(ValueError, 'no available'):
                ports.select_port('chaos', 1200, '/runtime/data/wg0.conf', 'awg')

    def test_real_udp_listener_is_detected_without_confusing_tcp(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind(('0.0.0.0', 0))
            self.assertFalse(ports.can_bind(listener.getsockname()[1]))

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
