import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('awg_peer_state', ROOT / 'runtime_assets/awg-peer-state.py')
peer_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer_state)

INTERFACE = '[Interface]\nPrivateKey = server-one\nAddress = 10.8.1.0/24\n'
PEER = '# node-alice\n[Peer]\nPublicKey = public-one\nPresharedKey = shared-one\nAllowedIPs = 10.8.1.2/32\n'


class AwgPeerStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'wg0.conf'
        self.clients = self.root / 'clients'
        self.clients.mkdir()
        self.config.write_text(INTERFACE + '\n' + PEER)
        (self.clients / 'node-alice.txt').write_text('private client config')

    def mutate(self, action):
        return peer_state.mutate(action, self.config, self.clients, 'node-alice', 'awg', 'wg0')

    def test_revoke_persists_before_live_removal_and_restores_same_keys(self):
        def removed(*args, **kwargs):
            self.assertNotIn('public-one', self.config.read_text())
            self.assertTrue((self.clients / 'node-alice.revoked.json').exists())
        with patch.object(peer_state, 'docker', side_effect=removed):
            self.assertEqual(self.mutate('revoke'), 0)
        self.assertTrue((self.clients / 'node-alice.txt').exists())
        with patch.object(peer_state, 'docker') as docker:
            self.assertEqual(self.mutate('restore'), 0)
        self.assertIn(PEER, self.config.read_text())
        self.assertEqual(docker.call_args.kwargs['input'], 'shared-one\n')
        self.assertIn('public-one', docker.call_args.args)

    def test_failed_live_revoke_can_be_retried_without_restoring_access(self):
        with patch.object(peer_state, 'docker', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                self.mutate('revoke')
        self.assertNotIn('public-one', self.config.read_text())
        with patch.object(peer_state, 'docker') as docker:
            self.mutate('revoke')
        docker.assert_called_once()
        self.assertNotIn('public-one', self.config.read_text())

    def test_clean_reinstall_does_not_restore_peer_from_old_server(self):
        with patch.object(peer_state, 'docker'):
            self.mutate('revoke')
        self.config.write_text(INTERFACE.replace('server-one', 'server-two'))
        with patch.object(peer_state, 'docker') as docker:
            self.assertEqual(self.mutate('restore'), 2)
        docker.assert_not_called()
        self.assertNotIn('public-one', self.config.read_text())

    def test_address_collision_does_not_restore_wrong_peer(self):
        with patch.object(peer_state, 'docker'):
            self.mutate('revoke')
        self.config.write_text(INTERFACE + '\n' + PEER.replace('node-alice', 'node-bob').replace('public-one', 'public-two'))
        with patch.object(peer_state, 'docker') as docker:
            self.assertEqual(self.mutate('restore'), 2)
        docker.assert_not_called()

    def test_failed_live_restore_keeps_durable_peer_for_retry(self):
        with patch.object(peer_state, 'docker'):
            self.mutate('revoke')
        with patch.object(peer_state, 'docker', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                self.mutate('restore')
        with patch.object(peer_state, 'docker'):
            self.mutate('restore')
        self.assertEqual(self.config.read_text().count('# node-alice'), 1)
