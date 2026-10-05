from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runtime_assets"))
import awg_profile

spec = importlib.util.spec_from_file_location("refresh_awg_config", ROOT / "runtime_assets/refresh-awg-config.py")
refresh_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh_module)


class ConfigRefreshTests(unittest.TestCase):
    def test_awg_refresh_uses_node_and_profile_name(self) -> None:
        self.assertEqual(refresh_module.profile_description("Moscow #1 · alice"), "Moscow #1 · alice")
        self.assertEqual(refresh_module.profile_description(""), "AmneziaWG")

    def test_existing_awg_peer_gets_current_endpoint_and_entropy(self) -> None:
        old = """[Interface]
PrivateKey = client-secret
PublicKey = client-pub
Address = 10.8.1.2/32
I1 = old

[Peer]
PublicKey = old-server
PresharedKey = shared-secret
Endpoint = old.example:51820
AllowedIPs = 0.0.0.0/0
"""
        profile = awg_profile.new_profile("quic")
        current = "[Interface]\n" + "\n".join(f"{key} = {value}" for key, value in profile.items()) + "\n"
        updated = refresh_module.refresh(old, current, "new.example", "53000", "new-server")
        self.assertIn("PrivateKey = client-secret", updated)
        self.assertIn("PresharedKey = shared-secret", updated)
        self.assertIn("Address = 10.8.1.2/32", updated)
        self.assertIn(f"I1 = {profile['I1']}", updated)
        self.assertIn(f"HeaderProtectionKey = {profile['HeaderProtectionKey']}", updated)
        self.assertIn("PublicKey = new-server", updated)
        self.assertIn("Endpoint = new.example:53000", updated)
        self.assertNotIn("old.example", updated)

    def test_awg_refresh_rejects_unmigrated_server(self) -> None:
        with self.assertRaisesRegex(ValueError, "3.1"):
            refresh_module.refresh("[Interface]\nPrivateKey = old\n", "[Interface]\nJc = 4\n", "new.example", "51820", "server")
