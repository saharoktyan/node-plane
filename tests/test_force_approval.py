import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from backend.force_approval import publish
from backend.driver_transport import GrpcIntentDriver
from driver.v1 import runtime_service_pb2


def make_test_spec(node_key='node-test', kind='settings'):
    command_id = str(uuid4())
    ticket_id = str(uuid4())
    intent = {
        'kind': 'node_settings',
        'node_key': node_key,
        'command_id': command_id,
        'revision': 1,
        'protocols': ['awg'],
        'settings': {'public_host': '127.0.0.1', 'awg_port': 51820},
    }
    return {
        'ticket_id': ticket_id,
        'node_key': node_key,
        'command_id': command_id,
        'kind': kind,
        'intent': intent,
    }


class ForceApprovalPublishTests(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.shared = self.root / 'shared'
        self.shared.mkdir(mode=0o700)
        self.data = self.shared / 'data'
        self.data.mkdir(mode=0o700)

    def test_publish_creates_private_bound_approval_with_300s_expiry(self):
        spec = make_test_spec()
        intent_json = json.dumps(spec['intent'], sort_keys=True)

        with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': str(self.shared)}):
            now = int(time.time())
            publish(spec, intent_json)

            approvals_dir = self.data / 'force-recovery-approvals'
            self.assertTrue(approvals_dir.is_dir())
            dir_mode = approvals_dir.stat().st_mode
            self.assertEqual(dir_mode & 0o077, 0, 'approvals dir must be private (0700)')

            approval_file = approvals_dir / f"{spec['ticket_id']}.json"
            self.assertTrue(approval_file.is_file())
            file_mode = approval_file.stat().st_mode
            self.assertEqual(file_mode & 0o077, 0, 'approval file must be private (0600)')

            data = json.loads(approval_file.read_text())
            self.assertEqual(data['node_key'], spec['node_key'])
            self.assertEqual(data['command_id'], spec['command_id'])
            self.assertEqual(data['ticket_id'], spec['ticket_id'])
            self.assertEqual(data['kind'], spec['kind'])
            expected_hash = hashlib.sha256(intent_json.encode()).hexdigest()
            self.assertEqual(data['intent_sha256'], expected_hash)

            self.assertGreaterEqual(data['expires_at'], now + 295)
            self.assertLessEqual(data['expires_at'], now + 305)

            # Assert no raw intent body, credentials or tokens leaked in approval capability
            self.assertNotIn('settings', data)
            self.assertNotIn('intent', data)

    def test_publish_store_missing_or_unavailable(self):
        spec = make_test_spec()
        intent_json = json.dumps(spec['intent'], sort_keys=True)

        # Unset NODE_PLANE_SHARED_DIR
        with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': ''}):
            with self.assertRaises(ValueError) as ctx:
                publish(spec, intent_json)
            self.assertEqual(str(ctx.exception), 'force_approval_store_unavailable')

        # data directory missing
        empty_dir = self.root / 'empty_shared'
        empty_dir.mkdir(mode=0o700)
        with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': str(empty_dir)}):
            with self.assertRaises(ValueError) as ctx:
                publish(spec, intent_json)
            self.assertEqual(str(ctx.exception), 'force_approval_store_unavailable')

    def test_publish_rejects_untrusted_permissions_and_symlinks(self):
        spec = make_test_spec()
        intent_json = json.dumps(spec['intent'], sort_keys=True)

        # 1. approvals directory has loose permissions
        approvals_dir = self.data / 'force-recovery-approvals'
        approvals_dir.mkdir(mode=0o755)
        with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': str(self.shared)}):
            with self.assertRaises(ValueError) as ctx:
                publish(spec, intent_json)
            self.assertEqual(str(ctx.exception), 'force_approval_store_untrusted')
        os.chmod(approvals_dir, 0o700)

        # 2. data directory has loose permissions without sticky bit
        os.chmod(self.data, 0o777)
        with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': str(self.shared)}):
            with self.assertRaises(ValueError) as ctx:
                publish(spec, intent_json)
            self.assertEqual(str(ctx.exception), 'force_approval_store_untrusted')
        os.chmod(self.data, 0o700)

        # 3. data directory is a symlink
        real_data = self.root / 'real_data'
        real_data.mkdir(mode=0o700)
        symlink_shared = self.root / 'symlink_shared'
        symlink_shared.mkdir(mode=0o700)
        (symlink_shared / 'data').symlink_to(real_data)
        with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': str(symlink_shared)}):
            with self.assertRaises(ValueError) as ctx:
                publish(spec, intent_json)
            self.assertEqual(str(ctx.exception), 'force_approval_store_untrusted')


class RealDriverForceRecoveryTests(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.shared = Path(self.tmpdir.name) / 'shared'
        self.shared.mkdir(mode=0o700)
        (self.shared / 'data').mkdir(mode=0o700)

    def test_driver_force_recovery_publishes_on_prepare_and_forwards_rpc(self):
        spec = make_test_spec()
        intent_json = json.dumps(spec['intent'], sort_keys=True)

        mock_runtime = Mock()
        mock_response = runtime_service_pb2.BackendNodeSettingsResult(
            result_json=json.dumps({
                'ticket_id': spec['ticket_id'],
                'node_key': spec['node_key'],
                'restart_required': True,
            })
        )
        mock_runtime.ForceRecovery.return_value = mock_response

        driver = GrpcIntentDriver.__new__(GrpcIntentDriver)
        driver.timeout = 5
        driver.runtime = mock_runtime

        with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': str(self.shared)}):
            # Phase: prepare -> publishes approval and calls RPC
            result = driver.force_recovery(spec, 'prepare')
            self.assertEqual(result['ticket_id'], spec['ticket_id'])
            self.assertTrue(result['restart_required'])

            # Verify approval capability published on disk
            approval_file = self.shared / 'data' / 'force-recovery-approvals' / f"{spec['ticket_id']}.json"
            self.assertTrue(approval_file.is_file())
            approval = json.loads(approval_file.read_text())
            self.assertEqual(approval['ticket_id'], spec['ticket_id'])
            self.assertEqual(approval['command_id'], spec['command_id'])
            self.assertEqual(approval['node_key'], spec['node_key'])
            self.assertEqual(approval['kind'], spec['kind'])
            self.assertEqual(approval['intent_sha256'], hashlib.sha256(intent_json.encode()).hexdigest())

            # Verify RPC arguments
            mock_runtime.ForceRecovery.assert_called_once()
            call_arg = mock_runtime.ForceRecovery.call_args[0][0]
            self.assertEqual(call_arg.node_key, spec['node_key'])
            self.assertEqual(call_arg.ticket_id, spec['ticket_id'])
            self.assertEqual(call_arg.command_id, spec['command_id'])
            self.assertEqual(call_arg.operation_kind, spec['kind'])
            self.assertEqual(call_arg.intent_json, intent_json)
            self.assertEqual(call_arg.phase, 'prepare')

            # Phase: retire -> does NOT republish approval, forwards RPC
            mock_runtime.reset_mock()
            mock_retire_response = runtime_service_pb2.BackendNodeSettingsResult(
                result_json=json.dumps({
                    'ticket_id': spec['ticket_id'],
                    'node_key': spec['node_key'],
                    'command_id': spec['command_id'],
                    'kind': spec['kind'],
                    'forced': True,
                    'outcome_unknown': True,
                })
            )
            mock_runtime.ForceRecovery.return_value = mock_retire_response

            with patch('backend.force_approval.publish') as mock_publish:
                retire_result = driver.force_recovery(spec, 'retire')
                mock_publish.assert_not_called()
                self.assertTrue(retire_result['forced'])
                self.assertTrue(retire_result['outcome_unknown'])

            mock_runtime.ForceRecovery.assert_called_once()
            retire_call_arg = mock_runtime.ForceRecovery.call_args[0][0]
            self.assertEqual(retire_call_arg.phase, 'retire')
