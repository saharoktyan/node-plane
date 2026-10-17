import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('awg_ipv6', ROOT / 'runtime_assets/awg_ipv6.py')
IPV6 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IPV6)


class AwgIPv6Tests(unittest.TestCase):
    def test_full_tunnel_captures_both_families_without_changing_split_routes(self):
        self.assertEqual(IPV6.capture_routes('0.0.0.0/0'), '0.0.0.0/0, ::/0')
        self.assertEqual(IPV6.capture_routes('0.0.0.0/0, ::/0'), '0.0.0.0/0, ::/0')
        self.assertEqual(IPV6.capture_routes('10.0.0.0/8'), '10.0.0.0/8')

    def test_migration_is_repeatable_preserves_keys_and_assigns_unique_ipv6(self):
        old = ('[Interface]\nPrivateKey = server\nAddress = 10.8.1.0/24\n'
               '\n# first\n[Peer]\nPublicKey = first\nPresharedKey = first-psk\nAllowedIPs = 10.8.1.2/32\n'
               '\n# second\n[Peer]\nPublicKey = second\nPresharedKey = second-psk\nAllowedIPs = 10.8.1.3/32\n')
        updated = IPV6.migrate(old)
        self.assertEqual(IPV6.migrate(updated), updated)
        self.assertIn('Address = 10.8.1.0/24, fd42:6e70:6177:2::1/64', updated)
        self.assertIn('10.8.1.2/32, fd42:6e70:6177:2::a08:102/128', updated)
        self.assertIn('10.8.1.3/32, fd42:6e70:6177:2::a08:103/128', updated)
        for line in old.splitlines():
            if not line.startswith(('Address', 'AllowedIPs')):
                self.assertIn(line, updated)

    def test_existing_custom_ipv6_does_not_remove_managed_address(self):
        addresses = IPV6.dual_addresses('10.8.1.2/32, fd00::2/128')
        self.assertEqual(addresses, '10.8.1.2/32, fd00::2/128, fd42:6e70:6177:2::a08:102/128')
        self.assertEqual(IPV6.dual_addresses(addresses), addresses)

    def test_backend_and_agent_reject_invalid_egress_policy(self):
        from backend.nodes import _validate, protocol_defaults
        from backend.authorization import AccessDenied
        self.assertEqual(protocol_defaults({}, ['xray'])['ipv6_outbound'], 'on')
        for choice in ('on', 'off'):
            self.assertEqual(_validate({'settings': {'ipv6_outbound': choice}}, create=False)['settings']['ipv6_outbound'], choice)
        for choice in ('auto', True, '', 'ON'):
            with self.assertRaises(AccessDenied):
                _validate({'settings': {'ipv6_outbound': choice}}, create=False)
